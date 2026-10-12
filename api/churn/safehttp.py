"""Outbound HTTP to addresses customers type in (alert webhooks, data connectors), without letting them point the server at its own network.

  * http(s) only, no credentials in the URL; http only when SAFEHTTP_ALLOW_LOCAL=1 (tests / local development, never production)
  * the host name is resolved once; every address must be public (no loopback, private, link-local, metadata or reserved ranges);
    the connection goes to that exact address, so the answer cannot change between the check and the call (DNS rebinding)
  * redirects are never followed; timeouts and response size are capped
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
from urllib.parse import urlsplit


class UnsafeDestination(ValueError):
    """The URL is malformed or points somewhere we will not call."""


def allow_local() -> bool:
    return os.environ.get('SAFEHTTP_ALLOW_LOCAL') == '1'


def is_public(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def resolve_safe(url: str) -> tuple[str, str, int, str, str]:
    """Validate a destination. Returns (scheme, host, port, path_with_query, pinned_ip)."""
    parts = urlsplit(url or '')
    if parts.scheme not in ('https', 'http') or not parts.hostname:
        raise UnsafeDestination('Use a full https:// address.')
    if parts.scheme == 'http' and not allow_local():
        raise UnsafeDestination('Only https:// destinations are allowed.')
    if parts.username or parts.password:
        raise UnsafeDestination('Credentials inside the URL are not allowed.')
    host = parts.hostname
    try:
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, ValueError) as exc:
        raise UnsafeDestination(f'Could not resolve {host}.') from exc
    addrs = [ipaddress.ip_address(i[4][0]) for i in infos]
    if not addrs:
        raise UnsafeDestination(f'Could not resolve {host}.')
    if not allow_local() and not all(is_public(a) for a in addrs):
        raise UnsafeDestination('That address points to a private or internal network, which is not allowed.')
    path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
    return parts.scheme, host, port, path, str(addrs[0])


class _Pinned(http.client.HTTPConnection):
    def __init__(self, host, port, ip, secure, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip, self._secure, self._ctx = ip, secure, ssl.create_default_context()

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(sock, server_hostname=self.host) if self._secure else sock


def request(method: str, url: str, body: bytes | None = None, headers: dict | None = None, timeout: float = 5,
            max_bytes: int = 64 * 1024) -> tuple[int, bytes]:
    """One request, never following redirects. Raises UnsafeDestination before connecting, or ValueError if the body is over max_bytes."""
    scheme, host, port, path, ip = resolve_safe(url)
    conn = _Pinned(host, port, ip, scheme == 'https', timeout)
    try:
        conn.request(method, path, body=body, headers={'User-Agent': 'ChurnSight', **(headers or {})})
        resp = conn.getresponse()
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f'The response is larger than {max_bytes // 1024} KB.')
        return resp.status, data
    finally:
        conn.close()
