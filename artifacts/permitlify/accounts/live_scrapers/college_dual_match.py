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
``application/pdf``; HTML is cleaned to markup and split into ~160 000-char
chunks. Claude returns a JSON array of match objects which map 1:1 onto this
scraper's bespoke output columns. Two deterministic parsers run as fallbacks
(when Claude is off, or yields nothing for a URL): the ``auburntigers`` sidearm
stats-XML fallback, and a Sidearm-**HTML** parser for box-score pages that embed
every match in the page itself (e.g. ``cmsathletics.org``).

Credentials:

- ``settings.CLAUDE_KEYS`` (a list, sourced from ``CLAUDE_KEYS`` /
  ``ANTHROPIC_API_KEY``). **Optional** — when empty the run still proceeds using
  the deterministic parsers (a direct box-score link works with no key); it only
  fails honestly when nothing at all could be extracted. A key is chosen with
  ``random.choice`` per worker (mirrors the source's rotation) and is **never**
  logged.
- ``settings.OPENAI_API_KEY`` — **optional**. Used only to recover a missing
  ``tournament_date`` and (in the auburn fallback) to normalize gender/college
  names. When unset that step is skipped gracefully and the field is left as-is.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import base64
import csv
import io
import json
import os
import random
import re
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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
CLAUDE_MAX_TOKENS = 4096
# Anthropic returns 529 ("overloaded") under load; retry it alongside the usual
# transient/rate-limit statuses.
CLAUDE_RETRY_STATUSES = frozenset(RETRY_STATUSES | {529})
# Claude (especially on a multi-page PDF) can take well past the default 30 s.
CLAUDE_TIMEOUT = 180

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_DATE_MODEL = "gpt-4o-mini"
OPENAI_HELPER_MODEL = "gpt-4.1-mini"

# HTML is fed to Claude in ~160 000-char chunks (matches the source).
CHUNK_SIZE = 160_000

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
    optimization in the source) is skipped — the chunker handles size and the
    surviving ``href``/``class``/``id`` attributes actually help Claude.
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
# Page fetch with a patchright (persistent-profile) anti-bot fallback
# ---------------------------------------------------------------------------
class _HtmlResponse:
    """Adapt browser-rendered HTML to the curl client's read surface.

    A page fetched through the browser fallback then slots transparently into
    the same callers that consume a ``curl_cffi`` ``Response`` (they read
    ``.status_code`` / ``.text`` / ``.content`` / ``.headers``).
    """

    __slots__ = ("status_code", "text", "content", "headers")

    def __init__(self, html):
        self.status_code = 200
        self.text = html or ""
        self.content = (html or "").encode("utf-8", "ignore")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


class _BrowserFallback:
    """Patchright + persistent-profile fallback for anti-bot-challenged pages.

    The curl_cffi client handles the vast majority of college schedule / box-
    score pages. A few athletics hosts sit behind a JavaScript anti-bot
    interstitial (Cloudflare / Imperva) that answers a plain HTTP client with a
    403 no matter how many times it retries (e.g. the ``sammieetc.com`` block
    seen in the wild). For those a real patchright Chromium executes the
    challenge JS, earns the clearance cookie and returns the rendered HTML.

    One **persistent** profile dir (``user_data_dir``) is reused for every
    fallback fetch, so the clearance cookie survives across pages *and* across
    runs — once a host is cleared, later pages on it skip the challenge. A
    persistent Chrome profile can be opened by only one process at a time, and
    one Chromium is about all the container's memory can spare, so every
    fallback fetch is serialized behind a lock: phase-2 worker threads queue
    for it. The primary curl path stays fully concurrent; only the genuinely
    blocked pages pay the browser cost. Any error is an honest ``None`` (the
    caller records the original failure) — never fabricated content.
    """

    def __init__(self, *, scraper, log, tele, allowed_hosts, profile_dir):
        self._scraper = scraper
        self._log = log
        self._tele = tele
        self._allowed_hosts = allowed_hosts or None
        self._profile_dir = profile_dir
        self._lock = threading.Lock()

    def fetch_html(self, url):
        """Return cleared page HTML via patchright, or ``None`` (honest fail)."""
        with self._lock:
            try:
                from ._browser import BrowserClient
            except Exception as exc:  # noqa: BLE001 - patchright not importable
                self._tele.record_error(
                    redact_secrets(f"Browser fallback unavailable for {url}: {exc}")
                )
                return None
            self._log(
                "INFO",
                f"\U0001f310 anti-bot challenge \u2014 retrying {url} via "
                "patchright (persistent profile)",
            )
            try:
                with BrowserClient(
                    log=self._log,
                    tele=self._tele,
                    proxy=getattr(self._scraper, "proxy", None),
                    allowed_hosts=self._allowed_hosts,
                    headless=getattr(settings, "SCRAPER_BROWSER_HEADLESS", True),
                    channel=getattr(settings, "SCRAPER_BROWSER_CHANNEL", "") or None,
                    user_data_dir=self._profile_dir,
                ) as browser:
                    sel = browser.get_selector(url)
                if sel is None:
                    return None
                return sel.get() or None
            except Exception as exc:  # noqa: BLE001 - honest fail, never fabricate
                self._tele.record_error(
                    redact_secrets(f"Browser fallback failed for {url}: {exc}"),
                    exc=exc,
                )
                self._log(
                    "WARN",
                    redact_secrets(
                        f"\u26a0\ufe0f browser fallback failed for {url}: "
                        f"{exc.__class__.__name__}: {exc}"
                    ),
                )
                return None


