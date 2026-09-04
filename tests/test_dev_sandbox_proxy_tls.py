"""Regression coverage for the dev-sandbox MITM TLS relay."""

from __future__ import annotations

import importlib.util
import socket
import ssl
import sys
import threading
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROXY_PATH = REPO_ROOT / "scripts" / "sandbox" / "proxy.py"


def _load_proxy(monkeypatch, tmp_path):
    root = tmp_path / "fixtures"
    certs = tmp_path / "certs"
    root.mkdir()
    certs.mkdir()
    real_ca = tmp_path / "real-ca.pem"
    real_ca.write_text("test-only", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [str(PROXY_PATH), str(root), str(certs), str(real_ca)],
    )
    spec = importlib.util.spec_from_file_location("sandbox_proxy_under_test", PROXY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _TracingTLSSocket:
    def __init__(
        self,
        *,
        upstream: bool = False,
        max_send: int | None = None,
        send_want_read_once: bool = False,
        recv_want_write_once: bool = False,
        report_pending: bool = False,
    ):
        self.upstream = upstream
        self.max_send = max_send
        self.send_want_read_once = send_want_read_once
        self.recv_want_write_once = recv_want_write_once
        self.report_pending = report_pending
        self.calls: list[tuple[str, int]] = []
        self.sent = bytearray()
        self._upstream_reads = [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok", b""]
        self.initial_request_sent = threading.Event()

    def _record(self, operation: str) -> None:
        self.calls.append((operation, threading.get_ident()))

    def setblocking(self, enabled: bool) -> None:
        self._record("setblocking")

    def pending(self) -> int:
        self._record("pending")
        return int(
            self.report_pending
            and self.upstream
            and self.initial_request_sent.is_set()
            and bool(self._upstream_reads)
        )

    def send(self, data: bytes) -> int:
        self._record("send")
        if self.send_want_read_once:
            self.send_want_read_once = False
            raise ssl.SSLWantReadError(ssl.SSL_ERROR_WANT_READ, "read required")
        sent = len(data) if self.max_send is None else min(len(data), self.max_send)
        self.sent.extend(data[:sent])
        if self.upstream:
            self.initial_request_sent.set()
        return sent

    def sendall(self, data: bytes) -> None:
        self._record("sendall")
        self.sent.extend(data)
        if self.upstream:
            self.initial_request_sent.set()

    def recv(self, size: int) -> bytes:
        self._record("recv")
        if not self.upstream:
            raise ssl.SSLWantReadError(ssl.SSL_ERROR_WANT_READ, "not ready")
        assert self.initial_request_sent.wait(timeout=1), "request was never relayed"
        if self.recv_want_write_once:
            self.recv_want_write_once = False
            raise ssl.SSLWantWriteError(ssl.SSL_ERROR_WANT_WRITE, "write required")
        return self._upstream_reads.pop(0)

    def shutdown(self, how: int) -> None:
        self._record("shutdown")

    def close(self) -> None:
        self._record("close")


def _run_forward(proxy, monkeypatch, client, upstream, *, ready_select=None) -> None:
    class _Context:
        def wrap_socket(self, raw, *, server_hostname):
            return upstream

    monkeypatch.setattr(proxy.ssl, "create_default_context", lambda **kwargs: _Context())
    monkeypatch.setattr(proxy.socket, "create_connection", lambda *args, **kwargs: object())
    if ready_select is None:
        ready_select = lambda readers, writers, errors, timeout: (readers, writers, [])
    monkeypatch.setattr(
        proxy,
        "select",
        types.SimpleNamespace(select=ready_select),
        raising=False,
    )
    proxy.forward_https(
        client,
        "example.invalid",
        443,
        b"GET / HTTP/1.1\r\nHost: example.invalid\r\nProxy-Connection: keep-alive\r\n\r\n",
    )


def test_forwarded_tls_socket_io_stays_on_one_thread(monkeypatch, tmp_path) -> None:
    """One SSLSocket must never be read and written from different threads.

    CPython #151508 demonstrates that concurrent ``recv`` / ``send`` calls on
    one SSLSocket can corrupt OpenSSL state and segfault the interpreter. The
    sandbox proxy used exactly that pattern under install traffic.
    """
    proxy = _load_proxy(monkeypatch, tmp_path)
    client = _TracingTLSSocket(max_send=3)
    upstream = _TracingTLSSocket(upstream=True)

    main_thread = threading.get_ident()
    _run_forward(proxy, monkeypatch, client, upstream)

    upstream_threads = {thread_id for _, thread_id in upstream.calls}
    client_threads = {thread_id for _, thread_id in client.calls}
    assert upstream_threads == {main_thread}
    assert client_threads == {main_thread}
    assert b"Proxy-Connection:" not in upstream.sent
    assert client.sent.endswith(b"ok")


def test_tls_relay_retries_cross_direction_readiness(monkeypatch, tmp_path) -> None:
    """WANT_READ from send and WANT_WRITE from recv resume on the right event."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    client = _TracingTLSSocket()
    upstream = _TracingTLSSocket(
        upstream=True,
        send_want_read_once=True,
        recv_want_write_once=True,
    )

    _run_forward(proxy, monkeypatch, client, upstream)

    assert [name for name, _ in upstream.calls].count("send") >= 2
    assert [name for name, _ in upstream.calls].count("recv") >= 3
    assert client.sent.endswith(b"ok")


def test_tls_relay_drains_ssl_pending_without_fd_readiness(monkeypatch, tmp_path) -> None:
    """Decrypted bytes reported by pending() bypass a non-readable kernel fd."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    client = _TracingTLSSocket()
    upstream = _TracingTLSSocket(upstream=True, report_pending=True)

    def writable_only(readers, writers, errors, timeout):
        return [], writers, []

    _run_forward(
        proxy,
        monkeypatch,
        client,
        upstream,
        ready_select=writable_only,
    )

    assert client.sent.endswith(b"ok")


def test_connect_bypasses_tls_for_hosts_without_fixtures(monkeypatch, tmp_path) -> None:
    """High-volume public hosts must remain opaque raw CONNECT tunnels."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    class _Upstream:
        def close(self):
            observed["closed"] = True

    upstream = _Upstream()
    observed = {}

    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        lambda address, timeout: observed.update(address=address) or upstream,
    )
    monkeypatch.setattr(
        proxy,
        "relay_raw_duplex",
        lambda left, right: observed.update(relay=(left, right)),
        raising=False,
    )

    class _Client:
        def sendall(self, data):
            observed["response"] = data

    client = _Client()
    proxy.handle_connect(client, "registry.npmjs.org:443")

    assert observed["address"] == ("registry.npmjs.org", 443)
    assert observed["relay"] == (client, upstream)
    assert observed["response"].startswith(b"HTTP/1.1 200")
    assert observed["closed"] is True


def test_connect_keeps_mitm_for_fixture_hosts(monkeypatch, tmp_path) -> None:
    """The canonical installer fixture must still terminate TLS at the proxy."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    (proxy.ROOT / "hermes-agent.nousresearch.com").mkdir()
    observed = {}

    monkeypatch.setattr(
        proxy,
        "intercept_connect",
        lambda conn, host, port: observed.update(args=(conn, host, port)),
        raising=False,
    )
    client = object()
    proxy.handle_connect(client, "hermes-agent.nousresearch.com:443")

    assert observed["args"] == (client, "hermes-agent.nousresearch.com", 443)


def test_connect_normalizes_dns_case_before_fixture_routing(monkeypatch, tmp_path) -> None:
    """Mixed-case DNS names must not bypass a lowercase fixture directory."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    (proxy.ROOT / "hermes-agent.nousresearch.com").mkdir()
    observed = {}
    monkeypatch.setattr(
        proxy,
        "intercept_connect",
        lambda conn, host, port: observed.update(args=(conn, host, port)),
    )

    client = object()
    proxy.handle_connect(client, "Hermes-Agent.NousResearch.COM:443")

    assert observed["args"] == (client, "hermes-agent.nousresearch.com", 443)


def test_connect_normalizes_idna_and_root_dot_before_fixture_routing(
    monkeypatch, tmp_path
) -> None:
    """Valid internationalized DNS authorities use canonical fixture names."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    (proxy.ROOT / "xn--bcher-kva.example").mkdir()
    observed = {}
    monkeypatch.setattr(
        proxy,
        "intercept_connect",
        lambda conn, host, port: observed.update(args=(conn, host, port)),
    )

    client = object()
    proxy.handle_connect(client, "BÜCHER.Example.:443")

    assert observed["args"] == (client, "xn--bcher-kva.example", 443)


def test_connect_rejects_malformed_authorities_before_routing(monkeypatch, tmp_path) -> None:
    """Malformed CONNECT targets fail closed instead of reaching fixtures or DNS."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    routed = []
    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        lambda *args, **kwargs: routed.append((args, kwargs)),
    )
    monkeypatch.setattr(
        proxy,
        "intercept_connect",
        lambda *args: routed.append(args),
    )

    class _Client:
        def __init__(self):
            self.sent = bytearray()

        def sendall(self, data):
            self.sent.extend(data)

    malformed = (
        "",
        ":443",
        "registry.npmjs.org",
        "registry.npmjs.org:0",
        "registry.npmjs.org:65536",
        "registry.npmjs.org:not-a-port",
        "user@registry.npmjs.org:443",
        "registry.npmjs.org/path:443",
        "../registry.npmjs.org:443",
        "registry.npmjs.org:\n443",
        "[::1",
    )
    for target in malformed:
        client = _Client()
        proxy.handle_connect(client, target)
        assert bytes(client.sent).startswith(b"HTTP/1.1 400 Bad Request"), target

    assert routed == []


def test_connect_reports_upstream_connection_failure(monkeypatch, tmp_path) -> None:
    """A valid authority with an unreachable upstream receives an explicit 502."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("unreachable")),
    )

    class _Client:
        sent = bytearray()

        def sendall(self, data):
            self.sent.extend(data)

    client = _Client()
    proxy.handle_connect(client, "registry.npmjs.org:443")

    assert bytes(client.sent).startswith(b"HTTP/1.1 502 Bad Gateway")


def test_fixture_lookup_rejects_direct_and_intermediate_symlink_escape(
    monkeypatch, tmp_path
) -> None:
    """Fixture paths must remain below the canonical per-host directory."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    host_root = proxy.ROOT / "example.com"
    host_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (host_root / "direct.txt").symlink_to(outside / "secret.txt")
    (host_root / "nested").symlink_to(outside, target_is_directory=True)

    assert proxy.file_for("example.com", "/direct.txt") is None
    assert proxy.file_for("example.com", "/nested/secret.txt") is None


def test_fixture_lookup_rejects_host_path_and_host_symlink_escape(
    monkeypatch, tmp_path
) -> None:
    """An untrusted Host cannot select or alias a directory outside fixture ROOT."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    outside = proxy.ROOT.parent / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (proxy.ROOT / "alias.example").symlink_to(outside, target_is_directory=True)

    assert proxy.file_for("../outside", "/secret.txt") is None
    assert proxy.file_for("alias.example", "/secret.txt") is None


def test_fixture_lookup_preserves_contained_files_and_indexes(monkeypatch, tmp_path) -> None:
    """Containment checks keep normal files, directories, and safe aliases working."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    host_root = proxy.ROOT / "example.com"
    nested = host_root / "nested"
    nested.mkdir(parents=True)
    payload = nested / "index.html"
    payload.write_text("fixture", encoding="utf-8")
    (host_root / "alias.html").symlink_to(payload)

    assert proxy.file_for("example.com", "/nested") == payload.resolve()
    assert proxy.file_for("example.com", "/alias.html") == payload.resolve()


def test_non_fixture_connect_relays_real_full_duplex_bytes(monkeypatch, tmp_path) -> None:
    """Transparent CONNECT preserves large payloads and propagates half-closes."""
    proxy = _load_proxy(monkeypatch, tmp_path)
    proxy_side, client = socket.socketpair()
    upstream_for_proxy, upstream = socket.socketpair()
    monkeypatch.setattr(
        proxy.socket,
        "create_connection",
        lambda address, timeout: upstream_for_proxy,
    )
    monkeypatch.setattr(
        proxy,
        "intercept_connect",
        lambda *args: (_ for _ in ()).throw(AssertionError("unexpected MITM")),
    )

    worker = threading.Thread(
        target=proxy.handle_connect,
        args=(proxy_side, "registry.npmjs.org:443"),
    )
    worker.start()
    response = client.recv(4096)
    assert response == b"HTTP/1.1 200 Connection Established\r\n\r\n"

    client_payload = b"client" * (proxy.MAX_REQUEST_BYTES + 1)
    upstream_payload = b"upstream" * (proxy.MAX_REQUEST_BYTES + 1)
    received = {}

    def send_then_half_close(sock, payload):
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)

    def drain(sock, key):
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
        received[key] = b"".join(chunks)

    threads = [
        threading.Thread(target=send_then_half_close, args=(client, client_payload)),
        threading.Thread(target=send_then_half_close, args=(upstream, upstream_payload)),
        threading.Thread(target=drain, args=(client, "client")),
        threading.Thread(target=drain, args=(upstream, "upstream")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    worker.join(timeout=10)
    assert not worker.is_alive()

    assert received["upstream"] == client_payload
    assert received["client"] == upstream_payload
    client.close()
    upstream.close()
    proxy_side.close()
