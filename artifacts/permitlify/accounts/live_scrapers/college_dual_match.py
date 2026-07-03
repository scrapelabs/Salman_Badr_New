"""College Dual Match (AI) scraper.

Ports the production ``college_dual_match_scraper_ai`` spider onto MatchMiner's
shared HTTP client (:mod:`accounts.live_scrapers._http`) + telemetry. Unlike the
other ports this one is **intentionally AI-core**: the box-score recaps it reads
are unstructured HTML/PDF, so the extraction is delegated to **Anthropic Claude**
(the AI step is preserved on purpose, not stripped).

Input is a **URL** (``params["tournament_url"]`` — a single URL or a list). The
URL is classified the way the source does:

1. ``docs.google.com`` → a Google Sheet of ``Team`` / ``Link`` rows; each link is
   itself classified (schedule page or direct box score).
2. a URL containing ``/schedule`` → a team schedule page; it is crawled for
   "Box Score" / "Box Score (PDF)" / "PDF Box" links.
3. otherwise → a direct box-score recap (HTML or PDF).

Each discovered **box score** is fed to Claude (``POST
https://api.anthropic.com/v1/messages``): PDFs go up as base64 with media type
``application/pdf``; HTML is first **cleaned** (scripts/styles/nav chrome
stripped) and **chunked** at :data:`CLAUDE_HTML_CHUNK` chars, then each chunk is
sent and the per-chunk match lists merged — exactly like the source's
``claude_extractor._extract_text``. A raw box-score page is ~490 KB of ~96 %
noise; cleaning reduces it to ~17 KB of pure match markup so Claude stops losing
score lines in the clutter. Claude returns a JSON array of match objects which
map 1:1 onto this scraper's bespoke output columns. There is **no deterministic
fallback** — Claude is the only parser.

Credentials:

- The Anthropic key (**required**), resolved in order: the per-scraper key
  (Lab -> Settings tab) -> the workspace ``GeneralConfig`` key (Settings page)
  -> the env-sourced ``settings.CLAUDE_KEYS`` (``CLAUDE_KEYS`` /
  ``ANTHROPIC_API_KEY``). The run fails honestly up front when no key (or no
  prompt) is configured. A key is chosen with ``random.choice`` per worker
  (mirrors the source's rotation) and is **never** logged.
- ``settings.OPENAI_API_KEY`` — **optional**. Used only to recover a missing
  ``tournament_date``. When unset that step is skipped gracefully and the field
  is left as-is.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import base64
import csv
import io
import json
import os
import queue
import random
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

from django.conf import settings
from django.db.models import F
from parsel import Selector

from accounts import college_store
from accounts.models import Run

from ._http import RETRY_STATUSES, ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"
# A standard 9-line dual already uses ~3.1k output tokens, so the source's 4096
# leaves almost no headroom — an exhibition line or a combined men+women page
# would truncate the JSON array (silently dropping matches). 8192 gives ample
# room; ``_claude_request`` also flags any ``max_tokens`` stop so a truncation is
# never silent (correct-data-trumps-source-parity, per the user's preference).
CLAUDE_MAX_TOKENS = 8192
# Cleaned box-score HTML is split into chunks of this many chars before Claude
# (source parity with ``claude_extractor``); a single dual match cleans to
# ~17 KB so it is normally one chunk, but oversized pages split instead of being
# silently truncated at the context window.
CLAUDE_HTML_CHUNK = 160_000
# Anthropic returns 529 ("overloaded") under load; retry it alongside the usual
# transient/rate-limit statuses.
CLAUDE_RETRY_STATUSES = frozenset(RETRY_STATUSES | {529})
# Claude (especially on a multi-page PDF) can take well past the default 30 s.
CLAUDE_TIMEOUT = 180

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_DATE_MODEL = "gpt-4o-mini"
OPENAI_HELPER_MODEL = "gpt-4.1-mini"

# Loaded once from the prompt data file that ships next to this module.
PROMPT_PATH = os.path.join(os.path.dirname(__file__), "college_dual_match_prompt.txt")

# Inline tournament-date prompt (ported from the source's
# ``tournament_dates_prompt.txt``); only used for the optional OpenAI fallback.
TOURNAMENT_DATE_PROMPT = (
    "You are a data extraction assistant. Your task is to extract the "
    "tournament date from the provided text or HTML content.\n\n"
    "You MUST return ONLY a valid JSON object. No text before it. No text "
    "after it. No markdown. No code fences. No explanations. JSON only.\n\n"
    "The JSON object MUST always contain exactly this key:\n\n"
    "-   tournament_date\n\n"
    "Rules:\n\n"
    "1. The key MUST always be present in the output, even if the value is "
    "unknown.\n"
    "2. If a date is found, format it as \"YYYY-MM-DD\".\n"
    "3. If a date range is found, use the start date.\n"
    "4. If a date cannot be determined, use \"\" (empty string).\n"
    "5. NEVER use null. NEVER omit the key. NEVER add extra keys.\n"
    "6. NEVER wrap the JSON in markdown code fences or backticks.\n"
    "7. NEVER add any text, explanation, or commentary outside the JSON.\n"
)

# Realistic page-fetch headers (the source's Chrome/Edge fingerprint).
BROWSER_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "en-US,en;q=0.9,fr;q=0.8,en-GB;q=0.7,ar;q=0.6",
    "upgrade-insecure-requests": "1",
}

# HTML nodes the source strips before handing markup to Claude.
_NOISE_TAGS = (
    "script", "style", "link", "meta", "noscript", "iframe", "svg", "img",
    "picture", "video", "audio", "canvas", "head", "footer", "nav", "aside",
)

# Bespoke output schema — the EXACT keys (in order) the prompt / source emit.
COLUMNS = [
    "tournament_date", "tournament_name", "tournament_gender",
    "draw_name", "draw_gender", "draw_team_type",
    "winner_1_name", "winner_2_name", "winner_1_gender", "winner_2_gender",
    "winner_1_college", "winner_2_college",
    "loser_1_name", "loser_2_name", "loser_1_gender", "loser_2_gender",
    "loser_1_college", "loser_2_college",
    "score", "winner_team", "loser_team", "team_score", "outcome",
]
HEADER = [c.replace("_", " ").title() for c in COLUMNS]


# ---------------------------------------------------------------------------
# Small URL / text helpers
# ---------------------------------------------------------------------------
def _load_prompt():
    """Read the Claude system prompt from the data file shipped beside us."""
    try:
        with open(PROMPT_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:  # noqa: BLE001 - a missing prompt file is handled by run()
        return ""


def _host(url, *, strip_www=False):
    """Return the lower-cased netloc of ``url`` (optionally without ``www.``)."""
    try:
        net = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001 - a malformed URL has no host
        return ""
    if strip_www and net.startswith("www."):
        net = net[4:]
    return net


def _normalize_url(url):
    """Ensure ``url`` has a scheme (defaults to https), mirroring the source."""
    url = (url or "").strip()
    if not url:
        return ""
    if not urlparse(url).scheme:
        return f"https://{url}"
    return url


def _parse_field(query, node):
    """Return ``normalize-space(query)`` against ``node`` (a selector), or ''."""
    try:
        return node.xpath(f"normalize-space({query})").get() or ""
    except Exception:  # noqa: BLE001 - a bad xpath/node is non-fatal
        return ""


def _clean_html(content):
    """Strip noise tags + collapse whitespace, returning cleaned HTML markup.

    The source uses BeautifulSoup; that dependency is absent here, so parsel's
    ``.drop()`` removes the same noise tags. Attribute pruning (a size
    optimization in the source) is skipped — the surviving
    ``href``/``class``/``id`` attributes actually help the model. Both the Claude
    box-score path (:func:`_claude_extract_html`) and the optional OpenAI
    tournament-date recovery clean the page this way before sending it.
    """
    try:
        sel = Selector(text=content)
        sel.css(", ".join(_NOISE_TAGS)).drop()
        cleaned = sel.get() or content
    except Exception:  # noqa: BLE001 - fall back to the raw content
        cleaned = content
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned


def _parse_json(text):
    """Best-effort JSON recovery from a model response (ported from source)."""
    if not isinstance(text, str):
        return text
    for pattern in (r"```(?:json)?\s*([\s\S]*?)\s*```", r"(\{[\s\S]*\}|\[[\s\S]*\])"):
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:  # noqa: BLE001 - try the next pattern
                pass
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001 - not JSON
        return text


def _as_list_of_dicts(value):
    """Coerce a parsed Claude result into a flat list of match dicts."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


