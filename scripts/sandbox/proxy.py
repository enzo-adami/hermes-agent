"""MITM proxy backing the dev sandbox's fake Internet.

Listens on 127.0.0.1:8080 and is pointed at by http_proxy/https_proxy inside
the sandbox. For each request it either serves a fixture from the filesystem or
forwards to the real host:

* ``<root>/<host>/<path>`` exists -> serve it. This is how the sandbox answers
  the canonical install URL with the installer under test, so the payload can
  run the true ``curl -fsSL https://…/install.sh | bash`` one-liner.
* otherwise -> forward upstream, verifying against the real CA bundle. The
  sandbox is isolated from the *host*, not from the internet: a real install
  still has to reach PyPI and npm.

HTTPS is intercepted by minting a per-host certificate from the sandbox's own
throwaway CA, which the payload trusts via CURL_CA_BUNDLE / SSL_CERT_FILE.

Usage: proxy.py <fixture-root> <certs-dir> <real-ca-bundle>
"""

import ipaddress
import os
import pathlib
import re
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

ROOT, CERTS, REAL_CA = map(pathlib.Path, sys.argv[1:])

LISTEN_ADDRESS = ('127.0.0.1', 8080)
MAX_REQUEST_BYTES = 65536
# Cap on a single upstream connect; generous because it now also bounds the
# idle time between keep-alive requests on a reused connection.
UPSTREAM_IDLE_SECONDS = 30
CERT_VALIDITY_DAYS = 2


def strip_proxy_headers(request):
    """Drop hop-by-hop proxy headers from an inner (tunneled) request."""
    headers, separator, body = request.partition(b'\r\n\r\n')
    lines = [
        line for line in headers.split(b'\r\n')
        if not line.lower().startswith((b'proxy-', b'proxy-connection:'))
    ]
    return b'\r\n'.join(lines) + separator + body


def read_request(conn):
    data = b""
    while b"\r\n\r\n" not in data and len(data) < MAX_REQUEST_BYTES:
        part = conn.recv(4096)
        if not part:
            return b""
        data += part
    return data