def _get_page(client, url, log, tele, *, browser=None, headers=BROWSER_HEADERS):
    """GET a page via curl_cffi; on an anti-bot challenge, fall back to a real
    patchright browser (persistent profile).

    Returns a response-like object exposing ``.status_code`` / ``.text`` /
    ``.content`` / ``.headers`` — the curl ``Response`` on success, or an
    :class:`_HtmlResponse` wrapping the browser-rendered HTML — else the failing
    curl response (or ``None``). The browser is launched **only** when curl
    exhausts its retries on an anti-bot *challenge* (``client.last_challenge``),
    so a 404 / timeout / transport failure never spins up Chromium.
    """
    resp = client.get(url, headers=headers)
    if resp is not None and 200 <= resp.status_code < 300:
        return resp
    if browser is not None and getattr(client, "last_challenge", False):
        html = browser.fetch_html(url)
        if html:
            return _HtmlResponse(html)
    return resp


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
    """Clean + chunk HTML, send each chunk to Claude, merge into one list."""
    cleaned = _clean_html(content)
    chunks = [cleaned[i:i + CHUNK_SIZE] for i in range(0, len(cleaned), CHUNK_SIZE)] or [""]
    log(
        "INFO",
        f"   \U0001f9f9 HTML cleaned to {len(cleaned):,} chars \u2192 "
        f"{len(chunks)} chunk(s) for Claude",
    )
    merged = []
    for i, chunk in enumerate(chunks):
        text = f"Part {i + 1}/{len(chunks)}:\n\n{chunk}" if len(chunks) > 1 else chunk
        log("INFO", f"   \u23f3 Claude chunk {i + 1}/{len(chunks)}\u2026")
        parsed = _claude_request(client, key, system, log, tele, text=text)
        merged.extend(_as_list_of_dicts(parsed))
    return merged


# ---------------------------------------------------------------------------
# Optional OpenAI fallbacks (gated on settings.OPENAI_API_KEY)
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


def _official_college_name(client, openai_key, name, log, tele):
    """Normalize a college name via OpenAI; leave it as-is when disabled."""
    name = (name or "").strip()
    if not (name and openai_key):
        return name
    system = (
        "You are a data standardization assistant. Identify the official, full, "
        "formal name of a college or university from a user-provided name, "
        "nickname, abbreviation, or partial text. Return only the official "
        "institution name used by the college itself. If multiple match, return "
        "the most common U.S. one. If uncertain, return \"Unknown\". "
        'Return results as: {"official_name": "<name>"}'
    )
    text = _openai_chat(
        client, openai_key,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": f'Convert this college name to its official full name: "{name}"'},
        ],
        OPENAI_HELPER_MODEL, log, tele,
    )
    parsed = _parse_json(text) if text else None
    if isinstance(parsed, dict):
        official = (parsed.get("official_name") or "").strip()
        if official and official.lower() != "unknown":
            return official
    return name


