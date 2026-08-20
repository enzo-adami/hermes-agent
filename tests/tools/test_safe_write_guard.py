"""End-to-end contract for the safe-write (anti-blanking) guard.

The failure being prevented (2026-06-09): ``write_file`` replaced STATUS.md and
a full research report with a fraction of their content. The write reported
success; nothing in the runtime noticed.

A first implementation of this guard lived in ``agent/tool_guardrails.py`` and
was reverted on 2026-08-04 with a documented verdict (thread
``hermes-runtime-guards-20260804``): *"besoin réel reconnu, implémentation
générique non admissible"*. Two of the four defects are what this file exists
to prevent from coming back:

  * it measured the file with ``os.path.getsize(expanduser(raw_path))``
    **before** task/backend resolution — the wrong file whenever the path is
    relative to a task cwd, or the backend is a container or remote shell.
    Covered by :func:`test_size_is_measured_after_path_resolution`.
  * it was confirmed by re-issuing the identical call, and was never proven at
    the tool boundary. Every test here goes through ``write_file_tool`` or the
    tool-dispatch handler and asserts what is ON DISK afterwards, which is the
    only thing the incident was about.

Fail-open is a contract, not an omission: a guard that cannot measure a size
must let the write through. See :func:`test_unmeasurable_path_fails_open`.
"""
from __future__ import annotations

import json

import pytest

import tools.file_tools as ft
import tools.terminal_tool as terminal_tool


BIG = "x" * 4000          # comfortably above safe_write_min_bytes (200)
TINY = "oops"             # 4 bytes — 0.1% of BIG