def run_openssl(args):
    """Run openssl, raising with its stderr when it fails.

    Discarding stderr here costs real debugging time: the caller sees only a
    dropped connection (``curl: (35) Recv failure``) and the log holds nothing
    but the argv, so an unwritable directory, a missing CA key, and an option
    the host's openssl rejects all look identical.
    """
    done = subprocess.run(
        ['openssl', *args], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    if done.returncode != 0:
        detail = done.stderr.decode('utf-8', 'replace').strip()
        raise RuntimeError(
            f'openssl {args[0]} failed (exit {done.returncode}): {detail}'
        )


_CERT_LOCK = threading.Lock()


def cert_for(host):
    """Return a (cert, key) pair for host, minting it from the sandbox CA.

    Minting is serialized and published atomically. The proxy is threaded, so
    two concurrent requests for the same host would otherwise both run openssl
    into the same paths, and a reader could pick up a finished certificate
    beside a key from the other writer -- which TLS rejects as
    ``[X509: KEY_VALUES_MISMATCH] key values mismatch``.
    """
    safe = ''.join(char if char.isalnum() or char in '.-' else '_' for char in host)
    cert, key = CERTS / f'{safe}.pem', CERTS / f'{safe}.key'
    if cert.exists() and key.exists():
        return cert, key
    with _CERT_LOCK:
        # Re-check: another thread may have finished while we waited.
        if cert.exists() and key.exists():
            return cert, key
        # Build under unique temp names, then rename into place. os.replace is
        # atomic, so a reader sees either the old pair or the new one, never a
        # half-written mix. The key lands first: the certificate's existence is
        # what everything else keys off.
        stamp = f'{os.getpid()}.{threading.get_ident()}'
        tmp_key = CERTS / f'{safe}.key.{stamp}'
        tmp_cert = CERTS / f'{safe}.pem.{stamp}'
        csr = CERTS / f'{safe}.csr.{stamp}'
        run_openssl([
            'req', '-newkey', 'rsa:2048', '-nodes',
            '-subj', f'/CN={host}',
            '-addext', f'subjectAltName=DNS:{host}',
            '-keyout', str(tmp_key), '-out', str(csr),
        ])
        run_openssl([
            'x509', '-req', '-days', str(CERT_VALIDITY_DAYS), '-in', str(csr),
            '-CA', str(CERTS / 'ca.pem'), '-CAkey', str(CERTS / 'ca.key'),
            '-CAcreateserial', '-copy_extensions', 'copy', '-out', str(tmp_cert),
        ])
        csr.unlink(missing_ok=True)
        os.replace(tmp_key, key)
        os.replace(tmp_cert, cert)
    return cert, key


def file_for(host, target):
    """Resolve a request to a fixture file, or None to forward upstream."""
    path = urlsplit(target).path or '/'
    parts = pathlib.PurePosixPath(unquote(path)).parts
    if '..' in parts:
        return None
    candidate = ROOT / host / pathlib.PurePosixPath(*[p for p in parts if p != '/'])
    if candidate.is_dir():
        candidate /= 'index.html'
    return candidate if candidate.is_file() else None


def respond_fixture(conn, found):
    body = found.read_bytes()
    headers = (
        f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()
    )
    conn.sendall(b'HTTP/1.1 200 OK\r\n' + headers + body)


def close_request(request, target=None):
    """Rewrite a proxied request for a direct upstream connection."""
    headers, separator, body = request.partition(b'\r\n\r\n')
    lines = headers.split(b'\r\n')
    if target is not None:
        method, _, version = lines[0].split(b' ', 2)
        lines[0] = b' '.join((method, target.encode(), version))
    lines = [
        line for line in lines
        if not line.lower().startswith(b'proxy-connection:')
    ]
    lines.append(b'Connection: close')
    return b'\r\n'.join(lines) + separator + body


def relay(source, destination):
    while True:
        chunk = source.recv(MAX_REQUEST_BYTES)
        if not chunk:
            return
        destination.sendall(chunk)


def relay_raw_duplex(left, right):
    """Relay a transparent CONNECT tunnel without terminating its TLS.

    Raw TCP sockets support full-duplex I/O, but a single readiness loop also
    gives deterministic half-close and teardown behavior without extra threads.
    """
    peers = {left: right, right: left}
    outgoing = {left: bytearray(), right: bytearray()}
    read_open = {left: True, right: True}
    write_shutdown = set()
    last_activity = time.monotonic()

    left.setblocking(False)
    right.setblocking(False)

    while any(read_open.values()) or any(outgoing.values()):
        readers = [
            source
            for source, destination in peers.items()
            if read_open[source] and not outgoing[destination]
        ]
        writers = [destination for destination, data in outgoing.items() if data]
        remaining = UPSTREAM_IDLE_SECONDS - (time.monotonic() - last_activity)
        if remaining <= 0:
            return
        try:
            readable, writable, exceptional = select.select(
                readers,
                writers,
                [left, right],
                min(1.0, remaining),
            )
        except OSError:
            return
        if exceptional:
            return

        progressed = False
        for source in readable:
            destination = peers[source]
            try:
                chunk = source.recv(MAX_REQUEST_BYTES)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            if chunk:
                outgoing[destination].extend(chunk)
                progressed = True
            else:
                read_open[source] = False

        for destination in writable:
            try:
                sent = destination.send(outgoing[destination])
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            if not sent:
                return
            del outgoing[destination][:sent]
            progressed = True

        # Propagate EOF only after bytes already read from that source have
        # drained to its peer.
        for source, destination in peers.items():
            if (
                not read_open[source]
                and not outgoing[destination]
                and destination not in write_shutdown
            ):
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                write_shutdown.add(destination)

        if progressed:
            last_activity = time.monotonic()


def relay_tls(client, upstream, initial_upstream):
    """Relay application bytes without concurrent access to either SSLSocket.

    OpenSSL does not support SSL_read and SSL_write concurrently on one SSL
    object. CPython #151508 demonstrates that doing so can corrupt native state
    and segfault. A readiness-driven loop keeps both directions on this handler
    thread while retaining HTTP keep-alive support.
    """
    peers = {client: upstream, upstream: client}
    outgoing = {client: bytearray(), upstream: bytearray(initial_upstream)}
    send_need = {client: 'write', upstream: 'write'}
    recv_need = {client: 'read', upstream: 'read'}
    last_activity = time.monotonic()

    client.setblocking(False)
    upstream.setblocking(False)

    while True:
        readers = []
        writers = []
        immediately_readable = set()

        for source, destination in peers.items():
            # Read at most one TLS record ahead. Draining it before the next
            # recv prevents an EOF on one side from discarding bytes already
            # queued for the other side.
            if outgoing[destination]:
                continue
            if recv_need[source] == 'write':
                writers.append(source)
            else:
                try:
                    if source.pending():
                        immediately_readable.add(source)
                    else:
                        readers.append(source)
                except OSError:
                    return

        for destination, buffered in outgoing.items():
            if not buffered:
                continue
            if send_need[destination] == 'read':
                readers.append(destination)
            else:
                writers.append(destination)

        remaining = UPSTREAM_IDLE_SECONDS - (time.monotonic() - last_activity)
        if remaining <= 0:
            return
        try:
            readable, writable, exceptional = select.select(
                list(dict.fromkeys(readers)),
                list(dict.fromkeys(writers)),
                [client, upstream],
                min(1.0, remaining),
            )
        except OSError:
            return
        if exceptional:
            return

        readable = set(readable) | immediately_readable
        writable = set(writable)
        progressed = False
        send_attempted = set()

        # Drain queued bytes before accepting more. This both applies
        # backpressure and gives TLS control records needed by a pending write
        # priority over application-level reads from the same SSL object.
        for destination, buffered in outgoing.items():
            if not buffered:
                continue
            need = send_need[destination]
            if not ((need == 'read' and destination in readable) or
                    (need != 'read' and destination in writable)):
                continue
            send_attempted.add(destination)
            try:
                sent = destination.send(buffered)
            except ssl.SSLWantReadError:
                send_need[destination] = 'read'
                continue
            except ssl.SSLWantWriteError:
                send_need[destination] = 'write'
                continue
            except OSError:
                return
            if not sent:
                return
            del buffered[:sent]
            send_need[destination] = 'write'
            progressed = True

        for source, destination in peers.items():
            if outgoing[destination]:
                continue
            need = recv_need[source]
            ready = (
                source in immediately_readable
                or (need == 'write' and source in writable)
                or (need != 'write' and source in readable)
            )
            if not ready:
                continue
            if send_need[source] == 'read' and source in send_attempted:
                continue
            try:
                chunk = source.recv(MAX_REQUEST_BYTES)
            except ssl.SSLWantReadError:
                recv_need[source] = 'read'
                continue
            except ssl.SSLWantWriteError:
                recv_need[source] = 'write'
                continue
            except OSError:
                return
            if not chunk:
                return
            outgoing[destination].extend(chunk)
            recv_need[source] = 'read'
            progressed = True

        if progressed:
            last_activity = time.monotonic()


def forward_https(conn, host, port, request):
    # Keep the tunnel open across HTTP keep-alive requests. The relay itself is
    # single-threaded because one SSLSocket cannot safely be read and written
    # from separate threads.
    context = ssl.create_default_context(cafile=str(REAL_CA))
    upstream = None
    # A transient upstream connect/TLS failure looks like "empty reply" to the
    # client, which reads as a broken installer rather than a network blip.
    # Retry the handshake a few times before giving up on this tunnel.
    for attempt in range(3):
        try:
            raw = socket.create_connection((host, port), timeout=UPSTREAM_IDLE_SECONDS)
            upstream = context.wrap_socket(raw, server_hostname=host)
            break
        except OSError:
            if attempt == 2:
                return
            time.sleep(1)

    try:
        relay_tls(conn, upstream, strip_proxy_headers(request))
    finally:
        upstream.close()


def forward_http(conn, host, port, request, target):
    parsed = urlsplit(target)
    path = parsed.path or '/'
    if parsed.query:
        path += f'?{parsed.query}'
    with socket.create_connection((host, port), timeout=UPSTREAM_IDLE_SECONDS) as upstream:
        upstream.sendall(close_request(request, path))
        relay(upstream, conn)


def intercept_connect(conn, host, port):
    """Terminate TLS for a host whose responses may come from fixtures."""
    conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
    cert, key = cert_for(host)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    with context.wrap_socket(conn, server_side=True) as tls:
        nested = read_request(tls)
        if not nested:
            return
        line = nested.split(b'\r\n', 1)[0].decode('iso-8859-1')
        nested_target = line.split(' ', 2)[1]
        found = file_for(host, nested_target)
        if found is not None:
            respond_fixture(tls, found)
        else:
            forward_https(tls, host, port, nested)


def fixture_host_exists(host):
    """Return whether HOST has any fixture paths, without path traversal."""
    return (
        host not in {'.', '..'}
        and '/' not in host
        and '\\' not in host
        and (ROOT / host).is_dir()
    )


def parse_connect_authority(target):
    """Return a validated ``(host, port)`` CONNECT authority.

    CONNECT uses authority-form rather than a URL.  Validate it before either
    filesystem fixture lookup or DNS so malformed input cannot accidentally
    name the fixture root (the empty-host case) or escape into ambiguous
    parsing.  Bracketed IPv6 is supported for transparent tunnels.
    """
    if (
        not target
        or any(ord(char) <= 32 or ord(char) == 127 for char in target)
        or any(char in target for char in '/\\?#@%')
    ):
        raise ValueError('invalid CONNECT authority')

    if target.startswith('['):
        closing = target.find(']')
        if (
            closing <= 1
            or target.count('[') != 1
            or target.count(']') != 1
            or target[closing + 1:closing + 2] != ':'
        ):
            raise ValueError('invalid bracketed CONNECT authority')
        host = target[1:closing]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as error:
            raise ValueError('invalid CONNECT IPv6 address') from error
        port_text = target[closing + 2:]
    else:
        if target.count(':') != 1:
            raise ValueError('CONNECT authority requires host:port')
        host, port_text = target.split(':', 1)
        if len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9.-]+', host):
            raise ValueError('invalid CONNECT hostname')
        labels = host.split('.')
        if any(
            not label
            or len(label) > 63
            or label.startswith('-')
            or label.endswith('-')
            for label in labels
        ):
            raise ValueError('invalid CONNECT hostname')

    if not port_text.isascii() or not port_text.isdecimal():
        raise ValueError('invalid CONNECT port')
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError('CONNECT port out of range')
    return host.lower(), port


