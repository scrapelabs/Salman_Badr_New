"""SSRF-safety helpers shared by the URL validator and the HTTP client.

Centralises the "is this URL safe to fetch?" decision so the same rules apply
both when validating a user-supplied seed URL (``views.validate_run_params``)
and when following redirects inside :class:`~accounts.live_scrapers._http.ScraperClient`.

The core invariant: a scraper must never issue an HTTP request whose host
resolves to a private, loopback, link-local, reserved, multicast or otherwise
non-public address. Otherwise the URL input (or an open redirect served by an
allowlisted host) could be turned into a probe of internal infrastructure.

Resolving the host (not just parsing it) is what defeats numeric-host
obfuscation: ``http://2130706433/`` or ``http://0x7f000001/`` parse as plain
hostnames but ``getaddrinfo`` resolves them to ``127.0.0.1``.
"""

import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe to fetch (bad scheme/host or private IP)."""


def _ip_is_blocked(ip):
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _normalize(ip):
    """Unwrap IPv4-mapped/compatible IPv6 so the v4 rules apply to them too."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _check_ip_literal(host):
    """If ``host`` is an IP literal, raise when non-public; else return ``None``."""
    try:
        ip = _normalize(ipaddress.ip_address(host))
    except ValueError:
        return None
    if _ip_is_blocked(ip):
        raise UnsafeUrlError(f"host {host} is a non-public address")
    return ip


def assert_resolves_public(host):
    """Raise :class:`UnsafeUrlError` if ``host`` is (or resolves to) a non-public IP.

    Catches both IP literals and hostname/numeric forms that ``getaddrinfo``
    resolves to private space.
    """
    if _check_ip_literal(host) is not None:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"cannot resolve host {host}") from exc
    for info in infos:
        addr = info[4][0].split("%", 1)[0]  # drop any IPv6 scope id
        ip = _normalize(ipaddress.ip_address(addr))
        if _ip_is_blocked(ip):
            raise UnsafeUrlError(f"host {host} resolves to non-public {ip}")


def assert_safe_url(url, *, allowed_hosts=None):
    """Validate ``url`` for fetching; raise :class:`UnsafeUrlError` if unsafe.

    Enforces an ``http(s)`` scheme and a real host, blocks obvious local names,
    optionally enforces a host allowlist, and (crucially) requires the host to
    resolve to a public IP. Returns the URL on success.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise UnsafeUrlError("only http(s) URLs with a host are allowed")
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise UnsafeUrlError(f"host {host} is not allowed")
    if allowed_hosts and not any(
        host == h or host.endswith("." + h) for h in allowed_hosts
    ):
        raise UnsafeUrlError(f"host {host} is not in the allowlist")
    assert_resolves_public(host)
    return url