# ---------------------------------------------------------------------------
# Page fetch via a headless browser (full JS render) with a curl fallback
# ---------------------------------------------------------------------------
class _HtmlResponse:
    """Adapt browser-rendered HTML to the curl client's read surface.

    A page rendered through the browser renderer then slots transparently into
    the same callers that consume a ``curl_cffi`` ``Response`` (they read
    ``.status_code`` / ``.text`` / ``.content`` / ``.headers``).
    """

    __slots__ = ("status_code", "text", "content", "headers")

    def __init__(self, html):
        self.status_code = 200
        self.text = html or ""
        self.content = (html or "").encode("utf-8", "ignore")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


# Full render attempts per URL before an honest failure. The user asked for
# "three retries in case a request failed" — three complete render attempts, the
# last one on a freshly relaunched browser context.
BROWSER_RENDER_TRIES = 3

# Sentinel pushed on the render queue to tell the owner thread to shut down.
_RENDER_STOP = object()


class _BrowserRenderer:
    """Headless-browser page renderer driven by a single owner thread.

    Every HTML page for this scraper is rendered by a real headless patchright
    Chromium (full JavaScript executed, the scraper's proxy attached) before its
    markup is handed to Claude — so Claude parses the fully-hydrated DOM, not a
    bare server response.

    Playwright's sync API is **bound to the thread that started it**, so the one
    browser cannot be shared across the phase-2 worker threads behind a mere lock
    (a ``goto`` from another thread crashes). Instead every render is marshaled
    onto one dedicated owner thread through a queue: the owner lazily launches a
    single :class:`BrowserClient` (persistent profile, so clearance cookies
    survive across pages *and* runs) and drives all renders; :meth:`render`
    blocks on a Future for the result.

    Because one Chromium is about all the container's memory can spare, renders
    serialize on the owner thread; the callers' Claude requests (the real
    bottleneck) still run concurrently across the worker pool. Each render is
    retried up to :data:`BROWSER_RENDER_TRIES` times — the final attempt on a
    freshly ``relaunch()``-ed context — before an honest ``None`` (per-attempt
    telemetry is recorded by ``get_selector``). Any launch/relaunch error is an
    honest failure too; content is never fabricated.
    """

    def __init__(self, *, scraper, log, tele, allowed_hosts, profile_dir):
        self._scraper = scraper
        self._log = log
        self._tele = tele
        self._allowed_hosts = allowed_hosts or None
        self._profile_dir = profile_dir
        self._queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._serve, name="college-browser-renderer", daemon=True
        )
        self._started = False
        self._start_lock = threading.Lock()

    def _ensure_started(self):
        with self._start_lock:
            if not self._started:
                self._started = True
                self._thread.start()

    def render(self, url):
        """Render ``url`` with full JS on the owner thread; HTML or ``None``."""
        self._ensure_started()
        fut = Future()
        self._queue.put((url, fut))
        return fut.result()

    def close(self):
        """Stop the owner thread and tear the browser down (idempotent)."""
        if not self._started:
            return
        self._queue.put(_RENDER_STOP)
        self._thread.join(timeout=30)

    # -- owner-thread internals ------------------------------------------
    def _serve(self):
        """Owner-thread loop: own one BrowserClient, render queued URLs."""
        try:
            from ._browser import BrowserClient
        except Exception as exc:  # noqa: BLE001 - patchright not importable
            self._tele.record_error(
                redact_secrets(f"Browser renderer unavailable: {exc}")
            )
            BrowserClient = None

        client = None
        try:
            while True:
                item = self._queue.get()
                if item is _RENDER_STOP:
                    break
                url, fut = item
                try:
                    if BrowserClient is None:
                        fut.set_result(None)
                        continue
                    if client is None:
                        client = self._launch(BrowserClient)
                    if client is None:
                        fut.set_result(None)
                        continue
                    fut.set_result(self._render(client, url))
                except Exception as exc:  # noqa: BLE001 - honest fail per render
                    # Complete the Future FIRST: render() blocks on fut.result()
                    # with no timeout, so if record_error itself ever raised the
                    # caller would hang until the inactivity reaper. Unblock, then
                    # record telemetry.
                    fut.set_result(None)
                    self._tele.record_error(
                        redact_secrets(f"Browser render error for {url}: {exc}"),
                        exc=exc,
                    )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass

    def _launch(self, browser_client_cls):
        """Open the one persistent-profile headless browser (owner thread)."""
        self._log(
            "INFO",
            "\U0001f310 launching headless browser (full JS render, "
            "persistent profile)",
        )
        try:
            client = browser_client_cls(
                log=self._log,
                tele=self._tele,
                proxy=getattr(self._scraper, "proxy", None),
                allowed_hosts=self._allowed_hosts,
                headless=getattr(settings, "SCRAPER_BROWSER_HEADLESS", True),
                channel=getattr(settings, "SCRAPER_BROWSER_CHANNEL", "") or None,
                user_data_dir=self._profile_dir,
            )
            client.__enter__()
            return client
        except Exception as exc:  # noqa: BLE001 - honest fail, never fabricate
            self._tele.record_error(
                redact_secrets(f"Browser launch failed: {exc}"), exc=exc
            )
            self._log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f browser launch failed: "
                    f"{exc.__class__.__name__}: {exc}"
                ),
            )
            return None

    def _render(self, client, url):
        """Render ``url`` with up to :data:`BROWSER_RENDER_TRIES` attempts."""
        for attempt in range(1, BROWSER_RENDER_TRIES + 1):
            if attempt == BROWSER_RENDER_TRIES and attempt > 1:
                # Final attempt on a fresh context. On the persistent profile
                # relaunch() reuses the same dir (clearance cookies survive) and
                # _launch clears any stale singleton locks; on failure the client
                # is left browser-less and returns honest Nones.
                try:
                    client.relaunch()
                except Exception as exc:  # noqa: BLE001 - honest None on failure
                    self._tele.record_error(
                        redact_secrets(
                            f"Browser relaunch failed for {url}: {exc}"
                        ),
                        exc=exc,
                    )
                    return None
            sel = client.get_selector(url)
            if sel is not None:
                html = sel.get()
                if html:
                    if attempt > 1:
                        self._log(
                            "INFO",
                            f"   \u2705 rendered {url} on attempt "
                            f"{attempt}/{BROWSER_RENDER_TRIES}",
                        )
                    return html
            if attempt < BROWSER_RENDER_TRIES:
                self._log(
                    "INFO",
                    f"   \U0001f501 render attempt {attempt}/"
                    f"{BROWSER_RENDER_TRIES} failed for {url} \u2014 retrying",
                )
        return None


