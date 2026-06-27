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

from django.http import HttpResponseNotFound

logger = logging.getLogger("accounts.security")

# Paths that only ever come from bots / vulnerability scanners. Kept precise so
# legitimate routes and static assets are never matched.
_PROBE_RE = re.compile(
    r"""
      (?:^|/)\.(?:env|git|aws|ssh|svn|hg|htpasswd|htaccess)\b  # dotfile secrets / VCS dirs
    | \.(?:php|phps|phtml|asp|aspx|jsp|cgi|cfm)(?=$|/)         # server-script probes
    | (?:^|/)wp-(?:admin|content|includes|login|config)        # WordPress fishing
    | (?:^|/)wordpress(?=$|/)
    | (?:phpunit|phpinfo|eval-stdin|xmlrpc)                     # known exploit markers
    """,
    re.IGNORECASE | re.VERBOSE,
)


class BlockProbesMiddleware:
    """Drop obvious scanner probes with a quiet 404 before anything else runs."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _PROBE_RE.search(request.path):
            # DEBUG so it's silent by default but available when investigating.
            logger.debug("Blocked scanner probe: %s %s", request.method, request.path)
            response = HttpResponseNotFound()
            # Django's BaseHandler.get_response() logs *every* response with
            # status >= 400 via log_response() (the "Not Found: <path>"
            # warning on the django.request logger). Setting the flag Django
            # itself uses for this keeps our intentional drop silent.
            response._has_been_logged = True
            return response
        return self.get_response(request)
