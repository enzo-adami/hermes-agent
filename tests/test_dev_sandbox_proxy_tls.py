"""Regression coverage for the dev-sandbox MITM TLS relay."""

from __future__ import annotations

import importlib.util
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