def _get_page(client, url, log, tele, *, renderer=None, headers=BROWSER_HEADERS):
    """Fetch a page for extraction.

    HTML pages are rendered by the headless browser (full JavaScript executed,
    the scraper's proxy attached) so the fully-hydrated markup — not a bare
    server response — is what reaches Claude. Direct ``.pdf`` URLs skip the
    browser (a ``goto`` on a PDF aborts as a download); they and the
    browser-unavailable / render-failed cases fall back to a single curl_cffi
    GET, which also keeps the "response body is itself a PDF" path in
    :func:`_core_extract` working and yields an honest status code.

    Returns a response-like object exposing ``.status_code`` / ``.text`` /
    ``.content`` / ``.headers`` — an :class:`_HtmlResponse` wrapping the rendered
    HTML on success, else the curl ``Response`` (or ``None``).
    """
    path = urlparse(url).path.lower()
    if renderer is not None and not path.endswith(".pdf"):
        html = renderer.render(url)
        if html:
            return _HtmlResponse(html)
    return client.get(url, headers=headers)


# ---------------------------------------------------------------------------
# Anthropic Claude (the AI core)
# ---------------------------------------------------------------------------
def _claude_request(client, key, system, log, tele, *, content=None, text=None):
    """POST one message to Claude and return the parsed JSON, or ``None``.

    The API key lives only in the request header — it is never logged. Any error
    text is scrubbed with :func:`redact_secrets` before it reaches telemetry.
    """
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": content if content else text}],
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = client.post(
        CLAUDE_API_URL,
        headers=headers,
        json=payload,
        timeout=CLAUDE_TIMEOUT,
        retry_statuses=CLAUDE_RETRY_STATUSES,
    )
    if resp is None:
        tele.record_error("Claude request failed (no response).")
        return None
    if resp.status_code != 200:
        snippet = (resp.text or "")[:500]
        tele.record_error(
            redact_secrets(f"Claude API error HTTP {resp.status_code}: {snippet}")
        )
        log("WARN", f"\u26a0\ufe0f Claude API HTTP {resp.status_code}")
        return None
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - body wasn't JSON
        tele.record_error("Claude response was not valid JSON.")
        return None
    try:
        usage = data.get("usage", {}) or {}
        log(
            "INFO",
            f"   \U0001f4ca Claude usage: {usage.get('input_tokens', 0)} in + "
            f"{usage.get('output_tokens', 0)} out",
        )
    except Exception:  # noqa: BLE001 - usage logging is best-effort
        pass
    # A ``max_tokens`` stop means the JSON array was cut off mid-flight, so
    # ``_parse_json`` will fail and this chunk yields zero matches. Surface it
    # loudly (error + WARN) instead of silently dropping scores.
    if data.get("stop_reason") == "max_tokens":
        tele.record_error(
            "Claude output truncated at max_tokens \u2014 some matches may be "
            "missing from this box score."
        )
        log(
            "WARN",
            "\u26a0\ufe0f Claude output hit max_tokens \u2014 response truncated "
            "(some matches may be missing)",
        )
    blocks = data.get("content") or []
    raw_text = blocks[0].get("text", "") if blocks else ""
    return _parse_json(raw_text)


