"""Regression coverage for the dev sandbox's Node TLS trust boundary."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE1 = REPO_ROOT / "scripts" / "dev-sandbox.sh"
STAGE2 = REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh"
INSTALL_E2E = REPO_ROOT / "tests" / "install" / "install-update-e2e.sh"


def test_node_trusts_the_proxy_ca_for_https_requests() -> None:
    """npm must trust the certificate minted by the sandbox MITM proxy.

    The proxy terminates TLS with ``ca.pem`` before opening its own verified
    upstream connection. Pointing Node at ``real-ca.pem`` instead makes every
    npm HTTPS request reject the proxy certificate, while the proxy only logs
    the client's TLS EOF and npm's captured log can be empty on timeout.
    """
    text = STAGE2.read_text(encoding="utf-8")

    assert "--setenv NODE_EXTRA_CA_CERTS /work/certs/ca.pem" in text
    assert "--setenv NODE_EXTRA_CA_CERTS /work/certs/real-ca.pem" not in text


def test_https_fallback_for_the_canonical_repo_stays_on_fake_main() -> None:
    """An installer's HTTPS fallback must retry the sandbox-local repository.

    Older installers try SSH first and then fall back to the canonical HTTPS
    URL. Letting that fallback reach the real GitHub repository makes the E2E
    target depend on external TLS and can install a different tree than the
    fake ``main`` prepared by the sandbox.
    """
    stage1 = STAGE1.read_text(encoding="utf-8")
    stage2 = STAGE2.read_text(encoding="utf-8")

    assert "--setenv GIT_CONFIG_GLOBAL /work/gitconfig" in stage2
    assert 'url."git@github.com:NousResearch/hermes-agent.git".insteadOf' in stage1


def test_release_submodules_are_prefetched_outside_the_tls_proxy() -> None:
    """Legacy top-level submodules must clone from sandbox-local mirrors."""
    text = STAGE1.read_text(encoding="utf-8")

    assert "protocol.file.allow always" in text
    assert "submodule_mirror" in text
    assert 'url."file:///work/repos/submodules/' in text
    assert '"$submodule_commit:refs/heads/sandbox"' in text
    assert "symbolic-ref HEAD refs/heads/sandbox" in text
    assert 'submodule_keys="$(git config' in text
    assert "done < <(git config" not in text


def test_managed_python_tarball_is_prefetched_for_the_sandbox() -> None:
    """uv's managed Python download must not cross the flaky TLS proxy."""
    text = INSTALL_E2E.read_text(encoding="utf-8")

    assert "astral-sh/python-build-standalone/releases/latest" in text
    assert "install_only_stripped" in text
    assert 'github.com/astral-sh/python-build-standalone' in text
