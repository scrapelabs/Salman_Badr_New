"""Patchright (stealth Chromium) read-client for anti-bot origins.

Some origins (notably ``www.itftennis.com``, behind Imperva/Incapsula) serve a
JavaScript interstitial that a plain ``curl_cffi`` request can't solve when the
egress IP is challenged — the request comes back as a block page and the scrape
silently collects zero rows. A real browser executes that challenge JS, gets the
clearance cookies, and can then hit the site's JSON/XML APIs normally.

:class:`BrowserClient` wraps a single `patchright`_ Chromium session and exposes
**only** the three read methods the itftennis engine needs, with the same shape
as :class:`accounts.live_scrapers._http.ScraperClient`:

* ``get_selector(url)`` — ``page.goto`` (which solves the Incapsula challenge)
  then a :class:`parsel.Selector` of the rendered HTML, or ``None``;
* ``get_json(url, params=, headers=)`` — a JSON API call via
  ``context.request`` (which reuses the page's solved cookies), parsed, or
  ``None``;
* ``get(url)`` — the same API path returning a small response adapter exposing
  ``.status_code`` / ``.content`` / ``.text`` / ``.json()`` (used for the player
  DOB XML).

One browser + one context + one page back the whole session, so the Incapsula
clearance is kept across calls. The Playwright **sync** API is single-thread
bound, so a single ``BrowserClient`` must be driven from one thread; the
itftennis engine parallelises its per-tournament browser phase by running
several independent ``BrowserClient`` instances (one per worker thread), each
with its own Playwright loop, and opts the whole pool out of Django's
async-safety guard once via :func:`allow_async_unsafe`.

Parity with the curl client is preserved: every fetch is recorded in telemetry,
every final URL is SSRF-validated with :func:`assert_safe_url`, the proxy address
(which may embed credentials) is **never** logged, and blocks/challenges record
an honest error rather than fabricating data.

.. _patchright: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright
"""

import json
import os
import secrets
import shutil
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import unquote, urljoin, urlsplit

from parsel import Selector

from ._ssrf import UnsafeUrlError, assert_resolves_public, assert_safe_url
from .telemetry import redact_secrets

# Distinctive anti-bot BLOCK markers. NOTE: a bare ``_Incapsula_Resource``
# script tag is injected into *legitimate* itftennis pages too, so it is
# deliberately NOT a marker — only the block-page wording counts.
_BLOCK_MARKERS = (
    "request unsuccessful",  # Imperva/Incapsula block page
    "incapsula incident",
    "incident id:",
    "request blocked",
    "access denied",
    "attention required",  # Cloudflare
    "just a moment",
    "captcha-delivery",  # DataDome
    "are you a human",
)
# Statuses that, on their own, signal a block even with an empty/odd body.
_BLOCK_STATUSES = frozenset({401, 403, 405, 406, 429, 503})

# Chromium flags needed to run headless in a sandboxed container.
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


def browser_proxy(proxy, *, session=None):
    """Translate a :class:`accounts.models.Proxy` into a Playwright proxy dict.

    Mirrors :func:`accounts.live_scrapers._http.build_proxies`' activation rule
    (active **and** a non-empty address) and returns ``None`` for a direct
    connection. The address may embed ``user:pass`` credentials, which are split
    into Playwright's separate ``username`` / ``password`` keys and are **never**
    logged.

    ``session`` enables per-request IP rotation: rotating/sticky residential
    endpoints usually key their exit IP off a token in the username
    (e.g. ``customer-user-session-{session}``). When given, every ``{session}``
    / ``{rand}`` placeholder in the address is replaced with a fresh token so a
    new launch earns a new IP. A rotating *gateway* (no placeholder) rotates per
    connection on its own, so the substitution is a harmless no-op there.
    """
    if not (proxy and proxy.is_active and (proxy.address or "").strip()):
        return None
    addr = proxy.address.strip()
    if session:
        addr = addr.replace("{session}", session).replace("{rand}", session)
    if "://" not in addr:
        addr = "http://" + addr
    parts = urlsplit(addr)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server = f"{server}:{parts.port}"
    out = {"server": server}
    if parts.username:
        out["username"] = unquote(parts.username)
    if parts.password:
        out["password"] = unquote(parts.password)
    return out


