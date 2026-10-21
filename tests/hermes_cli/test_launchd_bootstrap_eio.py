"""``launchctl bootstrap`` answers EIO 5 for a label that is still loaded, whether its job runs or not.

Regression for #138612: an update interrupted while a launchd gateway drained its turns resumed it at
exit; the bootstrap of the still-loaded job failed EIO and the stale-registration recovery booted it
out, so launchd SIGTERMed the gateway and cut the turn it was finishing. The recovery may clear a
registration launchd reports stopped or gone; a running job is left alone, and a state it cannot
read is no licence to stop one.
"""

from __future__ import annotations

import subprocess

import pytest

import hermes_cli.gateway as gateway_cli

_DOMAIN, _LABEL, _PLIST = "gui/501", "ai.hermes.gateway", "/tmp/ai.hermes.gateway.plist"
_RUNNING = f"{_DOMAIN}/{_LABEL} = {{\n\tstate = running\n\tpid = 4242\n}}\n"
_STOPPED = f"{_DOMAIN}/{_LABEL} = {{\n\tstate = not running\n\tlast exit code = 75\n}}\n"


def _fake_launchctl(monkeypatch, print_rc: int, print_out: str) -> list[str]:
    """``launchctl`` whose first bootstrap fails EIO 5 (the label is still loaded) and whose ``print``
    answers *print_rc* / *print_out*; returns every verb it was asked."""
    verbs: list[str] = []

    def run(cmd, check=False, **kwargs):
        verbs.append(cmd[1])
        rc = {"print": print_rc, "bootstrap": 5 if verbs.count("bootstrap") == 1 else 0}.get(cmd[1], 0)
        if check and rc:
            raise subprocess.CalledProcessError(rc, cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=print_out if cmd[1] == "print" else "", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return verbs


@pytest.mark.parametrize(("print_rc", "print_out", "expected"), [
    (0, _RUNNING, "left"),       # loaded and running: the gateway and its turns stay
    (0, _STOPPED, "cleared"),    # loaded, not running: the stale registration is cleared
    (113, "", "cleared"),        # gone since the bootstrap: nothing left to protect
    (1, "", "refused"),          # state unreadable: the caller keeps its recovery debt
], ids=["running", "loaded-not-running", "gone", "unreadable"])
def test_an_eio_bootstrap_boots_out_only_a_job_launchd_reports_stopped(monkeypatch, print_rc, print_out, expected):
    verbs = _fake_launchctl(monkeypatch, print_rc, print_out)
    try:
        gateway_cli._launchctl_bootstrap(_DOMAIN, _PLIST, _LABEL)
        outcome = "cleared" if "bootout" in verbs else "left"
    except RuntimeError:
        outcome = "refused"
    assert outcome == expected, verbs
    if outcome == "cleared":
        assert verbs.count("bootstrap") == 2, f"the cleared label was not bootstrapped again: {verbs}"
    else:
        assert "bootout" not in verbs, f"a job launchd did not report stopped was booted out: {verbs}"