def _claude_extract_pdf(client, key, system, pdf_bytes, log, tele):
    """Send a PDF (base64) to Claude and return a list of match dicts."""
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode()
    log("INFO", "   \u23f3 Sending PDF to Claude\u2026")
    parsed = _claude_request(
        client, key, system, log, tele,
        content=[
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": pdf_b64,
                },
            },
            {"type": "text", "text": "Extract as instructed. Return ONLY valid JSON."},
        ],
    )
    return _as_list_of_dicts(parsed)


def _claude_extract_html(client, key, system, content, log, tele):
    """Clean the page HTML, chunk it, extract with Claude and merge (source parity).

    Mirrors the source's ``claude_extractor._extract_text``: the raw box-score
    page (~490 KB, ~96 % scripts/styles/nav chrome) is first reduced to the match
    markup via :func:`_clean_html` (~17 KB, every Singles/Doubles row preserved),
    then split into :data:`CLAUDE_HTML_CHUNK`-char chunks. Each chunk is sent to
    Claude separately and the per-chunk match lists are concatenated. Sending the
    raw page whole instead lets Claude lose match lines in the noise (the "not
    picking up all the scores" regression).
    """
    cleaned = _clean_html(content or "")
    chunks = [
        cleaned[i : i + CLAUDE_HTML_CHUNK]
        for i in range(0, len(cleaned), CLAUDE_HTML_CHUNK)
    ] or [""]
    log(
        "INFO",
        f"   \U0001f9f9 HTML cleaned to {len(cleaned):,} chars \u2192 "
        f"{len(chunks)} chunk(s) for Claude",
    )
    matches = []
    for idx, chunk in enumerate(chunks, 1):
        log("INFO", f"   \u23f3 Claude chunk {idx}/{len(chunks)}\u2026")
        text = f"Part {idx}/{len(chunks)}:\n\n{chunk}" if len(chunks) > 1 else chunk
        parsed = _claude_request(client, key, system, log, tele, text=text)
        matches.extend(_as_list_of_dicts(parsed))
    return matches


