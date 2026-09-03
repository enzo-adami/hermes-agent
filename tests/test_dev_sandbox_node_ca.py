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
