"""Per-request HTTP I/O limits, with no detached network workers or retries."""

import errno
import http.client
import io
import selectors
import socket
import ssl
import time
import urllib.request

POLL_SECONDS = 0.1


class RequestDeadline(TimeoutError):
    pass


class IdleTimeout(TimeoutError):
    pass


class IOBudget:
    def __init__(self, idle, total, check_cancelled=None):
        self.idle, self.check_cancelled = idle, check_cancelled or (lambda: None)
        self.last_activity = time.monotonic()
        self.deadline = self.last_activity + total

    def check(self):
        self.check_cancelled()
        now = time.monotonic()
        if now >= self.deadline:
            raise RequestDeadline("request deadline exceeded")
        if now - self.last_activity >= self.idle:
            raise IdleTimeout("network idle timeout")

    def timeout(self):
        self.check()
        now = time.monotonic()
        return max(0.000001, min(self.deadline - now, self.idle - (now - self.last_activity)))

    def activity(self):
        self.check()  # Late data cannot turn an expired request into success.
        self.last_activity = time.monotonic()

    def wait(self, sock, event):
        with selectors.DefaultSelector() as selector:
            selector.register(sock, event)
            while True:
                ready = selector.select(min(POLL_SECONDS, self.timeout()))
                self.check()
                if ready:
                    return

    def connect(self, address, timeout=None, source_address=None):
        self.check()
        # The platform resolver is synchronous. Check again immediately after
        # it returns, so cancellation/expiry cannot cause a late TCP/POST send.
        addresses = self.resolve(address)
        self.check()
        pending = {errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY, errno.EINTR}
        pending.update(getattr(errno, name) for name in ("WSAEWOULDBLOCK", "WSAEINPROGRESS", "WSAEALREADY")
                       if hasattr(errno, name))
        error = OSError("no addresses returned")
        for family, kind, protocol, _, target in addresses:
            self.check()
            sock = socket.socket(family, kind, protocol)
            try:
                if source_address:
                    sock.bind(source_address)
                sock.setblocking(False)
                result = sock.connect_ex(target)
                if result in pending:
                    self.wait(sock, selectors.EVENT_WRITE)
                    result = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if result:
                    raise OSError(result, "connection failed")
                self.activity()
                sock.settimeout(self.timeout())
                return sock
            except BaseException as exc:
                sock.close()
                if not isinstance(exc, OSError) or isinstance(exc, TimeoutError):
                    raise
                error = exc
        raise error

    def resolve(self, address):
        return socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM)

    def handshake(self, sock):
        sock.setblocking(False)
        while True:
            self.check()
            try:
                sock.do_handshake()
                self.activity()
                sock.settimeout(self.timeout())
                return
            except ssl.SSLWantReadError:
                self.wait(sock, selectors.EVENT_READ)
            except ssl.SSLWantWriteError:
                self.wait(sock, selectors.EVENT_WRITE)


class _Reader(io.RawIOBase):
    def __init__(self, sock, budget):
        super().__init__()
        self.sock, self.budget = sock, budget
        self.lease = None
        # Hold the standard socket's file reference until this reader closes.
        # urllib closes its connection owner as soon as headers are returned.
        # Never read this lease: socket.SocketIO becomes unusable after timeout.
        self.lease = sock.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        if self.closed:
            raise ValueError("read of closed HTTP response")
        while True:
            self.sock.settimeout(min(POLL_SECONDS, self.budget.timeout()))
            try:
                count = self.sock.recv_into(buffer)
            except TimeoutError:
                self.budget.check()
                continue  # Receive polling only; never resend request bytes.
            if count:
                self.budget.activity()
            else:
                self.budget.check()
            return count

    def close(self):
        try:
            super().close()
        finally:
            if self.lease is not None:
                self.lease.close()


class _Socket:
    def __init__(self, sock, budget):
        self.sock, self.budget = sock, budget

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def makefile(self, mode="rb", buffering=None):
        if mode != "rb":
            raise ValueError("HTTP transport only supports binary response reads")
        raw = _Reader(self.sock, self.budget)
        try:
            return io.BufferedReader(raw)
        except BaseException:
            raw.close()
            raise

    def sendall(self, data):
        # A failed write has ambiguous delivery; do not retry its bytes. A
        # blocked send is bounded by the remaining idle and total limits.
        self.sock.settimeout(self.budget.timeout())
        try:
            self.sock.sendall(data)
        except TimeoutError:
            self.budget.check()
            raise
        self.budget.activity()


class _Connection:
    def __init__(self, *args, budget, **kwargs):
        super().__init__(*args, **kwargs)
        self.budget = budget
        self._create_connection = budget.connect

    def _tunnel(self):
        original = self.sock
        self.sock = _Socket(original, self.budget)
        try:
            return super()._tunnel()
        finally:
            self.sock = original  # TLS must wrap the actual connected socket.


class HTTPConnection(_Connection, http.client.HTTPConnection):
    def connect(self):
        super().connect()
        self.budget.check()
        self.sock = _Socket(self.sock, self.budget)


class HTTPSConnection(_Connection, http.client.HTTPSConnection):
    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.budget.check()
        hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(self.sock, server_hostname=hostname, do_handshake_on_connect=False)
        self.budget.handshake(self.sock)
        self.sock = _Socket(self.sock, self.budget)


class HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(lambda *args, **kwargs: HTTPConnection(*args, budget=req._sprig_io_budget, **kwargs), req)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # Python 3.11 keeps check_hostname on the handler; newer Python applies
        # it to the context. Preserve the standard handler's TLS policy.
        options = {"context": self._context}
        if hasattr(self, "_check_hostname"):
            options["check_hostname"] = self._check_hostname
        return self.do_open(lambda *args, **kwargs: HTTPSConnection(*args, budget=req._sprig_io_budget, **kwargs), req,
                            **options)
