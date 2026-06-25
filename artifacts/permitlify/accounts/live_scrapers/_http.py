"""Shared anti-bot HTTP client for the live scrapers.

A thin wrapper over ``curl_cffi`` that every non-Stadion scraper uses so they
all get the same hardening in one place:

- Chrome TLS/JA3 impersonation (``impersonate="chrome"``) + realistic browser
  headers, so requests look like a real browser rather than a bot.
- Per-scraper proxy routing (:func:`build_proxies`) — a residential proxy is
  required to reach origins behind CDNs that block datacenter IPs. The proxy
  address may embed credentials, so it is **never** logged (only the pool name
  and type) and is scrubbed from any error text via ``redact_secrets``.
- Retry with exponential backoff + jitter on transient/blocked statuses, plus
  lightweight anti-bot **challenge detection** (Cloudflare / DataDome markers).
- Per-attempt telemetry recording (so every run still exports a ``requests``
  CSV) and honest error recording on give-up.

One :class:`ScraperClient` owns a single ``curl_cffi`` session and is **not**
shared across threads (curl_cffi sessions aren't thread-safe). Create one per
worker thread; the shared, thread-safe pieces (telemetry, the log callable, the
proxies dict) are passed in. ``trust_env=False`` makes "direct" authoritative:
the client never silently inherits ``HTTP(S)_PROXY`` env vars — per-scraper
routing is the only source of truth.
"""

import random
import time
from urllib.parse import urljoin

from curl_cffi import CurlMime
from curl_cffi import requests as cffi_requests
from parsel import Selector

from ._ssrf import UnsafeUrlError, assert_safe_url
from .telemetry import redact_secrets


def _to_bytes(value):
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _build_multipart(files):
    """Build a ``CurlMime`` from a requests-style ``files`` dict.

    ``curl_cffi`` doesn't accept the ``files=`` kwarg (it raises
    ``NotImplementedError`` and wants a ``multipart`` ``CurlMime``). This adapts
    the familiar ``{name: value}`` / ``{name: (filename, content)}`` form so the
    scrapers can keep expressing multipart fields the requests way. A
    ``filename`` of ``None`` (or a bare value) becomes a plain form field — which
    is exactly how the tenisintegrado index POST sends its parameters.
    """
    parts = []
    for name, value in files.items():
        if isinstance(value, (tuple, list)):
            filename = value[0]
            content = value[1] if len(value) > 1 else ""
            content_type = value[2] if len(value) > 2 else None
        else:
            filename, content, content_type = None, value, None
        part = {"name": str(name), "data": _to_bytes(content)}
        if filename:
            part["filename"] = str(filename)
        if content_type:
            part["content_type"] = str(content_type)
        parts.append(part)
    return CurlMime.from_list(parts)

DEFAULT_IMPERSONATE = "chrome"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
)

# Realistic browser headers; curl_cffi's impersonation supplies the rest of the
# fingerprint (TLS, HTTP/2 settings, header order).
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Statuses worth retrying: transient server errors, rate limits, and the
# 403/503 that CDNs return when they (temporarily) block a request.
RETRY_STATUSES = frozenset({403, 408, 425, 429, 500, 502, 503, 504})

# Substrings that betray an anti-bot interstitial rather than real content.
_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "cf-chl",
    "/cdn-cgi/challenge-platform",
    "attention required",
    "request blocked",
    "access denied",
    "captcha-delivery",
    "datadome",
    "are you a human",
)
# Only sniff the body for a challenge when the status hints at a block — normal
# 200 HTML must not trip a false positive.
_CHALLENGE_STATUSES = frozenset({202, 403, 405, 429, 503})


def build_proxies(scraper, log, *, impersonate=DEFAULT_IMPERSONATE):
    """Return a ``curl_cffi`` proxies dict for the scraper's selected proxy.

    A proxy with a non-empty address routes traffic through it; otherwise the
    scraper connects directly (returns ``None``). The address (which may carry
    credentials) is never logged — only the pool's name and type. Mirrors the
    Stadion scraper's routing so behaviour is identical across scrapers.
    """
    proxy = getattr(scraper, "proxy", None)
    if proxy and proxy.is_active and (proxy.address or "").strip():
        addr = proxy.address.strip()
        if "://" not in addr:
            addr = "http://" + addr
        log(
            "INFO",
            f"\U0001f50c HTTP client: curl_cffi (impersonate {impersonate}) via "
            f"{proxy.get_kind_display()} proxy '{proxy.name}'",
        )
        return {"http": addr, "https": addr}
    if proxy and proxy.is_active:
        log(
            "WARN",
            f"\u26a0\ufe0f Proxy '{proxy.name}' ({proxy.get_kind_display()}) "
            "selected but has no address \u2014 using direct connection",
        )
    else:
        log(
            "INFO",
            f"\U0001f50c HTTP client: curl_cffi (impersonate {impersonate}, "
            "direct \u2014 no proxy)",
        )
    return None