@pytest.fixture(autouse=True)
def _clean_file_ops(monkeypatch):
    """Each test gets a fresh backend cache and no inherited session cwd."""
    monkeypatch.setattr(ft, "_file_ops_cache", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    yield
    monkeypatch.setattr(ft, "_file_ops_cache", {})


def _write(path, content, **kw):
    return json.loads(ft.write_file_tool(str(path), content, **kw))


# ---------------------------------------------------------------------------
# The data-loss contract
# ---------------------------------------------------------------------------


def test_blanking_an_existing_file_is_refused_and_disk_is_untouched(tmp_path):
    """THE contract. Not "an error is returned" — the bytes survive."""
    target = tmp_path / "STATUS.md"
    target.write_text(BIG)

    result = _write(target, TINY)

    assert result.get("error"), f"blanking write should be refused: {result}"
    assert "Refusing to overwrite" in result["error"]
    assert "allow_shrink" in result["error"], "refusal must name its override"
    assert target.read_text() == BIG, "the file was modified despite the refusal"


def test_allow_shrink_lets_the_write_land(tmp_path):
    target = tmp_path / "STATUS.md"
    target.write_text(BIG)

    result = _write(target, TINY, allow_shrink=True)

    assert not result.get("error"), f"explicit shrink should proceed: {result}"
    assert target.read_text() == TINY


def test_guard_reaches_the_tool_dispatch_boundary(tmp_path):
    """The wire from tool-call args to the guard, both directions.

    A guard that only works when called from Python is not the guard the
    incident needed — the incident came through a tool call.
    """
    target = tmp_path / "report.md"
    target.write_text(BIG)

    refused = json.loads(ft._handle_write_file(
        {"path": str(target), "content": TINY}, task_id="default"))
    assert refused.get("error") and "Refusing to overwrite" in refused["error"]
    assert target.read_text() == BIG

    allowed = json.loads(ft._handle_write_file(
        {"path": str(target), "content": TINY, "allow_shrink": True},
        task_id="default"))
    assert not allowed.get("error"), allowed
    assert target.read_text() == TINY


def test_allow_shrink_is_declared_in_the_tool_schema():
    """The model cannot use an override it is never told about."""
    props = ft.WRITE_FILE_SCHEMA["parameters"]["properties"]
    assert "allow_shrink" in props
    assert props["allow_shrink"]["type"] == "boolean"
    assert props["allow_shrink"]["default"] is False


# ---------------------------------------------------------------------------
# The defect that killed the 2026-06 version
# ---------------------------------------------------------------------------


def test_size_is_measured_after_path_resolution(tmp_path, monkeypatch):
    """A relative path must be measured where it will be WRITTEN.

    Two checkouts hold the same relative filename. The terminal is in the
    workspace (big file); the agent process cwd is the decoy (tiny file). The
    rejected version did ``os.path.getsize("notes.md")``, which resolves
    against the PROCESS cwd — it would have measured the decoy's 4 bytes,
    found no shrink, and blanked the workspace file. Measuring through the
    backend after resolution sees the 4000 bytes that are actually at risk.
    """
    workspace = tmp_path / "workspace"
    decoy = tmp_path / "decoy"
    workspace.mkdir()
    decoy.mkdir()
    (workspace / "notes.md").write_text(BIG)
    (decoy / "notes.md").write_text(TINY)

    monkeypatch.chdir(decoy)
    monkeypatch.setenv("TERMINAL_CWD", str(workspace))

    result = _write("notes.md", TINY)

    assert result.get("error"), (
        "relative path measured against the process cwd — the exact defect "
        f"the 2026-08-05 verdict named: {result}"
    )
    assert (workspace / "notes.md").read_text() == BIG
    assert (decoy / "notes.md").read_text() == TINY


def test_backend_that_cannot_measure_does_not_block(tmp_path, monkeypatch):
    """A backend without ``file_size`` (older or exotic) must not wedge writes."""
    target = tmp_path / "STATUS.md"
    target.write_text(BIG)

    real_get_file_ops = ft._get_file_ops

    class _NoSizeOps:
        def __init__(self, inner):
            self._inner = inner
        def __getattr__(self, name):
            if name == "file_size":
                raise AttributeError("backend has no size probe")
            return getattr(self._inner, name)

    monkeypatch.setattr(
        ft, "_get_file_ops",
        lambda task_id="default": _NoSizeOps(real_get_file_ops(task_id)))

    result = _write(target, TINY)

    assert not result.get("error"), f"must fail open, not refuse: {result}"
    assert target.read_text() == TINY


def test_unmeasurable_path_fails_open(tmp_path):
    """A directory is not a regular file — the probe returns unknown.

    The guard must decline to judge rather than invent a size. The write then
    fails on its own merits (writing to a directory), but NOT with our refusal.
    """
    target = tmp_path / "a_directory"
    target.mkdir()

    result = _write(target, TINY)

    assert "Refusing to overwrite" not in (result.get("error") or "")


# ---------------------------------------------------------------------------
# No false positives — the half that makes a guard livable
# ---------------------------------------------------------------------------


def test_new_file_is_allowed(tmp_path):
    target = tmp_path / "brand-new.md"
    result = _write(target, TINY)
    assert not result.get("error"), result
    assert target.read_text() == TINY


def test_small_existing_file_is_allowed(tmp_path):
    """Below safe_write_min_bytes there is nothing worth protecting."""
    target = tmp_path / "small.md"
    target.write_text("a" * 50)
    result = _write(target, "b")
    assert not result.get("error"), result
    assert target.read_text() == "b"


def test_moderate_shrink_is_allowed(tmp_path):
    """Editing a file down to 60% of its size is normal work, not blanking."""
    target = tmp_path / "notes.md"
    target.write_text(BIG)
    trimmed = "y" * 2400  # 60% — above the 0.5 ratio
    result = _write(target, trimmed)
    assert not result.get("error"), result
    assert target.read_text() == trimmed


def test_growth_is_allowed(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text(BIG)
    grown = BIG + "more"
    result = _write(target, grown)
    assert not result.get("error"), result
    assert target.read_text() == grown


def test_guard_can_be_disabled_by_config(tmp_path, monkeypatch):
    monkeypatch.setattr(ft, "_safe_write_config", lambda: (False, 200, 0.5))
    target = tmp_path / "STATUS.md"
    target.write_text(BIG)
    result = _write(target, TINY)
    assert not result.get("error"), result
    assert target.read_text() == TINY


def test_config_read_failure_keeps_the_guard_armed(monkeypatch):
    """Data-loss prevention fails SAFE, unlike the fail-open size probe."""
    import builtins
    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name == "hermes_cli.config":
            raise RuntimeError("config unavailable")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    enabled, min_bytes, ratio = ft._safe_write_config()
    assert enabled is True
    assert min_bytes == 200
    assert ratio == 0.5
