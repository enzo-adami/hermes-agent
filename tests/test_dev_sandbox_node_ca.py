"""Regression coverage for the dev sandbox's Node TLS trust boundary."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE1 = REPO_ROOT / "scripts" / "dev-sandbox.sh"
STAGE2 = REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh"
INSTALL_E2E = REPO_ROOT / "tests" / "install" / "install-update-e2e.sh"
INSTALL_E2E_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "install-e2e-run.yml"
INSTALLER = REPO_ROOT / "scripts" / "install.sh"


def test_sandbox_clients_trust_proxy_and_public_certificate_authorities() -> None:
    """Clients must trust both fixture MITM and transparent upstream TLS.

    Fixture hosts terminate TLS at the proxy with ``ca.pem``. Non-fixture hosts
    are raw CONNECT tunnels, so their public certificates require the system CA
    bundle. The generated combined bundle is therefore the only correct trust
    boundary for curl, Python, Git, and Node.
    """
    stage1 = STAGE1.read_text(encoding="utf-8")
    text = STAGE2.read_text(encoding="utf-8")

    assert "combined-ca.pem" in stage1
    assert 'cp "$REAL_CA_CERT" "$SANDBOX_ROOT/root/certs/real-ca.pem"' in stage1
    assert 'if [ ! -f "$SANDBOX_ROOT/root/certs/real-ca.pem" ]; then' not in stage1
    for variable in (
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
    ):
        assert f"--setenv {variable} /work/certs/combined-ca.pem" in text
    assert "--setenv NODE_EXTRA_CA_CERTS /work/certs/ca.pem" not in text
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


def test_managed_python_api_auth_uses_the_runtime_token() -> None:
    """The release API request must not send a placeholder credential."""
    text = INSTALL_E2E.read_text(encoding="utf-8")

    assert 'Authorization: Bearer $GITHUB_TOKEN' in text
    assert 'Authorization: Bearer $GH_TOKEN' in text
    assert 'Authorization: Bearer ***' not in text


def test_install_e2e_workflow_exposes_the_github_token() -> None:
    """Each reusable matrix leg must receive an authenticated API token."""
    text = INSTALL_E2E_WORKFLOW.read_text(encoding="utf-8")

    assert 'GITHUB_TOKEN: ${{ github.token }}' in text


def test_node_dependency_failure_keeps_npm_diagnostics() -> None:
    """Captured npm failures must not be silenced before the log is printed."""
    text = INSTALLER.read_text(encoding="utf-8")

    assert 'npm install --silent \\\n                >"$npm_log"' not in text
    assert 'npm install \\\n                >"$npm_log"' in text


def test_sandbox_does_not_force_host_node_headers_on_managed_node() -> None:
    """A user install must let node-gyp resolve headers for its managed Node."""
    stage1 = STAGE1.read_text(encoding="utf-8")

    assert 'NODE_DIR="${DEV_SANDBOX_NODE_DIR:-}"' in stage1
    assert 'NODE_DIR="$(dirname "$(dirname "$(command -v node)")")"' not in stage1