class ScraperClient:
    """A single-thread ``curl_cffi`` session with retries, backoff and telemetry.

    Use one per worker thread (optionally as a context manager so the session is
    closed). Cookies persist across calls on the same client, which the
    tournamentsoftware / tenisintegrado flows rely on.
    """

    def __init__(
        self,
        *,
        log,
        tele,
        proxies=None,
        impersonate=DEFAULT_IMPERSONATE,
        headers=None,
        tries=4,
        timeout=30,
        backoff_base=1.0,
        backoff_cap=12.0,
        jitter=0.75,
        allowed_hosts=None,
        max_redirects=5,
    ):
        self.log = log
        self.tele = tele
        self.proxies = proxies
        self.impersonate = impersonate
        self.default_headers = dict(DEFAULT_HEADERS)
        if headers:
            self.default_headers.update(headers)
        self.allowed_hosts = tuple(allowed_hosts) if allowed_hosts else None
        self.max_redirects = max_redirects
        self.tries = tries
        self.timeout = timeout
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.jitter = jitter
        self._session = None

    # -- lifecycle -------------------------------------------------------
    @property
    def session(self):
        if self._session is None:
            self._session = cffi_requests.Session(trust_env=False)
        return self._session

    def close(self):
        if self._session is not None:
            try:
                self._session.close()
            finally:
                self._session = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals -------------------------------------------------------
    def _sleep(self, attempt):
        delay = min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_cap)
        time.sleep(delay + random.uniform(0, self.jitter))

    def _is_challenge(self, resp):
        if resp.status_code not in _CHALLENGE_STATUSES:
            return False
        try:
            sample = resp.text[:8192].lower()
        except Exception:  # noqa: BLE001 - body may be binary / undecodable
            return False
        return any(marker in sample for marker in _CHALLENGE_MARKERS)

    # -- requests --------------------------------------------------------
    def _fetch_one(
        self, method, url, *, merged, params, data, json, multipart, tries,
        timeout, retry_statuses,
    ):
        """Try one URL up to ``tries`` times (no redirect following).

        Returns ``(resp_or_None, last_exc)``. Redirect responses (3xx with a
        ``Location``) are handed back unfollowed so :meth:`request` can validate
        the hop before continuing. Each attempt is recorded in telemetry.
        """
        last_exc = None
        for attempt in range(1, tries + 1):
            start = time.time()
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=merged,
                    params=params,
                    data=data,
                    json=json,
                    multipart=multipart,
                    impersonate=self.impersonate,
                    proxies=self.proxies,
                    timeout=timeout,
                    allow_redirects=False,
                )
                status = resp.status_code
                body = resp.content or b""
                self.tele.record_request(
                    url=url, method=method, status=status, size=len(body),
                    duration_ms=(time.time() - start) * 1000,
                )
                if self._is_challenge(resp):
                    self.log(
                        "WARN",
                        f"\U0001f6e1\ufe0f {method} {url} \u2192 anti-bot "
                        f"challenge (HTTP {status}, retry {attempt}/{tries})",
                    )
                    last_exc = RuntimeError(f"anti-bot challenge (HTTP {status})")
                elif 300 <= status < 400 and resp.headers.get("location"):
                    # A redirect — hand back so request() can vet the next hop.
                    return resp, None
                elif 200 <= status < 300:
                    return resp, None
                elif status in retry_statuses:
                    self.log(
                        "WARN",
                        f"\u26a0\ufe0f {method} {url} \u2192 HTTP {status} "
                        f"(retry {attempt}/{tries})",
                    )
                    last_exc = RuntimeError(f"HTTP {status}")
                else:
                    # A non-retryable response (e.g. 404) — hand it back so the
                    # caller can decide; it isn't a transport failure.
                    self.log(
                        "WARN", f"\u26a0\ufe0f {method} {url} \u2192 HTTP {status}"
                    )
                    return resp, None
            except Exception as exc:  # noqa: BLE001 - log, record and retry
                self.tele.record_request(
                    url=url, method=method, status=None, size=0,
                    duration_ms=(time.time() - start) * 1000,
                )
                self.log(
                    "WARN",
                    redact_secrets(
                        f"\u26a0\ufe0f {method} {url} \u2192 "
                        f"{exc.__class__.__name__}: {exc} "
                        f"(retry {attempt}/{tries})"
                    ),
                )
                last_exc = exc
            if attempt < tries:
                self._sleep(attempt)
        return None, last_exc

    def request(
        self,
        method,
        url,
        *,
        headers=None,
        params=None,
        data=None,
        json=None,
        files=None,
        tries=None,
        timeout=None,
        retry_statuses=RETRY_STATUSES,
    ):
        """Perform an HTTP request with retries; return the final ``Response`` or
        ``None`` if every attempt failed.

        Redirects are **not** followed automatically. Each hop's target is
        validated with :func:`assert_safe_url` (http(s), no local names, must
        resolve to a public IP) before it is fetched, so neither a user-supplied
        URL nor an open redirect from an allowlisted host can be used to reach
        internal/private addresses (SSRF). 301/302/303 downgrade to ``GET`` and
        drop the body the way browsers do; 307/308 preserve method + body.
        """
        tries = tries or self.tries
        timeout = timeout or self.timeout
        merged = dict(self.default_headers)
        if headers:
            merged.update(headers)
        # curl_cffi has no requests-style ``files=`` kwarg; convert it to a
        # ``CurlMime`` once and reuse it across retries (closed in finally).
        multipart = _build_multipart(files) if files else None
        cur_method, current_url = method, url
        cur_params, cur_data, cur_json, cur_multipart = (
            params, data, json, multipart,
        )
        redirects_left = self.max_redirects
        last_exc = None
        try:
            # Validate the INITIAL target too, not just redirect hops. A scraper
            # may fetch URLs it discovered from external content (Google Sheets,
            # schedule pages, PDF/box-score links) that never passed through the
            # view's validate_run_params SSRF guard. Same rules as redirects:
            # http(s) only, no local names, must resolve to a public IP (with the
            # optional host allowlist). This makes the public-IP guard apply to
            # every request the client issues, closing second-stage SSRF.
            try:
                assert_safe_url(current_url, allowed_hosts=self.allowed_hosts)
            except UnsafeUrlError as exc:
                self.log(
                    "WARN",
                    redact_secrets(
                        f"\U0001f6e1\ufe0f blocked unsafe URL "
                        f"{current_url}: {exc}"
                    ),
                )
                self.tele.record_error(
                    redact_secrets(f"Unsafe URL blocked for {current_url}: {exc}")
                )
                return None
            while True:
                resp, last_exc = self._fetch_one(
                    cur_method, current_url, merged=merged, params=cur_params,
                    data=cur_data, json=cur_json, multipart=cur_multipart,
                    tries=tries, timeout=timeout, retry_statuses=retry_statuses,
                )
                if resp is None:
                    break  # every attempt failed; error recorded below
                status = resp.status_code
                location = resp.headers.get("location") if 300 <= status < 400 else None
                if not location or redirects_left <= 0:
                    return resp
                next_url = urljoin(current_url, location)
                try:
                    assert_safe_url(next_url, allowed_hosts=self.allowed_hosts)
                except UnsafeUrlError as exc:
                    self.log(
                        "WARN",
                        redact_secrets(
                            f"\U0001f6e1\ufe0f blocked unsafe redirect "
                            f"{current_url} \u2192 {next_url}: {exc}"
                        ),
                    )
                    self.tele.record_error(
                        redact_secrets(
                            f"Unsafe redirect blocked for {current_url}: {exc}"
                        )
                    )
                    return None
                redirects_left -= 1
                current_url = next_url
                if status not in (307, 308):
                    # Browser behaviour: drop to a bodyless GET on 301/302/303.
                    cur_method = "GET"
                    cur_params = cur_data = cur_json = cur_multipart = None
        finally:
            if multipart is not None:
                try:
                    multipart.close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
        self.tele.record_error(
            redact_secrets(f"Request failed for {url}: {last_exc}"),
            exc=last_exc if isinstance(last_exc, BaseException) else None,
        )
        return None

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get_selector(self, url, **kwargs):
        """GET ``url`` and return a parsel :class:`Selector`, or ``None``."""
        resp = self.get(url, **kwargs)
        if resp is not None and 200 <= resp.status_code < 300:
            return Selector(text=resp.text)
        return None

    def get_json(self, url, **kwargs):
        """GET ``url`` and return parsed JSON, or ``None`` on any failure."""
        resp = self.get(url, **kwargs)
        if resp is not None and 200 <= resp.status_code < 300:
            try:
                return resp.json()
            except Exception:  # noqa: BLE001 - body wasn't JSON
                return None
        return None

    def selector(self, resp):
        """Build a parsel :class:`Selector` from an existing response."""
        return Selector(text=resp.text) if resp is not None else None
