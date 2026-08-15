"""Persistence/replay contract for the degenerate-repetition sanitizer."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.anthropic_adapter import _convert_assistant_message
from agent.chat_completion_helpers import build_assistant_message
from agent.message_sanitization import (
    collapse_degenerate_repetition,
    collapse_degenerate_repetition_in_text_blocks,
    text_has_degenerate_repetition,
)


class _FakeAgent:
    model = "claude-opus-4-8"
    provider = "anthropic"
    stream_delta_callback = None
    _stream_callback = None
    reasoning_callback = None
    verbose_logging = False

    def _extract_reasoning(self, message):
        return getattr(message, "reasoning", None)

    def _strip_think_blocks(self, text):
        return text

    def _needs_thinking_reasoning_pad(self):
        return False


@pytest.mark.parametrize(
    "fenced",
    [
        "```text\n" + "call\n" * 8 + "```",
        "~~~text\n" + "call\n" * 8 + "~~~",
    ],
)
def test_fenced_repetition_is_never_detected_or_modified(fenced):
    assert text_has_degenerate_repetition(fenced) is False
    assert collapse_degenerate_repetition(fenced) == (fenced, 0)


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_only_text_outside_fences_is_collapsed_in_order(fence):
    protected = f"{fence}text\n" + "call\n" * 8 + fence
    text = "prefix " + "call " * 8 + "\n" + protected + "\nsuffix " + "court " * 8

    cleaned, runs = collapse_degenerate_repetition(text)

    assert runs == 2
    assert protected in cleaned
    assert cleaned.index("prefix call") < cleaned.index(protected) < cleaned.index("suffix court")


def test_unclosed_fence_protects_the_remainder():
    text = "prefix " + "call " * 8 + "\n```text\n" + "court\n" * 20

    cleaned, runs = collapse_degenerate_repetition(text)

    assert runs == 1
    assert cleaned.endswith("```text\n" + "court\n" * 20)


def test_structured_sanitizer_changes_only_text_blocks():
    metadata = {"vendor": {"opaque": "keep-me"}}
    blocks = [
        {"type": "thinking", "thinking": "call " * 8, "signature": "sig-1"},
        {"type": "reasoning", "text": "call " * 8, "metadata": metadata},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "terminal",
            "input": {"command": "call call call call call call call call"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": "call call call call call call call call",
        },
        {"type": "text", "text": "visible " + "call " * 8, "metadata": metadata},
    ]
    original = deepcopy(blocks)

    cleaned, runs = collapse_degenerate_repetition_in_text_blocks(blocks)

    assert runs == 1
    assert blocks == original, "the provider-owned source list must not be mutated"
    assert [block["type"] for block in cleaned] == [
        "thinking",
        "reasoning",
        "tool_use",
        "tool_result",
        "text",
    ]
    assert cleaned[:4] == original[:4]
    assert cleaned[4]["text"] == "visible call"
    assert cleaned[4]["metadata"] is metadata


def test_openai_storage_and_anthropic_replay_sanitize_the_same_text_blocks():
    fenced = "~~~text\n" + "call\n" * 8 + "~~~"
    ordered = [
        {"type": "thinking", "thinking": "call " * 8, "signature": "sig-1"},
        {
            "type": "text",
            "text": "visible " + "call " * 8,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "terminal",
            "input": {"command": "printf ok"},
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": fenced},
        {"type": "redacted_thinking", "data": "opaque-provider-data"},
    ]
    original_ordered = deepcopy(ordered)
    repeated_reasoning = "call " * 8
    assistant = SimpleNamespace(
        content="flat " + "call " * 8,
        tool_calls=None,
        reasoning=repeated_reasoning,
        reasoning_content=repeated_reasoning,
        reasoning_details=[
            {"type": "thinking", "thinking": repeated_reasoning, "signature": "sig-1"}
        ],
        anthropic_content_blocks=ordered,
    )

    stored = build_assistant_message(_FakeAgent(), assistant, "tool_calls")

    assert stored["role"] == "assistant"
    assert stored["content"] == "flat call"
    assert stored["reasoning"] == repeated_reasoning
    assert stored["reasoning_content"] == repeated_reasoning
    assert stored["reasoning_details"] == assistant.reasoning_details
    assert ordered == original_ordered, "storage sanitization must not mutate provider data"
    stored_blocks = stored["anthropic_content_blocks"]
    assert [block["type"] for block in stored_blocks] == [
        "thinking",
        "text",
        "tool_use",
        "text",
        "redacted_thinking",
    ]
    assert stored_blocks[0] == original_ordered[0]
    assert stored_blocks[1]["text"] == "visible call"
    assert stored_blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert stored_blocks[2] == original_ordered[2]
    assert stored_blocks[3]["text"] == fenced
    assert stored_blocks[4] == original_ordered[4]

    replayed = _convert_assistant_message(stored)["content"]

    assert [block["type"] for block in replayed] == [
        "thinking",
        "text",
        "tool_use",
        "text",
        "redacted_thinking",
    ]
    assert replayed[0] == original_ordered[0]
    assert replayed[1]["text"] == "visible call"
    assert replayed[1]["cache_control"] == {"type": "ephemeral"}
    assert replayed[2] == original_ordered[2]
    assert replayed[3]["text"] == fenced
    assert replayed[4] == original_ordered[4]