def handle_connect(conn, target):
    """MITM fixture hosts and transparently tunnel every other TLS host."""
    try:
        host, port = parse_connect_authority(target)
    except ValueError:
        conn.sendall(
            b'HTTP/1.1 400 Bad Request\r\n'
            b'Connection: close\r\nContent-Length: 0\r\n\r\n'
        )
        return

    if fixture_host_exists(host):
        intercept_connect(conn, host, port)
        return

    try:
        upstream = socket.create_connection(
            (host, port), timeout=UPSTREAM_IDLE_SECONDS
        )
    except OSError:
        conn.sendall(
            b'HTTP/1.1 502 Bad Gateway\r\n'
            b'Connection: close\r\nContent-Length: 0\r\n\r\n'
        )
        return
    try:
        conn.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        relay_raw_duplex(conn, upstream)
    finally:
        upstream.close()


def host_from_headers(request):
    for header in request.split(b'\r\n')[1:]:
        if header.lower().startswith(b'host:'):
            value = header.split(b':', 1)[1].strip().decode()
            return value.split(':', 1)[0]
    return None


def handle_request(conn):
    with conn:
        request = read_request(conn)
        if not request:
            return
        line = request.split(b'\r\n', 1)[0].decode('iso-8859-1')
        method, target, _ = line.split(' ', 2)
        if method.upper() == 'CONNECT':
            handle_connect(conn, target)
            return
        parsed = urlsplit(target)
        host = parsed.hostname or host_from_headers(request) or 'unknown'
        found = file_for(host, target)
        if found is not None:
            respond_fixture(conn, found)
        else:
            forward_http(conn, host, parsed.port or 80, request, target)


def handle(conn):
    try:
        handle_request(conn)
    except Exception as error:
        print(f'proxy request failed: {error!r}', file=sys.stderr, flush=True)


def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(LISTEN_ADDRESS)
        server.listen()
        while True:
            conn, _ = server.accept()
            threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == '__main__':
    main()
