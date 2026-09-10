"""``git_fetch_retry`` must absorb transient remote failures and nothing else.

The install / update E2E matrix lost three legs to GitHub HTTP 429 replies on
the anonymous upstream fetch (run 34128019604). The helper retries those with
backoff, but a ref that does not exist must still fail on the first attempt so
the sandbox's own fallback (fetch main, resolve locally) keeps working.

A ``git`` shim ahead of the real binary fails the first N fetches with a
scripted message and then hands off to the real git, so every assertion is on
observable behaviour: exit status, attempt count, and what reached stderr.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "scripts" / "sandbox" / "git-fetch-retry.sh"

pytestmark = pytest.mark.skipif(
    not shutil.which("bash") or not shutil.which("git"), reason="requires bash and git"
)

RATE_LIMITED = (
    "remote: This request was rate-limited due to too many requests.\n"
    "fatal: unable to access 'https://github.com/NousResearch/hermes-agent.git/': "
    "The requested URL returned error: 429"
)
UNKNOWN_REF = "fatal: couldn't find remote ref refs/heads/does-not-exist"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, capture_output=True, text=True, check=True,
    )


def _make_remote(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-q", "-b", "main")
    (remote / "README").write_text("fixture\n")
    _git(remote, "add", "README")
    _git(remote, "commit", "-q", "-m", "fixture")
    return remote


def _make_shim(tmp_path, failures, message):
    """A ``git`` that fails the first *failures* fetches, then defers to real git."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    counter = tmp_path / "attempts"
    counter.write_text("0")
    real_git = shutil.which("git")
    (shim_dir / "git").write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "# Only fetches are scripted; -C <dir> precedes the subcommand.\n"
        'is_fetch=0; for a in "$@"; do [ "$a" = fetch ] && is_fetch=1; done\n'
        "if [ $is_fetch -eq 1 ]; then\n"
        f'  n=$(cat "{counter}"); n=$((n + 1)); echo "$n" > "{counter}"\n'
        f"  if [ \"$n\" -le {failures} ]; then\n"
        f"    printf '%s\\n' {_sh_quote(message)} >&2\n"
        "    exit 128\n"
        "  fi\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    (shim_dir / "git").chmod(0o755)
    return shim_dir, counter


def _sh_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def _run(tmp_path, shim_dir, ref, retries):
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-q")
    env = dict(
        os.environ,
        PATH=f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
        HERMES_SANDBOX_FETCH_RETRIES=str(retries),
        HERMES_SANDBOX_FETCH_RETRY_DELAY="0",
    )
    remote = tmp_path / "remote"
    return subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail\n. "{HELPER}"\n'
         f'git_fetch_retry "{clone}" "file://{remote}" "{ref}"\n'],
        capture_output=True, text=True, timeout=60, env=env,
    )


def test_transient_failures_are_retried_until_the_fetch_succeeds(tmp_path):
    _make_remote(tmp_path)
    shim_dir, counter = _make_shim(tmp_path, failures=2, message=RATE_LIMITED)

    result = _run(tmp_path, shim_dir, "refs/heads/main", retries=5)

    assert result.returncode == 0, result.stderr
    assert counter.read_text().strip() == "3"
    assert result.stderr.count("retrying in 0s") == 2
    assert "returned error: 429" in result.stderr


def test_permanent_failure_is_not_retried(tmp_path):
    _make_remote(tmp_path)
    shim_dir, counter = _make_shim(tmp_path, failures=99, message=UNKNOWN_REF)

    result = _run(tmp_path, shim_dir, "refs/heads/does-not-exist", retries=5)

    assert result.returncode == 1
    assert counter.read_text().strip() == "1"
    assert "retrying" not in result.stderr
    assert UNKNOWN_REF in result.stderr


def test_transient_failure_gives_up_after_the_attempt_budget(tmp_path):
    _make_remote(tmp_path)
    shim_dir, counter = _make_shim(tmp_path, failures=99, message=RATE_LIMITED)

    result = _run(tmp_path, shim_dir, "refs/heads/main", retries=3)

    assert result.returncode == 1
    assert counter.read_text().strip() == "3"
    assert result.stderr.count("retrying in") == 2
    assert "still failing after 3 attempts" in result.stderr


def test_real_missing_ref_without_shim_fails_fast(tmp_path):
    """No shim: real git's unknown-ref error must not match the transient list."""
    _make_remote(tmp_path)
    empty_shim = tmp_path / "noshim"
    empty_shim.mkdir()

    result = _run(tmp_path, empty_shim, "refs/heads/does-not-exist", retries=5)

    assert result.returncode == 1
    assert "retrying" not in result.stderr