class _ApiResponse:
    """Minimal response adapter matching the curl client's read surface."""

    __slots__ = ("status_code", "content", "_text")

    def __init__(self, status, body, text):
        self.status_code = status
        self.content = body or b""
        self._text = text or ""

    @property
    def text(self):
        return self._text

    def json(self):
        return json.loads(self.content)


@contextmanager
def allow_async_unsafe():
    """Opt a whole block out of Django's async-safety guard (process-global).

    Playwright's sync API runs an asyncio loop in the calling thread, so Django
    rejects every ORM call (the log/telemetry RunLogLine writes) made while a
    browser is open with SynchronousOnlyOperation. ``DJANGO_ALLOW_ASYNC_UNSAFE``
    is the official escape hatch, but it is a *process-global* env var: when the
    browser phase fans out across threads (one :class:`BrowserClient` per
    thread) it must be set ONCE around the entire pool, not per browser. A
    per-instance set/restore races — one thread's teardown would unset it while
    another thread's ORM write is still in flight. Wrap the concurrent phase in
    this and construct each client with ``manage_async_unsafe=False``.
    """
    prev = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
        else:
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = prev


class BrowserClient:
    """A single patchright Chromium session exposing the engine's read surface.

    Use as a context manager. A single instance is not thread-safe — drive it
    from one thread. Concurrency is achieved with several instances (one per
    thread); see :func:`allow_async_unsafe` for the shared-env-var caveat.
    """

    def __init__(
        self,
        *,
        log,
        tele,
        proxy=None,
        allowed_hosts=None,
        nav_timeout=45000,
        api_timeout=30000,
        settle_timeout=20000,
        api_tries=3,
        headless=True,
        channel=None,
        user_data_dir=None,
        rotate_proxy_session=False,
        manage_async_unsafe=True,
    ):
        self.log = log
        self.tele = tele
        self.proxy = proxy
        self.allowed_hosts = tuple(allowed_hosts) if allowed_hosts else None
        self.nav_timeout = nav_timeout
        self.api_timeout = api_timeout
        self.settle_timeout = settle_timeout
        self.api_tries = max(1, api_tries)
        self.headless = headless
        self.channel = (channel or "").strip() or None
        self.user_data_dir = (user_data_dir or "").strip() or None
        self.rotate_proxy_session = bool(rotate_proxy_session)
        # When False the caller owns DJANGO_ALLOW_ASYNC_UNSAFE for the whole
        # (possibly concurrent) browser phase via allow_async_unsafe(); when True
        # (the default, for a lone sequential client) this instance manages it.
        self.manage_async_unsafe = bool(manage_async_unsafe)
        self._session_token = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._profile_dir = None
        self._owns_profile_dir = False

    # -- lifecycle -------------------------------------------------------
    def __enter__(self):
        try:
            from patchright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001 - surfaced as honest failure
            raise RuntimeError(
                f"patchright is not importable: {exc.__class__.__name__}: {exc}"
            ) from exc

        # --- choose engine: real Chrome channel (most stealthy) else Chromium --
        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        launch_kwargs = {
            "headless": self.headless,
            "chromium_sandbox": False,
            # Context options accepted by launch_persistent_context:
            "ignore_https_errors": True,
            # service_workers="block": SW-originated requests bypass
            # context.route(), so block SWs to keep the SSRF guard authoritative.
            "service_workers": "block",
            # patchright stealth guidance: don't override the real window size.
            "no_viewport": True,
        }
        if self.channel:
            # A real browser channel (e.g. Google "chrome") — patchright resolves
            # the install itself. Keep the arg list empty so the fingerprint stays
            # as close to a vanilla Chrome launch as possible (patchright warns
            # against extra automation flags).
            launch_kwargs["channel"] = self.channel
            engine = f"Google {self.channel.title()}"
        else:
            # Replit/Nix ships only Chromium; pin its path. In a sandboxed
            # container it needs these stability flags. (On Windows with no
            # channel set, leave the path unset so patchright uses its bundled
            # browser from `patchright install chromium`.)
            if chromium_path:
                launch_kwargs["executable_path"] = chromium_path
            launch_kwargs["args"] = list(LAUNCH_ARGS)
            engine = "Chromium"
        # A fresh session token per launch forces a new exit IP from a
        # sticky-session residential proxy (substituted into a {session}
        # placeholder in the address); a no-op for direct / rotating-gateway.
        self._session_token = (
            secrets.token_hex(8) if self.rotate_proxy_session else None
        )
        proxy = browser_proxy(self.proxy, session=self._session_token)
        if proxy:
            launch_kwargs["proxy"] = proxy
            kind = (
                self.proxy.get_kind_display()
                if hasattr(self.proxy, "get_kind_display")
                else "?"
            )
            conn = f"via {kind} proxy '{getattr(self.proxy, 'name', '?')}'"
            if self.rotate_proxy_session:
                conn += " (rotating IP)"
        else:
            conn = "direct \u2014 no proxy"
        mode = "headless" if self.headless else "headed"
        persist = "persistent profile" if self.user_data_dir else "ephemeral profile"
        self.log(
            "INFO",
            f"\U0001f310 HTTP client: patchright {engine} ({mode}, {persist}) {conn}",
        )

        # Playwright's sync API drives an asyncio event loop in this thread, so
        # Django's async-safety guard rejects every ORM call the scrape makes
        # while the browser is open (the log/telemetry RunLogLine writes) with
        # SynchronousOnlyOperation. DJANGO_ALLOW_ASYNC_UNSAFE is the official
        # opt-out. It is *process-global*, so when several BrowserClients run
        # concurrently (one per thread) the caller must set it ONCE around the
        # whole pool via allow_async_unsafe() and construct each client with
        # manage_async_unsafe=False — a per-instance set/restore would race (one
        # thread's teardown unsets it under another still-running thread). A lone
        # sequential client manages it itself and restores it on teardown.
        if self.manage_async_unsafe:
            self._prev_async_unsafe = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
            os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"
        try:
            # Resolve the profile directory (inside the try so a rare makedirs /
            # mkdtemp failure still runs close() and restores the env var above).
            # A caller-supplied (stable, per-scraper) dir lets Incapsula clearance
            # cookies survive between runs — the whole point of "persistent
            # profile". Without one we use a throwaway temp dir that close()
            # deletes. launch_persistent_context needs *some* dir either way.
            if self.user_data_dir:
                self._profile_dir = self.user_data_dir
                os.makedirs(self._profile_dir, exist_ok=True)
                self._clear_stale_singleton_locks(self._profile_dir)
            else:
                self._profile_dir = tempfile.mkdtemp(prefix="mm-browser-")
                self._owns_profile_dir = True

            self._pw = sync_playwright().start()
            # launch_persistent_context returns the BrowserContext directly (no
            # separate Browser): patchright's recommended, most-stealthy path.
            # Every SSRF guard attaches to this context exactly as before.
            self._context = self._pw.chromium.launch_persistent_context(
                self._profile_dir, **launch_kwargs
            )
            self._context.set_default_timeout(self.api_timeout)
            self._context.route("**/*", self._route_guard)
            self._guard_websockets()
            # A persistent context opens with a default page; reuse it.
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        # launch_persistent_context has no separate Browser object — closing the
        # context tears the whole session (and its Chrome process) down.
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._pw and self._pw.stop(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._page = self._context = self._browser = self._pw = None
        # Delete the throwaway profile dir only when we created a temp one; a
        # caller-supplied persistent dir is kept so its cookies survive.
        if self._owns_profile_dir and self._profile_dir:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
        self._profile_dir = None
        self._owns_profile_dir = False
        prev = getattr(self, "_prev_async_unsafe", "__unset__")
        if prev != "__unset__":
            if prev is None:
                os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
            else:
                os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = prev
            self._prev_async_unsafe = "__unset__"

    @staticmethod
    def _clear_stale_singleton_locks(profile_dir):
        """Remove a previous crash's Chrome singleton locks before relaunch.

        A persistent profile left behind by a killed/crashed run keeps
        ``SingletonLock``/``SingletonCookie``/``SingletonSocket`` entries that
        make Chrome refuse to start ("profile appears to be in use"). At most one
        run per scraper is ever in flight (DB constraint), so any lock seen here
        is stale and safe to clear. Best-effort.
        """
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.unlink(os.path.join(profile_dir, name))
            except OSError:
                pass

    # -- helpers ---------------------------------------------------------
    def _safe(self, url):
        try:
            assert_safe_url(url, allowed_hosts=self.allowed_hosts)
            return True
        except UnsafeUrlError as exc:
            self.log(
                "WARN",
                redact_secrets(f"\U0001f6e1\ufe0f blocked unsafe URL {url}: {exc}"),
            )
            self.tele.record_error(
                redact_secrets(f"Unsafe URL blocked for {url}: {exc}")
            )
            return False

    @staticmethod
    def _looks_blocked(status, body_text):
        if status in _BLOCK_STATUSES:
            return True
        sample = (body_text or "")[:8192].lower()
        return any(marker in sample for marker in _BLOCK_MARKERS)

    def _route_guard(self, route):
        """SSRF guard for browser-issued requests (``page.goto`` & subresources).

        ``context.request`` calls bypass page routes — their redirects are
        validated in :meth:`_fetch` instead. This covers navigations, the
        redirect hops the browser follows on its own, and subresources:

        * the top-level document (and any redirect of it) must stay on the host
          allowlist — parity with ``ScraperClient``'s per-hop check;
        * every other request merely must not resolve to a private address;
        * heavy assets (image/media/font) are dropped for speed.

        It **fails closed**: any unexpected error aborts the request, since a
        guard bug must never silently widen the SSRF boundary.
        """
        req = route.request
        decision = "abort"  # fail closed
        try:
            if req.resource_type in ("image", "media", "font"):
                decision = "abort"
            else:
                main_nav = (
                    req.is_navigation_request() and req.frame.parent_frame is None
                )
                if urlsplit(req.url).scheme not in ("http", "https"):
                    # data:/blob: are browser-internal (no SSRF surface) and may
                    # load as subresources, but never as a navigation target.
                    decision = "abort" if main_nav else "continue"
                else:
                    hosts = self.allowed_hosts if main_nav else None
                    try:
                        assert_safe_url(req.url, allowed_hosts=hosts)
                        decision = "continue"
                    except UnsafeUrlError as exc:
                        decision = "abort"
                        self.log(
                            "WARN",
                            redact_secrets(
                                f"\U0001f6e1\ufe0f blocked unsafe request "
                                f"{req.url}: {exc}"
                            ),
                        )
                        self.tele.record_error(
                            redact_secrets(
                                f"Unsafe request blocked: {req.url}: {exc}"
                            )
                        )
        except Exception as exc:  # noqa: BLE001 - fail closed on any guard error
            decision = "abort"
            self.log(
                "WARN",
                redact_secrets(
                    f"\U0001f6e1\ufe0f guard error \u2014 aborting "
                    f"{getattr(req, 'url', '?')}: {exc}"
                ),
            )
        try:
            route.continue_() if decision == "continue" else route.abort()
        except Exception:  # noqa: BLE001 - route already handled / page closing
            return None

    def _guard_websockets(self):
        """SSRF guard for WebSocket handshakes (not covered by ``context.route``).

        ``context.route("**/*")`` does not intercept WebSocket connections, so a
        page on the allowlisted host could still open a ``ws://`` to an internal
        service. Register a dedicated ws route (when the runtime supports it)
        that closes any handshake whose host fails the public-IP check and
        transparently proxies the safe ones. Best-effort: a runtime without the
        API just logs and continues (the http(s) surface stays guarded).
        """
        route_ws = getattr(self._context, "route_web_socket", None)
        if route_ws is None:
            return

        def _ws_guard(ws_route):
            url = getattr(ws_route, "url", "") or ""
            try:
                host = (urlsplit(url).hostname or "").lower()
                if (
                    not host
                    or host == "localhost"
                    or host.endswith(".local")
                    or host.endswith(".internal")
                ):
                    raise UnsafeUrlError(f"websocket host {host!r} is not allowed")
                assert_resolves_public(host)
            except Exception as exc:  # noqa: BLE001 - any failure denies the ws
                self.log(
                    "WARN",
                    redact_secrets(
                        f"\U0001f6e1\ufe0f blocked websocket {url}: {exc}"
                    ),
                )
                self.tele.record_error(
                    redact_secrets(f"Unsafe websocket blocked: {url}: {exc}")
                )
                try:
                    ws_route.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                ws_route.connect_to_server()
            except Exception:  # noqa: BLE001 - if passthrough fails, deny safely
                try:
                    ws_route.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            route_ws("**/*", _ws_guard)
        except Exception as exc:  # noqa: BLE001 - unsupported runtime
            self.log(
                "INFO",
                redact_secrets(f"\u2139\ufe0f websocket guard unavailable: {exc}"),
            )

    # -- read surface ----------------------------------------------------
    def _settle(self):
        """Block until the page has actually finished loading.

        ``goto(wait_until="domcontentloaded")`` returns the moment the HTML is
        parsed \u2014 before subresources and JS/XHR-driven content render \u2014 so
        reading ``page.content()`` straight after gets a half-loaded document.
        Wait for the full ``load`` event and then ``networkidle`` so the read
        sees a fully-rendered page. Both waits are **tolerant**: a page with
        persistent polling/websockets never reaches ``networkidle``, and that
        must not fail the read, so a timeout just falls through to whatever has
        loaded so far.
        """
        for state in ("load", "networkidle"):
            try:
                self._page.wait_for_load_state(
                    state, timeout=self.settle_timeout
                )
            except Exception:  # noqa: BLE001 - tolerate a page that never settles
                pass

    def get_selector(self, url, **kwargs):
        """Navigate to ``url`` (solving any challenge) and return a Selector."""
        if not self._safe(url):
            return None
        start = time.time()
        try:
            resp = self._page.goto(
                url, wait_until="domcontentloaded", timeout=self.nav_timeout
            )
            self._settle()
            html = self._page.content()
            status = resp.status if resp is not None else None
            if self._looks_blocked(status, html):
                # Incapsula's JS interstitial auto-resolves and reloads; give it
                # one beat, let the reloaded page fully settle, then re-read.
                try:
                    self._page.wait_for_timeout(4000)
                    self._settle()
                except Exception:  # noqa: BLE001 - tolerate a flaky settle
                    pass
                html = self._page.content()
                if not self._looks_blocked(None, html):
                    status = 200
            size = len(html.encode("utf-8", "ignore"))
            self.tele.record_request(
                url=url, method="GET", status=status, size=size,
                duration_ms=(time.time() - start) * 1000,
            )
            if self._looks_blocked(status, html):
                self.log(
                    "WARN",
                    f"\U0001f6e1\ufe0f GET {url} \u2192 anti-bot challenge "
                    f"(HTTP {status})",
                )
                self.tele.record_error(
                    redact_secrets(f"Anti-bot challenge for {url} (HTTP {status})")
                )
                return None
            if status is not None and 200 <= status < 300:
                return Selector(text=html)
            self.tele.record_error(
                redact_secrets(f"Page load for {url} \u2192 HTTP {status}")
            )
            return None
        except Exception as exc:  # noqa: BLE001 - log, record, honest fail
            self.tele.record_request(
                url=url, method="GET", status=None, size=0,
                duration_ms=(time.time() - start) * 1000,
            )
            self.log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f GET {url} \u2192 {exc.__class__.__name__}: {exc}"
                ),
            )
            self.tele.record_error(
                redact_secrets(f"Page load failed for {url}: {exc}"), exc=exc
            )
            return None

    def _fetch(self, url, *, params, headers):
        """One API GET with manual, SSRF-validated redirect following.

        ``context.request`` bypasses the page route guard, so redirects are
        followed by hand here and every hop is re-validated with
        :func:`assert_safe_url` (parity with ``ScraperClient``).
        """
        current, cur_params = url, params
        for _hop in range(6):
            resp = self._context.request.get(
                current,
                params=cur_params or None,
                headers=headers or None,
                max_redirects=0,
                timeout=self.api_timeout,
            )
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    return resp
                nxt = urljoin(current, loc)
                assert_safe_url(nxt, allowed_hosts=self.allowed_hosts)
                current, cur_params = nxt, None
                continue
            return resp
        raise RuntimeError(f"too many redirects for {url}")

    def _api(self, url, *, params=None, headers=None):
        """Fetch a JSON/XML API endpoint via the browser context (shares cookies)."""
        if not self._safe(url):
            return None
        last_exc = None
        for attempt in range(1, self.api_tries + 1):
            start = time.time()
            try:
                resp = self._fetch(url, params=params, headers=headers)
                body = resp.body()
                status = resp.status
                try:
                    text = resp.text()
                except Exception:  # noqa: BLE001 - body may be binary
                    text = ""
                self.tele.record_request(
                    url=url, method="GET", status=status, size=len(body),
                    duration_ms=(time.time() - start) * 1000,
                )
                if self._looks_blocked(status, text):
                    self.log(
                        "WARN",
                        f"\U0001f6e1\ufe0f GET {url} \u2192 anti-bot challenge "
                        f"(HTTP {status}, retry {attempt}/{self.api_tries})",
                    )
                    last_exc = RuntimeError(f"anti-bot challenge (HTTP {status})")
                else:
                    return _ApiResponse(status, body, text)
            except UnsafeUrlError as exc:
                self.tele.record_request(
                    url=url, method="GET", status=None, size=0,
                    duration_ms=(time.time() - start) * 1000,
                )
                self.log(
                    "WARN",
                    redact_secrets(
                        f"\U0001f6e1\ufe0f blocked unsafe redirect from {url}: {exc}"
                    ),
                )
                self.tele.record_error(
                    redact_secrets(f"Unsafe redirect blocked from {url}: {exc}")
                )
                return None  # an SSRF-blocked redirect won't improve on retry
            except Exception as exc:  # noqa: BLE001 - record and retry
                self.tele.record_request(
                    url=url, method="GET", status=None, size=0,
                    duration_ms=(time.time() - start) * 1000,
                )
                self.log(
                    "WARN",
                    redact_secrets(
                        f"\u26a0\ufe0f GET {url} \u2192 "
                        f"{exc.__class__.__name__}: {exc} "
                        f"(retry {attempt}/{self.api_tries})"
                    ),
                )
                last_exc = exc
            if attempt < self.api_tries:
                time.sleep(0.8 * attempt)
        self.tele.record_error(
            redact_secrets(f"Request failed for {url}: {last_exc}"),
            exc=last_exc if isinstance(last_exc, BaseException) else None,
        )
        return None

    def get(self, url, **kwargs):
        return self._api(
            url, params=kwargs.get("params"), headers=kwargs.get("headers")
        )

    def get_json(self, url, **kwargs):
        resp = self._api(
            url, params=kwargs.get("params"), headers=kwargs.get("headers")
        )
        if resp is not None and 200 <= resp.status_code < 300:
            try:
                return resp.json()
            except Exception:  # noqa: BLE001 - body wasn't JSON
                return None
        return None