# ---------------------------------------------------------------------------
# Optional OpenAI tournament-date recovery (gated on settings.OPENAI_API_KEY)
# ---------------------------------------------------------------------------
def _openai_chat(client, key, messages, model, log, tele, *, json_object=False):
    """Call OpenAI chat-completions and return the response text, or ''.

    Returns '' (a graceful skip) when no key is configured or the call fails.
    The key lives only in the Authorization header — never logged.
    """
    if not key:
        return ""
    payload = {"model": model, "temperature": 0, "messages": messages}
    if json_object:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {key}",
        "content-type": "application/json",
    }
    resp = client.post(
        OPENAI_API_URL, headers=headers, json=payload, timeout=CLAUDE_TIMEOUT,
    )
    if resp is None or resp.status_code != 200:
        status = "no response" if resp is None else f"HTTP {resp.status_code}"
        tele.record_error(redact_secrets(f"OpenAI fallback unavailable ({status})."))
        log("WARN", f"\u26a0\ufe0f OpenAI fallback skipped ({status})")
        return ""
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""
    except Exception:  # noqa: BLE001 - unexpected body shape
        return ""


def _recover_tournament_date(client, openai_key, items, content, log, tele):
    """Return the tournament date — Claude's value, else the OpenAI fallback."""
    claude_date = ""
    if items:
        claude_date = (items[0].get("tournament_date") or "").strip()
    if claude_date:
        log("INFO", f"   \U0001f4c5 tournament_date from Claude: {claude_date}")
        return claude_date
    if not openai_key:
        log("INFO", "   \u2139\ufe0f tournament_date missing \u2014 OpenAI fallback disabled")
        return ""
    text = _openai_chat(
        client, openai_key,
        [
            {"role": "system", "content": TOURNAMENT_DATE_PROMPT},
            {"role": "user", "content": _clean_html(content)},
        ],
        OPENAI_DATE_MODEL, log, tele, json_object=True,
    )
    if not text:
        return ""
    parsed = _parse_json(text)
    date = ""
    if isinstance(parsed, dict):
        date = (parsed.get("tournament_date") or "").strip()
    if date:
        log("INFO", f"   \U0001f4c5 tournament_date from OpenAI: {date}")
    return date


def _extract_sheet_id(url):
    """Pull the spreadsheet id out of a docs.google.com URL."""
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", url or "")
    return match.group(1) if match else ""


def _read_google_sheet(client, url, log, tele):
    """Return ``[(team, link), ...]`` from the 'Main' tab of a Google Sheet.

    The source uses the Google Sheets API; without those credentials here the
    sheet is read through its public CSV export (``gviz/tq?tqx=out:csv``).
    """
    sheet_id = _extract_sheet_id(url)
    if not sheet_id:
        tele.record_error(f"Could not extract a Google Sheet id from {url}")
        return []
    export = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        "/gviz/tq?tqx=out:csv&sheet=Main"
    )
    resp = client.get(export, headers={"accept": "text/csv,*/*"})
    if resp is None or not (200 <= resp.status_code < 300):
        tele.record_error(
            f"Google Sheet export failed for {sheet_id} "
            f"(HTTP {getattr(resp, 'status_code', 'none')})."
        )
        return []
    rows = []
    try:
        reader = csv.reader(io.StringIO(resp.text))
        records = list(reader)
    except Exception as exc:  # noqa: BLE001 - malformed CSV is non-fatal
        tele.record_error(redact_secrets(f"Google Sheet CSV parse failed: {exc}"))
        return []
    if not records:
        return []
    header = [(h or "").strip().lower() for h in records[0]]
    try:
        team_idx = header.index("team")
    except ValueError:
        team_idx = None
    try:
        link_idx = header.index("link")
    except ValueError:
        link_idx = None
    if link_idx is None:
        tele.record_error("Google Sheet 'Main' tab has no 'Link' column.")
        return []
    for record in records[1:]:
        link = record[link_idx].strip() if len(record) > link_idx else ""
        team = (
            record[team_idx].strip()
            if team_idx is not None and len(record) > team_idx
            else ""
        )
        if link:
            rows.append((team, link))
    log("INFO", f"\U0001f4d1 Google Sheet: {len(rows)} row(s) with a link")
    return rows