def _convert_college_name(client, openai_key, in1, in2, out1, out2, log, tele):
    """Map two raw college labels to the two official team names (OpenAI)."""
    if not (in1 and in2 and out1 and out2 and openai_key):
        return {}
    system = (
        f"In the context of this college match: '{out1} vs {out2}' provide the "
        f"official college names for the texts: '{in1}' and '{in2}'. Your "
        f"response for each text should be either '{out1}' or '{out2}'. Your "
        "response should not be the same for both texts. List the two texts as "
        "key followed by the college names as value in JSON format. Return "
        'results as: {"<text_1>": "<college_1>", "<text_2>": "<college_2>"}'
    )
    text = _openai_chat(
        client, openai_key, [{"role": "system", "content": system}],
        OPENAI_HELPER_MODEL, log, tele,
    )
    parsed = _parse_json(text) if text else None
    return parsed if isinstance(parsed, dict) else {}


def _gender_from_players(client, openai_key, players, log, tele):
    """Infer 'Male'/'Female' for a player list via OpenAI; '' when disabled."""
    players = [p for p in players if p]
    if not (players and openai_key):
        return ""
    players_list = "; ".join(players)
    system = (
        f'In context of this list of player names: "{players_list}", provide the '
        "gender of the list as a whole. Respond in JSON with a key `Gender` whose "
        "value is only either `Male` or `Female`."
    )
    text = _openai_chat(
        client, openai_key, [{"role": "system", "content": system}],
        OPENAI_HELPER_MODEL, log, tele,
    )
    parsed = _parse_json(text) if text else None
    if isinstance(parsed, dict) and parsed.get("Gender"):
        return str(parsed["Gender"]).title()
    return ""


# ---------------------------------------------------------------------------
# Discovery: Google Sheet rows + schedule-page crawl
# ---------------------------------------------------------------------------
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


def _crawl_schedule(client, url, log, tele, *, browser=None):
    """Crawl a schedule page for box-score links (HTML or PDF variants)."""
    url = _normalize_url(url)
    resp = _get_page(client, url, log, tele, browser=browser)
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


def _discover(client, url, log, tele, *, browser=None):
    """Classify one input URL and return the box-score URLs it expands to."""
    url = (url or "").strip()
    if not url:
        return []
    host = _host(url)
    if "docs.google.com" in host:
        box_scores = []
        for _team, link in _read_google_sheet(client, url, log, tele):
            if "/schedule" in link:
                box_scores.extend(_crawl_schedule(client, link, log, tele, browser=browser))
            else:
                box_scores.append(_normalize_url(link))
        return box_scores
    if "/schedule" in url:
        return _crawl_schedule(client, url, log, tele, browser=browser)
    return [_normalize_url(url)]


# ---------------------------------------------------------------------------
# Box-score extraction: Claude core + auburn (sidearm stats XML) fallback
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


def _fmt_name(name):
    """Format a name as ``Last, First`` (ported from the auburn parser)."""
    name = (name or "").strip()
    if "," in name:
        return name
    parts = name.split()
    if not parts:
        return ""
    return f"{parts[-1]}, {' '.join(parts[:-1])}".strip().rstrip(",").strip()


