from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="runtime-receipt", assignee="test-worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        run_id = claimed.current_run_id
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return tid


def _identity():
    return {
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "api_mode": "responses",
        "session_id": "sess_actual",
        "source": "agent_runtime_after_provider_response",
    }


def test_complete_stamps_trusted_runtime_identity(monkeypatch, worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_SESSION_ID", "sess_actual")
    out = json.loads(
        kt._handle_complete(
            {
                "summary": "verified",
                "metadata": {
                    "runtime_identity": {
                        "provider": "spoofed",
                        "model": "spoofed",
                    }
                },
            },
            runtime_identity=_identity(),
        )
    )
    assert out["ok"] is True
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run is not None
        assert run.metadata["worker_session_id"] == "sess_actual"
        assert run.metadata["runtime_identity"] == _identity()
    finally:
        conn.close()


def test_block_stamps_trusted_runtime_identity(monkeypatch, worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_SESSION_ID", "sess_actual")
    out = json.loads(
        kt._handle_block(
            {"reason": "external input required", "kind": "needs_input"},
            runtime_identity=_identity(),
        )
    )
    assert out["ok"] is True
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
        assert run is not None
        assert run.metadata["runtime_identity"] == _identity()
    finally:
        conn.close()


def test_review_and_changes_runs_stamp_runtime_identity(monkeypatch, worker_env):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb

    monkeypatch.setenv("HERMES_SESSION_ID", "sess_implementer")
    review = json.loads(
        kt._handle_request_review(
            {"summary": "ready for independent review", "reviewer": "reviewer"},
            runtime_identity=_identity(),
        )
    )
    assert review.get("ok") is True, review

    conn = kb.connect()
    try:
        implementation_run = kb.latest_run(conn, worker_env)
        assert implementation_run is not None
        assert implementation_run.metadata is not None
        assert implementation_run.metadata["runtime_identity"] == _identity()
        claimed = kb.claim_review_task(conn, worker_env)
        assert claimed is not None
        review_run_id = claimed.current_run_id
    finally:
        conn.close()

    reviewer_identity = {
        **_identity(),
        "model": "gpt-5.6-sol",
        "session_id": "sess_reviewer",
    }
    monkeypatch.setenv("HERMES_SESSION_ID", "sess_reviewer")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review_run_id))
    changes = json.loads(
        kt._handle_request_changes(
            {"reason": "one correction required"},
            runtime_identity=reviewer_identity,
        )
    )
    assert changes["ok"] is True

    conn = kb.connect()
    try:
        review_run = kb.latest_run(conn, worker_env)
        assert review_run is not None
        assert review_run.metadata is not None
        assert review_run.outcome == "changes_requested"
        assert review_run.metadata["runtime_identity"] == reviewer_identity
    finally:
        conn.close()


def test_successful_lifecycle_handoff_latches_worker_stop(monkeypatch):
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    assert _record_successful_kanban_handoff(
        agent,
        "kanban_request_review",
        json.dumps({"ok": True, "task_id": "t_1", "run_id": 7, "status": "review"}),
    )
    assert agent._kanban_lifecycle_handoff == {
        "tool": "kanban_request_review",
        "task_id": "t_1",
        "run_id": 7,
        "status": "review",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "task_id": "t_successor", "run_id": 7},
        {"ok": True, "task_id": "t_1", "run_id": 8},
        {"ok": True, "task_id": "t_1"},
    ],
)
def test_lifecycle_handoff_requires_exact_dispatcher_claim(monkeypatch, payload):
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")

    assert not _record_successful_kanban_handoff(
        agent,
        "kanban_request_review",
        json.dumps(payload),
    )
    assert not hasattr(agent, "_kanban_lifecycle_handoff")


def test_lifecycle_handoff_requires_dispatcher_run_id(monkeypatch):
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)

    assert not _record_successful_kanban_handoff(
        agent,
        "kanban_complete",
        json.dumps({"ok": True, "task_id": "t_1", "run_id": 7}),
    )
    assert not hasattr(agent, "_kanban_lifecycle_handoff")


def test_stale_handoff_latch_cannot_stop_successor_claim(monkeypatch):
    from agent.tool_executor import _kanban_handoff_matches_current_claim

    agent = SimpleNamespace(
        _kanban_lifecycle_handoff={
            "tool": "kanban_request_changes",
            "task_id": "t_1",
            "run_id": 7,
            "status": "ready",
        }
    )
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "8")
    assert not _kanban_handoff_matches_current_claim(agent)
    assert agent._kanban_lifecycle_handoff is None


def test_lifecycle_result_uses_closed_worker_run_not_later_latest_run(
    monkeypatch,
    worker_env,
):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    worker_run_id = int(__import__("os").environ["HERMES_KANBAN_RUN_ID"])
    monkeypatch.setattr(
        kb,
        "latest_run",
        lambda _conn, _task_id: SimpleNamespace(id=worker_run_id + 1),
    )

    out = json.loads(
        kt._handle_block(
            {"reason": "external input required", "kind": "needs_input"},
            runtime_identity=_identity(),
        )
    )

    assert out["ok"] is True
    assert out["run_id"] == worker_run_id


def test_runtime_identity_uses_effective_route_not_initial_config():
    from agent.tool_executor import _runtime_identity

    agent = SimpleNamespace(
        provider="fallback-provider",
        model="effective-model",
        api_mode="responses",
        session_id="effective-session",
        _session_init_model_config={
            "provider": "requested-provider",
            "model": "requested-model",
            "api_mode": "chat_completions",
        },
    )

    assert _runtime_identity(agent) == {
        "provider": "fallback-provider",
        "model": "effective-model",
        "api_mode": "responses",
        "session_id": "effective-session",
        "source": "agent_runtime_after_provider_response",
    }


def test_failed_lifecycle_call_does_not_latch_worker_stop(monkeypatch):
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    assert not _record_successful_kanban_handoff(
        agent,
        "kanban_request_changes",
        json.dumps({"error": "ownership mismatch"}),
    )
    assert not hasattr(agent, "_kanban_lifecycle_handoff")


def test_delegate_child_does_not_latch_parent_worker_stop(monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    with non_dispatcher_owned_context():
        assert not _record_successful_kanban_handoff(
            agent,
            "kanban_complete",
            json.dumps({"ok": True, "task_id": "t_parent", "run_id": 4}),
        )
    assert not hasattr(agent, "_kanban_lifecycle_handoff")


def test_orchestrator_without_worker_env_does_not_latch_stop(monkeypatch):
    from agent.tool_executor import _record_successful_kanban_handoff

    agent = SimpleNamespace()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    assert not _record_successful_kanban_handoff(
        agent,
        "kanban_complete",
        json.dumps({"ok": True, "task_id": "t_explicit", "run_id": 9}),
    )
    assert not hasattr(agent, "_kanban_lifecycle_handoff")
