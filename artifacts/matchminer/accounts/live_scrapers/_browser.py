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
* ``get_json(url, params=, headers=)`` — a JSON API call issued as an in-page
  ``fetch()`` (so it inherits the page's solved Incapsula clearance and real
  browser fingerprint, not just its cookies), parsed, or ``None``;
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
from urllib.parse import unquote, urlencode, urlsplit

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
        api_tries=None,
        headless=True,
        channel=None,
        user_data_dir=None,
        rotate_proxy_session=False,
        manage_async_unsafe=True,
        announce=True,
    ):
        self.log = log
        self.tele = tele
        self.proxy = proxy
        self.allowed_hosts = tuple(allowed_hosts) if allowed_hosts else None
        self.nav_timeout = nav_timeout
        self.api_timeout = api_timeout
        self.settle_timeout = settle_timeout
        # Inherit the run's per-request try budget (Scraper.max_tries) set by the
        # worker, so the browser API-fetch retries honour the same setting as the
        # curl_cffi client; an explicit api_tries still wins.
        if api_tries is None:
            from ._http import get_default_tries
            api_tries = get_default_tries()
        self.api_tries = max(1, int(api_tries))
        self.headless = headless
        self.channel = (channel or "").strip() or None
        self.user_data_dir = (user_data_dir or "").strip() or None
        self.rotate_proxy_session = bool(rotate_proxy_session)
        # When False the caller owns DJANGO_ALLOW_ASYNC_UNSAFE for the whole
        # (possibly concurrent) browser phase via allow_async_unsafe(); when True
        # (the default, for a lone sequential client) this instance manages it.
        self.manage_async_unsafe = bool(manage_async_unsafe)
        # When False this client stays quiet about its own launch (engine/mode/
        # proxy). A run that spins up many browsers (per-request rotation) would
        # otherwise repeat the identical "HTTP client:" line once per launch and
        # bury the actual scrape output — so the engine announces it ONCE itself.
        self.announce = bool(announce)
        self._session_token = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._profile_dir = None
        self._owns_profile_dir = False

    # -- lifecycle -------------------------------------------------------
    def __enter__(self):
        # The async-unsafe env var is owned for the whole session lifetime: set
        # once here, restored once in close(). It must survive a mid-session
        # relaunch(), so it lives here rather than in the shared _launch() body.
        #
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
            self._launch(announce=self.announce)
        except Exception:
            self.close()
            raise
        return self

    def relaunch(self):
        """Shed the current identity and open a fresh browser (a new session
        token → a new exit IP on a rotating proxy, plus a fresh ephemeral
        profile) **without** releasing the async-unsafe env var the surrounding
        phase owns. Used mid-tournament to escape an accumulated anti-bot
        challenge; re-solving the page challenge afterwards (navigate to a
        cleared URL) is the caller's job. On failure the exception propagates and
        the (now browser-less) client returns honest failures until close()."""
        self._teardown_browser()
        self._launch(announce=False)
        return self

    def _launch(self, *, announce=False):
        """Open a fresh Chromium context (engine, proxy/session, profile, page).

        Shared by :meth:`__enter__` and :meth:`relaunch`. A new session token
        here forces a new exit IP from a sticky/rotating proxy. Does **not**
        touch the process-global async-unsafe env var (owned once by __enter__).
        On failure it propagates; the caller decides whether to ``close()``.
        """
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
        if announce:
            self.log(
                "INFO",
                f"\U0001f310 HTTP client: patchright {engine} ({mode}, {persist}) "
                f"{conn}",
            )

        # Resolve the profile directory. A caller-supplied (stable, per-scraper)
        # dir lets Incapsula clearance cookies survive between runs — the whole
        # point of "persistent profile". Without one we use a throwaway temp dir
        # that _teardown_browser() deletes. In the default rotate-per-request
        # path the client carries NO user_data_dir, so every relaunch() lands in
        # the else-branch below: a fresh ephemeral profile (sheds cookies) plus
        # the fresh session token minted above (a new exit IP on a rotating
        # proxy) — exactly what DOB rate-challenge recovery needs. In the
        # non-rotate persistent path relaunch() instead reuses this same dir and
        # mints no new token, so it keeps the same IP/cookies and is effectively
        # a no-op for challenge recovery (a documented limitation; that path is
        # not the production DOB path). launch_persistent_context needs *some*
        # dir either way. Exceptions propagate to the caller (__enter__ calls
        # close(); relaunch() leaves the client browser-less, returning honest
        # failures until close()).
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

    def __exit__(self, *exc):
        self.close()

    def close(self):
        # Tear the browser down, then release the async-unsafe env var this client
        # owns (set once in __enter__). relaunch() reuses only the first half via
        # _teardown_browser() so the env var survives the rotation.
        self._teardown_browser()
        prev = getattr(self, "_prev_async_unsafe", "__unset__")
        if prev != "__unset__":
            if prev is None:
                os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
            else:
                os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = prev
            self._prev_async_unsafe = "__unset__"

    def _teardown_browser(self):
        # launch_persistent_context has no separate Browser object — closing the
        # context tears the whole session (and its Chrome process) down. Leaves
        # the async-unsafe env-var state alone (close() owns that) so it is safe
        # to call from relaunch(). Safe to call repeatedly / on a half-open
        # client.
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

        This covers every request the page issues — top-level navigations, the
        in-page ``fetch()`` API calls (see :meth:`_fetch`), the redirect hops
        the browser follows on its own, and subresources:

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
        """One API GET issued **from inside the page's JS context**.

        The bare ``context.request`` client shares the context's cookies but runs
        *outside* the page, so Imperva/Incapsula still anti-bot-challenges it (a
        tiny interstitial body returned with HTTP 200) even after ``page.goto``
        cleared the page's own challenge. Issuing the request as an in-page
        ``fetch()`` runs it in the already-cleared browser JS context — same
        origin as the just-loaded tournament page, carrying the solved clearance
        cookies/token **and** the real browser fingerprint (UA, Referer,
        ``sec-fetch-*`` headers the bare client can't reproduce) — so the API
        returns real JSON/XML instead of a challenge.

        SSRF parity: the fully-resolved target is validated here up front, and
        every network hop the in-page fetch then makes (including redirects) is
        re-checked by :meth:`_route_guard` via ``context.route`` — which fails
        closed on any non-public address.

        Returns ``(status, body_bytes, text)``.
        """
        full_url = url
        if params:
            qs = urlencode(
                {k: ("" if v is None else str(v)) for k, v in dict(params).items()}
            )
            if qs:
                full_url = f"{url}{'&' if '?' in url else '?'}{qs}"
        # Validate the resolved target before handing it to the page; the route
        # guard re-validates each redirect hop the browser then follows.
        assert_safe_url(full_url, allowed_hosts=self.allowed_hosts)
        result = self._page.evaluate(
            """async ({ url, headers }) => {
                try {
                    const r = await fetch(url, {
                        method: 'GET',
                        headers: headers || {},
                        credentials: 'include',
                        redirect: 'follow',
                    });
                    return { ok: true, status: r.status, body: await r.text() };
                } catch (e) {
                    return { ok: false, status: 0, body: '', error: String(e) };
                }
            }""",
            {"url": full_url, "headers": headers or {}},
        )
        if not result.get("ok"):
            # A network-level failure (DNS, abort by the route guard, CORS) —
            # surface it so _api records/retries, never a silent empty body.
            raise RuntimeError(
                f"in-page fetch failed: {result.get('error') or 'unknown error'}"
            )
        text = result.get("body") or ""
        status = int(result.get("status") or 0)
        return status, text.encode("utf-8", "ignore"), text

    def _api(self, url, *, params=None, headers=None):
        """Fetch a JSON/XML API endpoint via an in-page fetch (inherits clearance)."""
        if not self._safe(url):
            return None
        last_exc = None
        for attempt in range(1, self.api_tries + 1):
            start = time.time()
            try:
                status, body, text = self._fetch(url, params=params, headers=headers)
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
