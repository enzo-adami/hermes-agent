"""End-to-end watchdog behavior for drip-feeding providers (#83657).

Two guards, driven through the real poll loop in
``interruptible_streaming_api_call``:

1. A stream that only emits content-free keep-alive frames must trip the
   stale-stream detector.  Before the fix every frame refreshed
   ``last_chunk_time``, so the detector — whose own comment promises to kill
   "connections kept alive by SSE pings but no actual data" — never fired.
2. A stream that keeps emitting *real* content forever must still be bounded
   by the wall-clock ceiling.  No activity-based guard can catch that shape;
   the observed 1239s nousresearch call is exactly it.

The fake streams are self-bounding (chunk cap + abort event) so a regression
fails the test instead of hanging the suite.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _keepalive_chunk():
    """Content-free frame: the OpenAI-compatible shape of an SSE heartbeat."""
    delta = SimpleNamespace(
        content=None, tool_calls=None, reasoning_content=None, reasoning=None
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(index=0, delta=delta, finish_reason=None)],
        model="drip-model",
        usage=None,
    )


def _content_chunk(text="tok ", finish_reason=None):
    delta = SimpleNamespace(
        content=text, tool_calls=None, reasoning_content=None, reasoning=None
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)],
        model="drip-model",
        usage=None,
    )


class _DripStream:
    """Emits ``factory()`` every ``interval`` seconds until aborted or capped."""

    def __init__(self, factory, aborted, interval=0.1, max_chunks=200):
        self._factory = factory
        self._aborted = aborted
        self._interval = interval
        self._max_chunks = max_chunks
        self.emitted = 0

    def __iter__(self):
        while self.emitted < self._max_chunks:
            if self._aborted.is_set():
                raise RuntimeError("connection closed by watchdog")
            time.sleep(self._interval)
            self.emitted += 1
            yield self._factory()

    def close(self):
        self._aborted.set()


def _make_agent():
    agent = AIAgent(
        api_key="test-key",
        base_url="https://inference-api.nousresearch.com/v1",
        model="drip-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    agent._consecutive_stale_streams = 0
    return agent


@pytest.fixture
def drip_env(monkeypatch):
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "1")
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "99")
    monkeypatch.setattr(
        "hermes_cli.timeouts.get_provider_max_call_timeout",
        lambda *_a, **_kw: None,
    )


def _run_against(stream, aborted):
    """Drive one streaming call against ``stream``; return (agent, error)."""
    client = MagicMock()
    client.chat.completions.create.return_value = stream

    agent = _make_agent()
    error = None
    # The poll loop is a stranger thread, so it aborts the transport's sockets
    # (#29507) instead of closing the client; the worker owns the FD release.
    # Either route must terminate the fake stream.
    with (
        patch.object(AIAgent, "_create_request_openai_client", return_value=client),
        patch.object(
            AIAgent,
            "_close_request_openai_client",
            side_effect=lambda *_a, **_kw: aborted.set(),
        ),
        patch.object(
            AIAgent,
            "_abort_request_openai_client",
            side_effect=lambda *_a, **_kw: aborted.set(),
        ),
    ):
        try:
            agent._interruptible_streaming_api_call({"model": "drip-model"})
        except BaseException as exc:  # noqa: BLE001 - the outcome under test
            error = exc
    return agent, error


class TestKeepAliveDrip:
    def test_keepalive_only_stream_trips_the_stale_detector(
        self, drip_env, monkeypatch
    ):
        """Heartbeats are not progress — the detector must still fire."""
        monkeypatch.setenv("HERMES_STREAM_MAX_CALL_SECONDS", "0")  # ceiling off
        aborted = threading.Event()
        stream = _DripStream(_keepalive_chunk, aborted, interval=0.1, max_chunks=120)

        agent, _error = _run_against(stream, aborted)

        assert agent._consecutive_stale_streams >= 1
        # 12s worth of heartbeats were queued up; the detector cut the
        # connection long before the fake provider ran out of them.
        assert stream.emitted < 120

    def test_content_drip_keeps_the_stale_window_open(self, drip_env, monkeypatch):
        """Real tokens still reset the window — no regression for slow models."""
        monkeypatch.setenv("HERMES_STREAM_MAX_CALL_SECONDS", "0")
        aborted = threading.Event()
        stream = _DripStream(_content_chunk, aborted, interval=0.1, max_chunks=15)

        agent, error = _run_against(stream, aborted)

        assert error is None
        assert agent._consecutive_stale_streams == 0


class TestWallClockCeiling:
    def test_endless_productive_stream_is_bounded(self, drip_env, monkeypatch):
        """The shape no activity-based guard can catch (#83657)."""
        monkeypatch.setenv("HERMES_STREAM_MAX_CALL_SECONDS", "1")
        aborted = threading.Event()
        stream = _DripStream(_content_chunk, aborted, interval=0.1, max_chunks=200)

        started = time.time()
        _agent, error = _run_against(stream, aborted)
        elapsed = time.time() - started

        assert isinstance(error, TimeoutError)
        assert "wall-clock ceiling" in str(error)
        assert elapsed < 10.0  # would be 20s of chunks without the ceiling
        assert aborted.is_set()  # the transport was torn down, not leaked

    def test_fast_stream_under_the_ceiling_is_untouched(self, drip_env, monkeypatch):
        """Control: the ceiling must not disturb a normal, quick response."""
        monkeypatch.setenv("HERMES_STREAM_MAX_CALL_SECONDS", "30")
        aborted = threading.Event()
        chunks = iter(
            [_content_chunk("Hello"), _content_chunk("!", finish_reason="stop")]
        )

        _agent, error = _run_against(chunks, aborted)

        assert error is None
