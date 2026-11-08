"""Test-wide guard: no real network.

Several dashboard tests post to /api/analyze without stubbing the
runner, which starts a background job that fetches the real site.
Those threads outlive their test, and when the network is slow or
blocked they pile up until an unrelated later test hangs. Every
test that needs HTTP already fakes it (monkeypatched fetchers), so
outbound connections are refused here, immediately, for the whole
session. Loopback stays open: Starlette's TestClient and the asyncio
event loop on Windows use local sockets.
"""

import socket

import pytest

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


def _is_loopback(host) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode(errors="ignore")
    host = str(host)
    return host in _LOOPBACK or host.startswith("127.")


@pytest.fixture(autouse=True, scope="session")
def _no_network():
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, *args, **kwargs):
        if not _is_loopback(host):
            raise socket.gaierror(f"network disabled in tests: {host}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _check(address):
        host = address[0] if isinstance(address, tuple) else None
        if not _is_loopback(host):
            raise ConnectionRefusedError(
                f"network disabled in tests: {address}")

    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            _check(address)
        return real_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo
        socket.socket.connect = real_connect
        socket.socket.connect_ex = real_connect_ex
