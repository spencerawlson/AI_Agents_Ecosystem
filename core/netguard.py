"""Outbound-fetch guard: never let a user-supplied URL reach an internal address.

Anyone can type a URL into the public "Analyze your site" box, and the
audits then fetch that site, its robots.txt, sitemap and links. A URL
check at submit time is not enough on its own:

- a public page can redirect to http://169.254.169.254/ (cloud
  metadata) or http://127.0.0.1:8000/admin;
- a hostname can resolve to a public address when it is checked and to
  a private one a moment later when it is fetched (DNS rebinding);
- a hostname can resolve to several addresses, one of them private.

So every request made through ``safe_urlopen`` is checked twice: the
URL's host before the request (literal IPs and every DNS answer), and
the address the socket actually connected to after it connects, before
a byte of the request is sent. Redirects go through the same checks
because each hop is a new request; they may only go to http/https.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

MAX_REDIRECTS = 5

# IANA benchmarking space. Never a LAN, but the sandbox egress proxy
# resolves every hostname into it, so it must not count as internal:
# blocking it would block every fetch there. Nothing internal lives in
# this range.
_BENCHMARK_NET = ipaddress.ip_network("198.18.0.0/15")


class BlockedAddressError(urllib.error.URLError):
    """The request would reach an internal or reserved address."""


def is_blocked_address(value: str) -> bool:
    """True for loopback, private, link-local (incl. cloud metadata),
    reserved, multicast and unspecified addresses — and IPv4-mapped
    IPv6 forms of them. False for public addresses and non-IPs."""
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip in _BENCHMARK_NET:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _resolve(host: str) -> list[str]:
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except OSError:
        return []


def check_url(url: str) -> None:
    """Raise BlockedAddressError unless the URL is http(s) to a host
    with no internal address (literal, or among its DNS answers)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise BlockedAddressError(f"scheme not allowed: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        raise BlockedAddressError("localhost is not allowed")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    addresses = [host] if literal else _resolve(host)
    if any(is_blocked_address(a) for a in addresses):
        raise BlockedAddressError(f"{host} resolves to an internal address")


def _check_peer(sock: socket.socket) -> None:
    peer = sock.getpeername()[0]
    if is_blocked_address(peer):
        sock.close()
        raise BlockedAddressError(f"connected to internal address {peer}")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        if not self._tunnel_host:
            _check_peer(self.sock)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Same steps as HTTPSConnection.connect, with the peer check
        # between the TCP connect and the TLS handshake.
        http.client.HTTPConnection.connect(self)
        if not self._tunnel_host:
            _check_peer(self.sock)
        server_hostname = self._tunnel_host or self.host
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=server_hostname)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)

    def http_request(self, req):
        check_url(req.full_url)
        return super().http_request(req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req,
                            context=self._context)

    def https_request(self, req):
        check_url(req.full_url)
        return super().https_request(req)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme not in ("http", "https"):
            raise BlockedAddressError(f"redirect to non-web URL {newurl!r}")
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    # build_opener replaces the default handlers with these subclasses;
    # no FTP/file/data handlers are reachable.
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(),
                    urllib.request.UnknownHandler(),
                    _GuardedHTTPHandler(), _GuardedHTTPSHandler(),
                    urllib.request.HTTPDefaultErrorHandler(),
                    _GuardedRedirectHandler(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


def safe_urlopen(req, timeout: float = 15):
    """urlopen for untrusted URLs: http(s) only, no internal addresses,
    at most MAX_REDIRECTS redirects, each hop re-checked."""
    url = req.full_url if isinstance(req, urllib.request.Request) else req
    check_url(url)
    return _opener().open(req, timeout=timeout)