def _crawl_schedule(client, url, log, tele, *, renderer=None):
    """Crawl a schedule page for box-score links (HTML or PDF variants)."""
    url = _normalize_url(url)
    resp = _get_page(client, url, log, tele, renderer=renderer)
    if resp is None or not (200 <= resp.status_code < 300):
        tele.record_error(
            f"Schedule page fetch failed for {url} "
            f"(HTTP {getattr(resp, 'status_code', 'none')})."
        )
        return []
    sel = Selector(text=resp.text)
    links = []
    for label in ("Box Score", "Box Score (PDF)", "PDF Box"):
        if links:
            break
        for anchor in sel.xpath(f'//a[normalize-space(.)="{label}"]'):
            href = _parse_field("./@href", anchor)
            if href:
                links.append(urljoin(url, href))
    # De-dupe while preserving order.
    seen = set()
    ordered = []
    for link in links:
        if link not in seen:
            seen.add(link)
            ordered.append(link)
    log("INFO", f"\U0001f5d3\ufe0f Schedule {url} \u2192 {len(ordered)} box-score link(s)")
    return ordered


def _discover(client, url, log, tele, *, renderer=None):
    """Classify one input URL and return the box-score URLs it expands to."""
    url = (url or "").strip()
    if not url:
        return []
    host = _host(url)
    if "docs.google.com" in host:
        box_scores = []
        for _team, link in _read_google_sheet(client, url, log, tele):
            if "/schedule" in link:
                box_scores.extend(_crawl_schedule(client, link, log, tele, renderer=renderer))
            else:
                box_scores.append(_normalize_url(link))
        return box_scores
    if "/schedule" in url:
        return _crawl_schedule(client, url, log, tele, renderer=renderer)
    return [_normalize_url(url)]


# ---------------------------------------------------------------------------
# Box-score extraction: Claude is the only parser (no deterministic fallback)
# ---------------------------------------------------------------------------
def _find_pdf_link(sel):
    """Return a box-score PDF link from a sidearm/recap page, or ''."""
    pdf_link = _parse_field(
        '//li[contains(@class, "sidearm-document-header-open")]'
        '//a[@data-test-id="s-btn__root"]/@href', sel,
    )
    if not pdf_link:
        pdf_link = _parse_field('//object[@type="application/pdf"]/@data', sel)
    if not pdf_link:
        pdf_link = _parse_field('//li[@id="ctl00_cplhMainContent_btnOpen"]/a/@href', sel)
    return pdf_link


def _row_from_claude(res, tournament_date):
    """Map one Claude match dict onto the bespoke COLUMNS schema, or ``None``.

    Mirrors the source's gate: a row is kept only when the team-level result and
    both first-player names are present.
    """
    row = {c: (res.get(c, "") or "") for c in COLUMNS}
    row["tournament_date"] = tournament_date or row.get("tournament_date", "")
    if (
        row["winner_team"] and row["loser_team"] and row["team_score"]
        and row["winner_1_name"] and row["loser_1_name"]
    ):
        return row
    return None


def _core_extract(client, claude_key, openai_key, system, url, content,
                  raw_bytes, content_type, log, tele):
    """The Claude path: PDF link → PDF response → HTML, in source order."""
    sel = Selector(text=content)
    items = None

    pdf_link = _find_pdf_link(sel)
    if pdf_link:
        pdf_link = urljoin(url, pdf_link)
        log("INFO", f"   \U0001f4c4 PDF box score: {pdf_link}")
        pdf_resp = client.get(pdf_link, headers=BROWSER_HEADERS)
        if pdf_resp is not None and 200 <= pdf_resp.status_code < 300 and pdf_resp.content:
            items = _claude_extract_pdf(client, claude_key, system, pdf_resp.content, log, tele)

    is_pdf_response = (
        "application/pdf" in (content_type or "").lower()
        or (raw_bytes or b"")[:5] == b"%PDF-"
    )
    if is_pdf_response:
        log("INFO", "   \U0001f4c4 Response body is a PDF \u2014 sending to Claude")
        items = _claude_extract_pdf(client, claude_key, system, raw_bytes, log, tele)

    if not items:
        items = _claude_extract_html(client, claude_key, system, content, log, tele)

    if not items:
        return []

    tournament_date = _recover_tournament_date(client, openai_key, items, content, log, tele)
    rows = []
    for res in items:
        row = _row_from_claude(res, tournament_date)
        if row:
            rows.append(row)
    return rows


