"""Regression coverage for the dev sandbox's Node TLS trust boundary."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
STAGE2 = REPO_ROOT / "scripts" / "sandbox" / "stage2-run.sh"


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
    text = STAGE2.read_text(encoding="utf-8")

    assert (
        'git config --global url."git@github.com:NousResearch/hermes-agent.git".insteadOf '
        '"https://github.com/NousResearch/hermes-agent.git"'
    ) in text
