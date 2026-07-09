"""Lightweight request hardening middleware.

The public server constantly receives automated vulnerability-scanner traffic
probing for secrets and exploit endpoints that this app does not serve
(``/.env``, ``*.php``, ``/wp-admin/...``, phpunit RCE paths, etc). Django
already 404s them, but each one routes through the full stack and emits a noisy
``django.request`` warning.

``BlockProbesMiddleware`` short-circuits those requests at the very top of the
stack: it returns a bare, body-less 404 *before* URL routing, CSRF, or static
serving run. Because it returns a response directly (instead of raising
``Http404``), Django never logs the "Not Found" warning, so the log spam stops
as well. The pattern list is intentionally narrow so it can never match a real
app route or static asset (e.g. the vendored ``/static/vendor/quill/`` files).
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from django.db import InterfaceError, OperationalError, connections
from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponseNotFound

logger = logging.getLogger("accounts.security")

# Paths that only ever come from bots / vulnerability scanners. Kept precise so
# legitimate routes and static assets are never matched.
_PROBE_RE = re.compile(
    r"""
      (?:^|/)\.(?:env|git|aws|ssh|svn|hg|bzr|htpasswd|htaccess|docker|dockerenv|npmrc|yarnrc)\b
    | \.(?:php\d*|phps|phtml|asp|aspx|jsp|cgi|cfm)(?=$|[/?#])  # server-script probes
    | (?:^|/)wp-(?:admin|content|includes|login|config|json)    # WordPress fishing
    | (?:^|/)wordpress(?=$|/)
    | (?:phpunit|phpinfo|eval-stdin|xmlrpc|composer\.(?:json|lock))
    | (?:^|/)(?:cgi-bin|boaform|HNAP1|actuator|jmx-console|manager/html|_ignition|telescope|server-status|server-info)(?=$|/)
    | (?:^|/)(?:backup|backups|dump|database|db)(?:\.(?:sql|zip|tar|gz|tgz|bak|old)|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_ENCODED_TRAVERSAL_RE = re.compile(
    r"(?:%2e|\.)(?:%2e|\.)(?:%2f|%5c|/|\\)|(?:%2f|%5c|/|\\)(?:%2e|\.)(?:%2e|\.)(?:%2f|%5c|/|\\|$)",
    re.IGNORECASE,
)
_DECODED_TRAVERSAL_RE = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ALLOWED_METHODS = frozenset({"GET", "HEAD", "POST", "OPTIONS"})
_MAX_REQUEST_TARGET_BYTES = 8192


def _request_target(request):
    parts = [request.path]
    for meta_name in ("RAW_URI", "REQUEST_URI"):
        value = request.META.get(meta_name)
        if value:
            parts.append(value)
    query = request.META.get("QUERY_STRING")
    if query:
        parts.append("?" + query)
    raw = " ".join(parts)
    decoded = raw
    for _ in range(2):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    return raw, decoded


def _blocked_response(status=404):
    response = HttpResponseNotFound() if status == 404 else HttpResponse(status=status)
    # Keep intentional drops out of django.request warning logs.
    response._has_been_logged = True
    return response


class BlockProbesMiddleware:
    """Drop obvious scanner probes with a quiet 404 before anything else runs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in _ALLOWED_METHODS:
            logger.debug("Blocked unsupported method: %s %s", request.method, request.path)
            response = HttpResponseNotAllowed(sorted(_ALLOWED_METHODS))
            response._has_been_logged = True
            return response

        raw_target, decoded_target = _request_target(request)
        if len(raw_target.encode("utf-8", errors="ignore")) > _MAX_REQUEST_TARGET_BYTES:
            logger.debug("Blocked overlong request target: %s %s", request.method, request.path)
            return _blocked_response(414)

        if (
            _PROBE_RE.search(raw_target)
            or _PROBE_RE.search(decoded_target)
            or _ENCODED_TRAVERSAL_RE.search(raw_target)
            or _DECODED_TRAVERSAL_RE.search(decoded_target)
            or _CONTROL_RE.search(decoded_target)
            or "%00" in raw_target.lower()
        ):
            # DEBUG so it's silent by default but available when investigating.
            logger.debug("Blocked scanner probe: %s %s", request.method, request.path)
            return _blocked_response()
        return self.get_response(request)


# Substrings of the libpq/psycopg2 errors that mean "this connection is dead"
# (the socket was dropped by the server, a NAT gateway, or a DB proxy) -- as
# opposed to a real query error we should never paper over. Matched
# case-insensitively against the exception text.
_DEAD_CONNECTION_SIGNS = (
    "server closed the connection unexpectedly",
    "terminating connection",
    "connection already closed",
    "connection not open",
    "could not receive data from server",
    "could not send data to server",
    "ssl connection has been closed unexpectedly",
    "eof detected",
    "consuming input failed",
    "server terminated abnormally",
)


def _is_dropped_connection(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(sign in msg for sign in _DEAD_CONNECTION_SIGNS)


class DBReconnectMiddleware:
    """Survive a remote Postgres connection that dies *mid-request*.

    On a networked DB (e.g. a self-hosted box talking to a managed Postgres
    over the internet) the server, a NAT gateway, or a DB proxy can drop an
    idle TCP connection at any moment. ``CONN_HEALTH_CHECKS`` only validates a
    pooled connection at the *start* of a request; if the socket dies *between*
    queries within one request, the query blows up with
    ``OperationalError: server closed the connection unexpectedly`` and the page
    500s.

    For a *safe, idempotent* request (GET/HEAD -- a page load, never a write)
    this middleware catches that specific dropped-connection error, discards the
    dead connection(s) so Django opens fresh ones, and replays the request
    exactly once. A real query/programming error, or any non-idempotent method,
    is re-raised untouched so we never silently retry a write or hide a genuine
    bug.

    Placed just inside ``BlockProbesMiddleware`` so its retry wraps every other
    middleware that touches the DB (sessions, auth) as well as the view itself.
    """

    SAFE_METHODS = frozenset({"GET", "HEAD"})

    def __init__(self, get_response):
        self.get_response = get_response

    def _drop_dead_connections(self) -> None:
        # Force-close every connection so the next query reconnects. We close
        # unconditionally (not close_if_unusable_or_obsolete) because the whole
        # point is that the socket is already gone; a fresh connect is cheap
        # next to a 500.
        for conn in connections.all():
            try:
                conn.close()
            except Exception:  # pragma: no cover - closing a dead socket
                pass

    def __call__(self, request):
        try:
            return self.get_response(request)
        except (OperationalError, InterfaceError) as exc:
            if request.method not in self.SAFE_METHODS or not _is_dropped_connection(exc):
                raise
            logger.warning(
                "DB connection dropped mid-request; retrying %s %s once (%s)",
                request.method,
                request.path,
                exc.__class__.__name__,
            )
            self._drop_dead_connections()
            return self.get_response(request)