def _extract_box_score(client, claude_key, openai_key, system, url, log, tele, *, renderer=None):
    """Fetch one box score and extract rows with Claude (no fallback)."""
    resp = _get_page(client, url, log, tele, renderer=renderer)
    if resp is None or not (200 <= resp.status_code < 300):
        tele.record_error(
            f"Box score fetch failed for {url} "
            f"(HTTP {getattr(resp, 'status_code', 'none')})."
        )
        return []
    content = resp.text or ""
    raw_bytes = resp.content or b""
    content_type = resp.headers.get("Content-Type", "")

    return _core_extract(
        client, claude_key, openai_key, system, url, content,
        raw_bytes, content_type, log, tele,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(run_obj, log):
    """Execute the College Dual Match (AI) scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    params = run_obj.params or {}

    log("INFO", "\U0001f3be College Dual Match starting \u2014 box-score extraction")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    # ---- extraction mode (Claude REQUIRED) -------------------------------
    # Prefer the per-scraper key saved in the Lab → Settings tab; fall back to
    # the workspace GeneralConfig key (Settings page), then env settings.CLAUDE_KEYS.
    # Comma-separate to rotate several.
    # Claude is the ONLY parser — the run fails honestly here when no key (or no
    # prompt) is configured. OpenAI stays optional (tournament_date recovery).
    scraper_key = (getattr(scraper, "claude_api_key", "") or "").strip()
    if scraper_key:
        claude_keys = [k.strip() for k in scraper_key.split(",") if k.strip()]
    else:
        from accounts.models import GeneralConfig

        claude_keys = GeneralConfig.claude_keys()

    if not claude_keys:
        msg = (
            "College Dual Match requires a Claude API key \u2014 set "
            "ANTHROPIC_API_KEY (or a per-scraper key in the Lab \u2192 Settings "
            "tab). There is no deterministic fallback."
        )
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    system = _load_prompt()
    if not system:
        msg = f"Claude prompt missing/empty ({PROMPT_PATH}) \u2014 cannot run."
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    openai_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if openai_key:
        log("INFO", "\U0001f513 OpenAI enabled (tournament_date recovery)")
    log("INFO", "\U0001f9e0 AI extraction enabled (Claude) \u2014 the only parser")

    # ---- input URL(s) (no date-only discovery) ---------------------------
    raw_url = params.get("tournament_url")
    if isinstance(raw_url, (list, tuple)):
        urls = [u.strip() for u in raw_url if isinstance(u, str) and u.strip()]
    elif isinstance(raw_url, str):
        urls = [raw_url.strip()] if raw_url.strip() else []
    else:
        urls = []

    if not urls:
        msg = (
            "College Dual Match AI requires a tournament/box-score/schedule/"
            "Google-Sheet URL in params['tournament_url'] \u2014 this scraper has "
            "no date-only discovery."
        )
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    # How the run was invoked decides what the items CSV contains (phase 3):
    #   * Google Sheet input  → CSV holds only the NEW matches. The production
    #     Sheets pipeline appends this CSV back to the sheet, so re-emitting
    #     already-stored matches would duplicate rows there.
    #   * single link(s) / schedule input → CSV holds EVERY extracted match,
    #     regardless of whether it is already in the database. A manual re-extract
    #     of a link must hand back the whole box score; the DB is used only to add
    #     the genuinely-new matches, never to filter the CSV.
    is_sheet_run = any("docs.google.com" in _host(u) for u in urls)

    proxies = build_proxies(scraper, log)

    # Page fetch: every HTML page (schedule crawl + box-score recap) is rendered
    # by a headless patchright Chromium (full JavaScript executed, the scraper's
    # proxy attached) so the fully-hydrated markup reaches Claude. Renders run on
    # one dedicated owner thread (Playwright's sync API is thread-bound) with a
    # PERSISTENT profile (clearance cookies survive across pages/runs); see
    # _BrowserRenderer. allowed_hosts=None mirrors the curl client (college
    # crawls arbitrary discovered athletics hosts); the SSRF public-IP guard
    # still applies inside BrowserClient. curl_cffi stays for the Google-Sheet
    # CSV export, PDF byte downloads, and as an honest fallback if a render fails.
    profile_root = getattr(settings, "SCRAPER_BROWSER_PROFILE_DIR", "")
    profile_dir = os.path.join(profile_root, scraper.slug) if profile_root else None
    renderer = _BrowserRenderer(
        scraper=scraper, log=log, tele=tele, allowed_hosts=None, profile_dir=profile_dir,
    )

    try:
        # ---- phase 1 · discover box-score URLs -------------------------------
        log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering box scores \u2500\u2500\u2500\u2500")
        box_scores = []
        with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
            for url in urls:
                log("INFO", f"\U0001f50e Classifying {url}")
                try:
                    box_scores.extend(_discover(discovery, url, log, tele, renderer=renderer))
                except Exception as exc:  # noqa: BLE001 - one bad input can't kill the run
                    tele.record_error(redact_secrets(f"Discovery failed for {url}: {exc}"), exc=exc)
                    log("WARN", redact_secrets(f"\u26a0\ufe0f discovery failed: {exc.__class__.__name__}: {exc}"))

        # De-dupe box scores, preserving order.
        seen_urls = set()
        ordered = []
        for url in box_scores:
            if url and url not in seen_urls:
                seen_urls.add(url)
                ordered.append(url)
        box_scores = ordered

        total = len(box_scores)
        Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
        log("INFO", f"\U0001f4cb {total} box-score recap(s) to extract")

        if not box_scores:
            msg = "No box-score recaps were discovered from the supplied URL(s)."
            log("ERROR", f"\U0001f6d1 {msg}")
            tele.record_error(msg)
            return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

        # ---- phase 2 · Claude extraction (threaded) --------------------------
        # Collect every extracted match (raw scraper-key dicts) under a lock; the
        # canonical 23->65 mapping + DB dedup happens once, after the pool, via
        # accounts.college_store.ingest(). A light in-run guard drops the obvious
        # case of one box score emitting the same match twice (keyed by normalized
        # identity); genuine cross-source/cross-run duplicates are left for the store
        # to collapse against what's already persisted.
        lock = threading.Lock()
        collected = []
        seen_ids = set()

        def process(box_url):
            client = ScraperClient(log=log, tele=tele, proxies=proxies)
            claude_key = random.choice(claude_keys) if claude_keys else ""
            try:
                rows = _extract_box_score(
                    client, claude_key, openai_key, system, box_url, log, tele,
                    renderer=renderer,
                )
                for row in rows:
                    ident = college_store.match_hash(college_store.map_extracted(row))
                    with lock:
                        if ident in seen_ids:
                            continue
                        seen_ids.add(ident)
                        collected.append(row)
                    log(
                        "INFO",
                        f"   \U0001f3c6 {row.get('draw_team_type', '') or 'Match'}: "
                        f"{row.get('winner_1_name') or '?'} def. "
                        f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                        f"\u2014 {row.get('tournament_name') or 'Dual Match'}",
                    )
            except Exception as exc:  # noqa: BLE001 - one bad URL can't kill the run
                tele.record_error(redact_secrets(f"Box score {box_url} failed: {exc}"), exc=exc)
                log("WARN", redact_secrets(f"\u26a0\ufe0f box score failed: {exc.__class__.__name__}: {exc}"))
            finally:
                Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
                client.close()

        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 extracting box scores \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, box_scores))

        extracted = len(collected)

        # ---- phase 3 · persist new matches (dedup vs the match database) -----
        log("INFO", "\u2500\u2500\u2500\u2500 phase 3 \u00b7 saving new matches \u2500\u2500\u2500\u2500")
        mapped = [college_store.map_extracted(row) for row in collected]
        new_rows, skipped = college_store.ingest(
            mapped, run=run_obj, source=college_store.SOURCE_SCRAPE
        )
        new_count = len(new_rows)

        # Items CSV per the invocation mode (see ``is_sheet_run`` above): a Google
        # Sheet run emits only the new matches (the Sheets pipeline appends them);
        # a single-link/schedule run emits every extracted match (a full re-extract),
        # while still persisting only the new ones to the DB. ``mapped`` is already
        # in-run de-duplicated (phase 2), so single-link CSVs never repeat a match.
        csv_rows = new_rows if is_sheet_run else mapped
        row_count = len(csv_rows)

        log("INFO", "\u2500\u2500\u2500\u2500 summary \u2500\u2500\u2500\u2500")
        log(
            "INFO",
            f"\U0001f9ee {extracted} match(es) extracted \u2014 {new_count} new, "
            f"{skipped} already in the database",
        )
        log(
            "INFO",
            f"\U0001f4c4 CSV mode: {'Google Sheet (new only)' if is_sheet_run else 'single link(s) (all matches)'} "
            f"\u2014 {row_count} row(s) written",
        )
        log(
            "INFO",
            f"\U0001f4ca Telemetry: {tele.request_count} request(s), {tele.error_count} error(s)",
        )
        # A run that extracted matches SUCCEEDS even when every one was already stored
        # (0 new is a valid, healthy outcome). It only FAILS when nothing at all was
        # extracted from the discovered box scores.
        status = Run.Status.SUCCESS if extracted else Run.Status.FAILED
        icon = "\U0001f3c1" if status == Run.Status.SUCCESS else "\U0001f6d1"
        log("INFO", f"{icon} Run finished \u2014 status={status}, new_rows={new_count}")
        items_csv = college_store.to_csv(csv_rows) if csv_rows else ""
        return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
    finally:
        # Stop the render owner thread and close the one Chromium so a run never
        # leaves a lingering browser process behind — even on an early return or
        # an unexpected error mid-phase.
        renderer.close()