def _clean_name(value):
    """Strip stray punctuation from a player name (ported from helper)."""
    value = (value or "").strip()
    value = re.sub(r"^\(\s*\d+\s*,\s*", "", value)
    value = re.sub(r"[^A-Za-z\u00c0-\u017f,\s-]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _auburn_colleges(root):
    return {team.get("vh"): team.get("name") for team in root.findall("team")}


def _auburn_score(winner, loser):
    parts = []
    for i in range(1, 6):
        w = winner.get(f"set_{i}")
        l = loser.get(f"set_{i}")
        if w is None or l is None:
            break
        parts.append(f"{w}-{l}")
    return " ".join(parts) + ";"


def _auburn_singles(root, colleges):
    results = []
    for match in root.findall(".//singles_match"):
        scores = match.findall("singles_score")
        if len(scores) != 2:
            continue
        a, b = scores

        def wins(x, y):
            count = 0
            for i in range(1, 6):
                sx, sy = x.get(f"set_{i}"), y.get(f"set_{i}")
                if sx and sy and int(sx) > int(sy):
                    count += 1
            return count

        winner, loser = (a, b) if wins(a, b) > wins(b, a) else (b, a)
        results.append({
            "draw_team_type": "Singles",
            "draw_name": f"#{match.get('match')} Singles",
            "winner_1_name": _clean_name(_fmt_name(winner.get("name"))),
            "winner_2_name": "",
            "loser_1_name": _clean_name(_fmt_name(loser.get("name"))),
            "loser_2_name": "",
            "winner_college": colleges.get(winner.get("vh"), ""),
            "loser_college": colleges.get(loser.get("vh"), ""),
            "score": _auburn_score(winner, loser),
        })
    return results


def _auburn_doubles(root, colleges):
    results = []
    for match in root.findall(".//doubles_match"):
        scores = match.findall("doubles_score")
        if len(scores) != 2:
            continue
        a, b = scores
        try:
            winner, loser = (a, b) if int(a.get("set_1")) > int(b.get("set_1")) else (b, a)
        except (TypeError, ValueError):
            continue
        results.append({
            "draw_team_type": "Doubles",
            "draw_name": f"#{match.get('match')} Doubles",
            "winner_1_name": _clean_name(_fmt_name(winner.get("name_1"))),
            "winner_2_name": _clean_name(_fmt_name(winner.get("name_2"))),
            "loser_1_name": _clean_name(_fmt_name(loser.get("name_1"))),
            "loser_2_name": _clean_name(_fmt_name(loser.get("name_2"))),
            "winner_college": colleges.get(winner.get("vh"), ""),
            "loser_college": colleges.get(loser.get("vh"), ""),
            "score": f"{winner.get('set_1')}-{loser.get('set_1')};",
        })
    return results


def _auburn_parse_all(xml_text):
    root = ET.fromstring(xml_text)
    colleges = _auburn_colleges(root)
    return _auburn_doubles(root, colleges) + _auburn_singles(root, colleges)


def _auburn_team_data(client, openai_key, sel, log, tele):
    """Read the dual-match winner/loser team + team score from a boxscore page."""
    teams = sel.xpath('//div[contains(@class,"boxscore-teams-info__team")]')
    names = teams.xpath(".//img/@alt").getall()
    score_txt = sel.xpath(
        '//div[contains(@class,"boxscore-teams-info__score-points")]/text()'
    ).get()
    if not score_txt or len(names) != 2:
        return "", "", ""
    try:
        s1, s2 = map(int, score_txt.strip().split("-"))
    except ValueError:
        return "", "", ""
    team1, team2 = names[0].strip(), names[1].strip()
    if s1 > s2:
        winner_team, loser_team, team_score = team1, team2, f"{s1}-{s2};"
    else:
        winner_team, loser_team, team_score = team2, team1, f"{s2}-{s1};"
    winner_team = _official_college_name(client, openai_key, winner_team, log, tele)
    loser_team = _official_college_name(client, openai_key, loser_team, log, tele)
    return winner_team, loser_team, team_score


def _auburn_row(match_data, winner_team, loser_team, team_score, tournament_date,
                tournament_name_pre, draw_gender, college_map):
    """Map one auburn match dict onto the bespoke COLUMNS schema, or ``None``."""
    draw_name = match_data.get("draw_name", "")
    draw_team_type = match_data.get("draw_team_type", "")
    score = match_data.get("score", "")
    winner_1_name = match_data.get("winner_1_name", "")
    winner_2_name = match_data.get("winner_2_name", "")
    loser_1_name = match_data.get("loser_1_name", "")
    loser_2_name = match_data.get("loser_2_name", "")
    winner_college = match_data.get("winner_college", "")
    loser_college = match_data.get("loser_college", "")

    if not (winner_1_name and loser_1_name):
        return None

    # Normalize colleges (cached); fall back to the raw label when OpenAI is off.
    winner_college_fmt = college_map.get(winner_college) or winner_college
    loser_college_fmt = college_map.get(loser_college) or loser_college
    winner_1_college = winner_2_college = winner_college_fmt
    loser_1_college = loser_2_college = loser_college_fmt

    player_gender = ""
    tournament_gender = ""
    if draw_gender == "Male":
        player_gender, tournament_gender = "M", "Men"
    elif draw_gender == "Female":
        player_gender, tournament_gender = "F", "Women"

    winner_1_gender = player_gender if (player_gender and winner_1_name) else ""
    winner_2_gender = player_gender if (player_gender and winner_2_name) else ""
    loser_1_gender = player_gender if (player_gender and loser_1_name) else ""
    loser_2_gender = player_gender if (player_gender and loser_2_name) else ""

    if draw_team_type == "Singles":
        winner_2_college = loser_2_college = ""
        winner_2_gender = loser_2_gender = ""

    if tournament_gender:
        tournament_name = f"{tournament_name_pre} - {tournament_gender}"
    else:
        tournament_name = tournament_name_pre

    return {
        "tournament_date": tournament_date,
        "tournament_name": tournament_name,
        "tournament_gender": tournament_gender,
        "draw_name": draw_name,
        "draw_gender": draw_gender,
        "draw_team_type": draw_team_type,
        "winner_1_name": winner_1_name,
        "winner_2_name": winner_2_name,
        "winner_1_gender": winner_1_gender,
        "winner_2_gender": winner_2_gender,
        "winner_1_college": winner_1_college,
        "winner_2_college": winner_2_college,
        "loser_1_name": loser_1_name,
        "loser_2_name": loser_2_name,
        "loser_1_gender": loser_1_gender,
        "loser_2_gender": loser_2_gender,
        "loser_1_college": loser_1_college,
        "loser_2_college": loser_2_college,
        "score": score,
        "winner_team": winner_team,
        "loser_team": loser_team,
        "team_score": team_score,
        "outcome": "Completed",
    }


def _convert_date(value, in_fmt, out_fmt):
    value = (value or "").strip()
    if not value:
        return ""
    try:
        return datetime.strptime(value, in_fmt).strftime(out_fmt)
    except Exception:  # noqa: BLE001 - unparseable date
        return value


def _auburn_extract(client, openai_key, url, content, log, tele):
    """Deterministic sidearm fallback: boxscore HTML + stats XML API."""
    sel = Selector(text=content)
    winner_team, loser_team, team_score = _auburn_team_data(
        client, openai_key, sel, log, tele
    )
    if not (winner_team and loser_team and team_score):
        return []

    tournament_name_pre = f"Dual Match: {winner_team} vs {loser_team}"
    raw_date = _parse_field(
        '//div[@class="boxscore-game-info-item" and '
        'span[@class="boxscore-game-info-item__name" and text()="Date"]]'
        '/span[@class="boxscore-game-info-item__value"]', sel,
    )
    tournament_date = _convert_date(raw_date, "%a, %b. %d (%Y)", "%m/%d/%Y")

    tournament_id = ""
    try:
        parts = urlparse(url).path.strip("/").split("/")
        tournament_id = parts[parts.index("boxscore") + 1]
    except Exception:  # noqa: BLE001 - not a /boxscore/ URL
        tournament_id = ""
    if not tournament_id:
        return []

    api_url = f"https://stats.{_host(url, strip_www=True)}/api/v1/game/xml/{tournament_id}"
    log("INFO", f"   \U0001f9ea auburn stats XML: {api_url}")
    resp = client.get(api_url, headers={
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
    })
    if resp is None or not (200 <= resp.status_code < 300):
        return []
    try:
        matches = _auburn_parse_all(resp.text)
    except Exception as exc:  # noqa: BLE001 - bad XML is non-fatal
        tele.record_error(redact_secrets(f"auburn XML parse failed for {url}: {exc}"))
        return []

    players = []
    for match in matches:
        for field in ("winner_1_name", "winner_2_name", "loser_1_name", "loser_2_name"):
            if match.get(field):
                players.append(match[field])
    draw_gender = _gender_from_players(
        client, openai_key, sorted(set(players)), log, tele
    )

    # Cache college normalization per (winner_college, loser_college) pair.
    college_map = {}
    pair_cache = {}
    for match in matches:
        wc = match.get("winner_college", "")
        lc = match.get("loser_college", "")
        key = (wc, lc)
        if wc and lc and key not in pair_cache:
            pair_cache[key] = _convert_college_name(
                client, openai_key, wc, lc, winner_team, loser_team, log, tele
            )
            college_map.update(pair_cache[key])

    rows = []
    for match in matches:
        row = _auburn_row(
            match, winner_team, loser_team, team_score, tournament_date,
            tournament_name_pre, draw_gender, college_map,
        )
        if row:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Deterministic Sidearm-HTML box score (every match embedded in the page)
# ---------------------------------------------------------------------------
# Some athletics sites (e.g. cmsathletics.org) render the whole dual match in
# the box-score page itself: one <table> per court with a caption ("#1 Doubles",
# "#3 Singles"), a "Winner" marker on the winning competitor's row, player cells
# of the form "Name [College]" (doubles join two names with " / "), and a team
# header carrying each side's logo alt + final team score. That is enough to
# build every row with no stats-XML API and no AI — names, colleges, per-set
# scores and gender (from the URL/title) all come straight from the page.
def _sidearm_cell_text(cell):
    """Visible text of a box-score cell, dropping the sr-only 'Winner' marker."""
    parts = cell.xpath(
        './/text()[not(ancestor::span[contains(@class, "boxscore-winner")])]'
    ).getall()
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _sidearm_player_cell(text):
    """Split a "Name [College]" (or "P1 / P2 [College]") cell into names + college."""
    text = re.sub(r"^\s*Winner\s+", "", text or "", flags=re.IGNORECASE).strip()
    college = ""
    match = re.search(r"\[([^\]]+)\]\s*$", text)
    if match:
        college = match.group(1).strip()
        text = text[: match.start()].strip()
    names = [
        _clean_name(_fmt_name(part)) for part in text.split("/") if part.strip()
    ]
    return [n for n in names if n], college


def _sidearm_set_cell(cell):
    """Return (games, tiebreak) for a set cell. A tiebreak set renders the loser's
    points in a <sup>, e.g. ``<td>6 <sup>4</sup></td>`` -> ("6", "4")."""
    games = re.sub(r"\s+", " ", " ".join(cell.xpath("./text()").getall())).strip()
    tb = (cell.xpath("./sup//text()").get() or "").strip()
    return games, tb


def _sidearm_score(winner_sets, loser_sets):
    """Build a "6-4, 7-6(4);" score from the two competitors' per-set (games, tb)
    cells. Matches the Claude prompt's canonical format: written from the match
    winner's perspective, sets joined with ", ", and a tiebreak set carrying the
    SET loser's tiebreak points in parentheses."""
    parts = []
    for (wg, wtb), (lg, ltb) in zip(winner_sets, loser_sets):
        wg, lg = (wg or "").strip(), (lg or "").strip()
        if not wg and not lg:
            continue
        piece = f"{wg}-{lg}"
        tb = ""
        try:
            if int(wg) > int(lg):
                tb = (ltb or "").strip()  # match-loser lost this set
            elif int(lg) > int(wg):
                tb = (wtb or "").strip()  # match-winner lost this set
        except ValueError:
            tb = ""
        if tb:
            piece += f"({tb})"
        parts.append(piece)
    return (", ".join(parts) + ";") if parts else ""


def _sidearm_table_match(table):
    """Parse one caption table into the auburn intermediate match dict, or None."""
    caption = (table.xpath("./caption//text()").get() or "").strip()
    low = caption.lower()
    if "doubles" in low:
        team_type = "Doubles"
    elif "singles" in low:
        team_type = "Singles"
    else:
        return None
    rows = [r for r in table.xpath(".//tr") if r.xpath("./td")]
    if len(rows) != 2:
        return None

    def is_winner(row):
        return bool(row.xpath('.//span[contains(@class, "boxscore-winner")]'))

    if is_winner(rows[0]) == is_winner(rows[1]):
        return None  # can't tell the winner row from the loser row
    winner_row, loser_row = (
        (rows[0], rows[1]) if is_winner(rows[0]) else (rows[1], rows[0])
    )

    def parse(row):
        cells = row.xpath("./td")
        names, college = _sidearm_player_cell(_sidearm_cell_text(cells[0]))
        sets = [_sidearm_set_cell(c) for c in cells[1:]]
        return names, college, sets

    w_names, w_college, w_sets = parse(winner_row)
    l_names, l_college, l_sets = parse(loser_row)
    if not w_names or not l_names:
        return None
    return {
        "draw_team_type": team_type,
        "draw_name": caption,
        "winner_1_name": w_names[0],
        "winner_2_name": w_names[1] if len(w_names) > 1 else "",
        "loser_1_name": l_names[0],
        "loser_2_name": l_names[1] if len(l_names) > 1 else "",
        "winner_college": w_college,
        "loser_college": l_college,
        "score": _sidearm_score(w_sets, l_sets),
    }


def _sidearm_team_block(sel, side):
    """Return (team_name, final_score|None) for the away/home header block."""
    blk = sel.xpath(f'//*[contains(@class, "team {side}")]')
    if not blk:
        return "", None
    name = re.sub(
        r"\s+logo$", "", (blk.xpath(".//img/@alt").get() or "").strip(),
        flags=re.IGNORECASE,
    ).strip()
    raw = (blk.xpath('.//span[@class="score"]/text()').get() or "").strip()
    try:
        score = int(raw)
    except ValueError:
        score = None
    return name, score


def _sidearm_html_extract(url, content, log, tele):
    """Deterministic parser for Sidearm box-score pages that embed every match in
    the page HTML. Returns [] when the page isn't that layout (honest fail)."""
    try:
        sel = Selector(text=content or "")
    except Exception:  # noqa: BLE001 - unparseable HTML is non-fatal
        return []
    tables = sel.xpath("//table[caption]")
    if not tables:
        return []
    away_name, away_score = _sidearm_team_block(sel, "away")
    home_name, home_score = _sidearm_team_block(sel, "home")
    if not (away_name and home_name) or away_score is None or home_score is None:
        return []
    if away_score == home_score:
        return []
    if away_score > home_score:
        winner_team, loser_team, hi, lo = away_name, home_name, away_score, home_score
    else:
        winner_team, loser_team, hi, lo = home_name, away_name, home_score, away_score
    team_score = f"{hi}-{lo};"
    tournament_name_pre = f"Dual Match: {winner_team} vs {loser_team}"

    title = sel.xpath("//title/text()").get() or ""
    date_match = re.search(r"on\s+(\d{1,2}/\d{1,2}/\d{4})", title) or re.search(
        r"\b(\d{1,2}/\d{1,2}/\d{4})\b", content or ""
    )
    tournament_date = _convert_date(
        date_match.group(1) if date_match else "", "%m/%d/%Y", "%m/%d/%Y"
    )

    haystack = f"{url} {title}".lower()
    if "women" in haystack:
        draw_gender = "Female"
    elif "men" in haystack:
        draw_gender = "Male"
    else:
        draw_gender = ""

    rows = []
    for table in tables:
        match_data = _sidearm_table_match(table)
        if not match_data:
            continue
        row = _auburn_row(
            match_data, winner_team, loser_team, team_score, tournament_date,
            tournament_name_pre, draw_gender, {},
        )
        if row:
            rows.append(row)
    if rows:
        log("INFO", f"   \U0001f9fe Sidearm box score (deterministic): {len(rows)} match(es)")
    return rows


def _extract_box_score(client, claude_key, openai_key, system, url, log, tele, *, browser=None):
    """Fetch one box score and extract rows.

    Order: Claude (only when a key is configured) -> the auburn stats-XML
    fallback -> the deterministic Sidearm-HTML parser. The last path needs no AI,
    so a direct box-score link works even when no Claude key is set.
    """
    resp = _get_page(client, url, log, tele, browser=browser)
    if resp is None or not (200 <= resp.status_code < 300):
        tele.record_error(
            f"Box score fetch failed for {url} "
            f"(HTTP {getattr(resp, 'status_code', 'none')})."
        )
        return []
    content = resp.text or ""
    raw_bytes = resp.content or b""
    content_type = resp.headers.get("Content-Type", "")

    if claude_key and system:
        rows = _core_extract(
            client, claude_key, openai_key, system, url, content,
            raw_bytes, content_type, log, tele,
        )
        if rows:
            return rows
        log(
            "WARN",
            "\u26a0\ufe0f Claude returned no rows \u2014 falling back to the "
            "deterministic parser (best-effort)",
        )
    rows = _auburn_extract(client, openai_key, url, content, log, tele)
    if rows:
        return rows
    return _sidearm_html_extract(url, content, log, tele)


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

    # ---- extraction mode (Claude optional, OpenAI optional) --------------
    # Prefer the per-scraper key saved in the Lab → Settings tab; fall back to
    # the env-sourced settings.CLAUDE_KEYS. Comma-separate to rotate several.
    # Claude is NO LONGER mandatory: a direct box-score link whose page embeds
    # every match (e.g. a Sidearm boxscore) is parsed deterministically with no
    # AI. The run still fails honestly later if nothing at all is extracted.
    scraper_key = (getattr(scraper, "claude_api_key", "") or "").strip()
    if scraper_key:
        claude_keys = [k.strip() for k in scraper_key.split(",") if k.strip()]
    else:
        claude_keys = [k for k in (getattr(settings, "CLAUDE_KEYS", []) or []) if k]

    openai_key = (getattr(settings, "OPENAI_API_KEY", "") or "").strip()
    if openai_key:
        log("INFO", "\U0001f513 OpenAI fallback enabled (tournament_date / auburn normalization)")
    else:
        log("INFO", "\u2139\ufe0f OpenAI fallback disabled (OPENAI_API_KEY unset) \u2014 fields left as-is")

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

    # The Claude prompt is only needed for the AI path; load it when a key is
    # present. A missing prompt downgrades to deterministic-only (not a failure).
    system = ""
    if claude_keys:
        system = _load_prompt()
        if not system:
            log(
                "WARN",
                f"\u26a0\ufe0f Claude prompt missing/empty ({PROMPT_PATH}) \u2014 "
                "AI extraction disabled; deterministic parsing only",
            )
            claude_keys = []
    if claude_keys:
        log("INFO", "\U0001f9e0 AI extraction enabled (Claude) \u2014 deterministic parser as fallback")
    else:
        log(
            "INFO",
            "\u2139\ufe0f No Claude key \u2014 deterministic box-score parsing only "
            "(direct links such as Sidearm pages)",
        )

    proxies = build_proxies(scraper, log)

    # Anti-bot fallback: a few athletics hosts answer the curl_cffi client with a
    # 403 JS challenge it can't solve. For those a patchright Chromium with a
    # PERSISTENT profile (clearance cookies survive across pages/runs) re-fetches
    # the page. Serialized internally to one browser at a time; see
    # _BrowserFallback. allowed_hosts=None mirrors the curl client (college
    # crawls arbitrary discovered athletics hosts); the SSRF public-IP guard
    # still applies inside BrowserClient.
    profile_root = getattr(settings, "SCRAPER_BROWSER_PROFILE_DIR", "")
    profile_dir = os.path.join(profile_root, scraper.slug) if profile_root else None
    browser_fb = _BrowserFallback(
        scraper=scraper, log=log, tele=tele, allowed_hosts=None, profile_dir=profile_dir,
    )

    # ---- phase 1 · discover box-score URLs -------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering box scores \u2500\u2500\u2500\u2500")
    box_scores = []
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        for url in urls:
            log("INFO", f"\U0001f50e Classifying {url}")
            try:
                box_scores.extend(_discover(discovery, url, log, tele, browser=browser_fb))
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
                browser=browser_fb,
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
    row_count = len(new_rows)

    log("INFO", "\u2500\u2500\u2500\u2500 summary \u2500\u2500\u2500\u2500")
    log(
        "INFO",
        f"\U0001f9ee {extracted} match(es) extracted \u2014 {row_count} new, "
        f"{skipped} already in the database",
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
    log("INFO", f"{icon} Run finished \u2014 status={status}, new_rows={row_count}")
    items_csv = college_store.to_csv(new_rows) if new_rows else ""
    return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
