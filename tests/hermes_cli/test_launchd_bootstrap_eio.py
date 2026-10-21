"""``launchctl bootstrap`` answers EIO 5 for a label that is still loaded, whether its job runs or not.

Regression for #138612: an update interrupted while a launchd gateway drained its turns resumed it at
exit; the bootstrap of the still-loaded job failed EIO and the stale-registration recovery booted it
out, so launchd SIGTERMed the gateway and cut the turn it was finishing. The resume now leaves a job
launchd reports running, clears one it reports otherwise or gone, and keeps its debt on a state it
cannot read. Every other caller keeps the clear-and-retry: ``install --force`` must load its new
definition, and a reload that races a bootout finds the exiting process still listed (``state =
SIGTERMed``), which is no proof the label came back.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import time

import pytest

import hermes_cli.gateway as gateway_cli
from tests.waiting import wait_until

_DOMAIN, _LABEL, _PLIST = "gui/501", "ai.hermes.gateway", "/tmp/ai.hermes.gateway.plist"
_RUNNING = f"{_DOMAIN}/{_LABEL} = {{\n\tstate = running\n\tpid = 4242\n}}\n"
_STOPPING = f"{_DOMAIN}/{_LABEL} = {{\n\tstate = SIGTERMed\n\tpid = 4242\n}}\n"
_STOPPED = f"{_DOMAIN}/{_LABEL} = {{\n\tstate = not running\n\tlast exit code = 75\n}}\n"
_NESTED_ONLY = f"{_DOMAIN}/{_LABEL} = {{\n\tpid = 4242\n\tendpoints = {{\n\t\tstate = active\n\t}}\n}}\n"


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
    (0, _RUNNING, "left"),         # loaded and running: the gateway and its turns stay
    (0, _STOPPING, "cleared"),     # a bootout is removing it: its old PID is still listed, but it is not running
    (0, _STOPPED, "cleared"),      # loaded, not running: the stale registration is cleared
    (113, "", "cleared"),          # gone since the bootstrap: nothing left to protect
    (0, _NESTED_ONLY, "refused"),  # no state of its own (an endpoint's is not the job's): the debt stays
    (1, "", "refused"),            # state unreadable: the resume keeps its restart debt
], ids=["running", "stopping", "loaded-not-running", "gone", "nested-state-only", "unreadable"])
def test_the_resume_boots_out_only_a_job_launchd_reports_stopped(monkeypatch, print_rc, print_out, expected):
    verbs = _fake_launchctl(monkeypatch, print_rc, print_out)
    try:
        gateway_cli._launchctl_bootstrap(_DOMAIN, _PLIST, _LABEL, leave_running=True)
        outcome = "cleared" if "bootout" in verbs else "left"
    except subprocess.CalledProcessError as exc:
        assert (exc.returncode, exc.cmd[1]) == (5, "bootstrap"), verbs  # the EIO its callers already handle
        outcome = "refused"
    assert outcome == expected, verbs
    if outcome == "cleared":
        assert verbs.count("bootstrap") == 2, f"the cleared label was not bootstrapped again: {verbs}"
    else:
        assert "bootout" not in verbs, f"a job launchd did not report stopped was booted out: {verbs}"


def test_install_start_and_reload_still_clear_an_eio_registration(monkeypatch):
    """``hermes gateway install --force`` over a running gateway must load its new definition: every
    caller but the update resume keeps the clear-and-retry, without asking launchd first."""
    verbs = _fake_launchctl(monkeypatch, 0, _RUNNING)
    gateway_cli._launchctl_bootstrap(_DOMAIN, _PLIST, _LABEL)
    assert verbs == ["bootstrap", "bootout", "bootstrap"]


@pytest.mark.platforms("macos")
def test_a_bootstrap_during_a_bootout_loads_the_job_again_once_launchd_has_removed_it(tmp_path):
    """``launchctl bootout`` returns while launchd still lists the exiting process (``state =
    SIGTERMed``) and bootstrap answers EIO. Taking that process for the reloaded job reported the
    label registered, and it vanished when the process exited: an unloaded service, no KeepAlive."""
    label = f"ai.hermes.p2probe-eio-{os.getpid()}"
    plist, trapped = tmp_path / f"{label}.plist", tmp_path / "trapped"
    # Like a draining gateway, it outlives SIGTERM by a few seconds, once its trap is set.
    script = f"trap 'sleep 3; exit 0' TERM; : > {shlex.quote(str(trapped))}; while :; do sleep 1; done"
    plist.write_bytes(plistlib.dumps({"Label": label, "RunAtLoad": True,
                                      "ProgramArguments": ["/bin/sh", "-c", script]}))
    domain = f"gui/{os.getuid()}"  # windows-footgun: ok — macOS-only test (platforms("macos"))
    if subprocess.run(["launchctl", "print", domain], capture_output=True, check=False).returncode:
        domain = f"user/{os.getuid()}"  # windows-footgun: ok — macOS-only test (platforms("macos"))

    def pid() -> int | None:
        return gateway_cli._launchd_print_service_pid(domain, label)[1]

    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=30)
    try:
        wait_until(trapped.exists, timeout=15, what="the probe job to trap SIGTERM")
        first = pid()
        assert first, "premise: launchd never started the probe job"
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=False, timeout=30)
        if pid() != first:
            pytest.skip("launchd removed the job before bootout returned: no removal to overlap")
        assert gateway_cli._retry_launchctl_bootstrap_until_registered(
            domain, plist, label, deadline=time.monotonic() + 30), "the label was never loaded again"
        wait_until(lambda: pid() not in (None, first), timeout=15,
                   what="the label to run a new process once the old one has exited")
    finally:
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, check=False)
