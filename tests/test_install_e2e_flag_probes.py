"""Execute the E2E shell flag probes under pipefail with large inputs.

Load only the function definitions: the script's main body requires a Linux
sandbox and performs installations. Assertions target exit status/output, not
source spelling. Git reads a real temporary repository for installer probes.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "install" / "install-update-e2e.sh"
pytestmark = pytest.mark.skipif(
    not shutil.which("bash") or not shutil.which("git"), reason="requires bash and git"
)


def _function(name):
    match = re.search(rf"^{name}\(\) \{{.*?^\}}", SCRIPT.read_text(), re.M | re.S)
    if match is None:
        raise ValueError(f"Shell function {name} not found")
    return match.group()


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )


@pytest.mark.parametrize("present", [True, False])
def test_installer_probe_handles_output_larger_than_pipe(tmp_path, present):
    _git(tmp_path, "init")
    (tmp_path / "scripts").mkdir()
    flag = "--skip-browser" if present else "--different-option"
    (tmp_path / "scripts" / "install.sh").write_text(flag + "\n" + "# filler\n" * 100000)
    _git(tmp_path, "add", "scripts/install.sh")
    _git(tmp_path, "commit", "-m", "fixture")
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + _function("installer_supports")
         + '\ninstaller_supports HEAD --skip-browser\n'],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == (0 if present else 1), result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


# in_sandbox is replaced with a file reader; no Hermes command is executed.
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("present,producer_status", [(True, 0), (False, 0), (True, 7)])
def test_update_probe_drains_help_and_preserves_producer_failure(tmp_path, present, producer_status):
    flag = "--no-restart" if present else "--different-option"
    (tmp_path / "help.txt").write_text(flag + "\n" + "filler\n" * 100000)
    result = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n"
         + f'in_sandbox() {{ cat help.txt; return {producer_status}; }}\n'
         + _function("update_supports") + '\nupdate_supports --no-restart\n'],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    expected = producer_status or (0 if present else 1)
    assert result.returncode == expected, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
