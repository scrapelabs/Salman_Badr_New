"""Shared engine for the **itftennis.com** circuit family.

The production framework ships five spiders that all scrape the same
``www.itftennis.com`` JSON API and differ only by a *circuit code* and a few
constant labels: ``itftennis_juniors`` (JT), ``itftennis_masters`` (VT, the
seniors tour), ``itftennis_mens`` (MT) and ``itftennis_womens`` (WT). (The bare
``itftennis`` package is the shared engine itself, not a runnable circuit.) This
module ports that single ``InfoParser`` engine onto MatchMiner's shared HTTP
client (:mod:`accounts.live_scrapers._http`) + telemetry, parameterised by an
:class:`ITFConfig` so each circuit is a thin wrapper (mirroring how
:mod:`accounts.live_scrapers._ts_tournament` backs the tournamentsoftware
wrappers).

The real-time start form collects **either** a tournament URL **or** a date
window (``input_kind = date_range_or_url``):

* **tournament URL** — scrape that single tournament directly;
* **date range** — page the public calendar
  (``/tennis/api/TournamentApi/GetCalendar``) between the two dates for the
  configured circuit and scrape every tournament found.

For each tournament the crawl walks: the tournament HTML page (name / surface /
city / dates from the embedded JSON-LD) → ``GetEventFilters`` (the draw matrix)
→ ``GetDrawsheet`` per draw (the matches) → ``GetHeadToHeadPlayerDetails`` per
player (the date of birth, an XML document). Each match is emitted once
(de-duplicated by match id).

Unlike most other ports gender here is a **legitimate deterministic signal**, so
it is preserved: the men's/women's circuits are single-gender, and the
juniors/seniors draws carry an explicit ``playerTypeCode`` (boys/girls/men/women)
— no AI inference is involved (the source ``get_gender`` is pure code). Player
names are emitted as the API returns them (``"Family, Given"``). The production
spider's hard-coded litport proxy pool is dropped in favour of the scraper's
configured proxy. Because ``www.itftennis.com`` sits behind Imperva/Incapsula,
**phase 2 fetches through a patchright (stealth Chromium) browser**
(:mod:`accounts.live_scrapers._browser`): ``page.goto`` solves the Incapsula JS
challenge and the ``GetEventFilters`` / ``GetDrawsheet`` / player-DOB API calls
run as **in-page ``fetch()`` calls** that inherit the page's solved clearance and
real browser fingerprint (a bare ``context.request`` shares only cookies and is
still challenged) — so an egress whose IP would otherwise be challenged at the
API (a tiny block body with HTTP 200, and zero rows) still collects data. Phase
1 discovery stays on curl_cffi. If the browser can't launch the run **fails
honestly** (no fabricated rows), like the Stadion scrapers behind CloudFront.

``run(config, run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

import csv
import io
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urljoin

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from lxml import etree

from accounts.models import Run

from ._browser import BrowserClient, allow_async_unsafe, browser_proxy
from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

API_ROOT = "https://www.itftennis.com"
_HOST = "www.itftennis.com"
BALL_TYPE = "Yellow"
ID_TYPE = "ITF"
IMPORT_SOURCE = "ITF"
EVENT_TYPE = "Tournament"
PER_PAGE = 100

# JSON endpoints want a plain ``*/*`` Accept; the browser/HTML default headers
# from the client are fine for the tournament page + player XML.
_API_HEADERS = {"Accept": "*/*"}


@dataclass(frozen=True)
class ITFConfig:
    """Per-circuit constants for an itftennis.com tour.

    ``circuit_title`` drives single-gender detection (``"Mens"`` / ``"Womens"``);
    ``circuit_code`` selects the calendar circuit (``JT`` / ``VT`` / ``MT`` /
    ``WT``); ``event_category`` and ``sanction_body`` are emitted verbatim on
    every row.
    """

    label: str
    circuit_title: str
    circuit_code: str
    event_category: str
    sanction_body: str


# Items CSV columns — the shared ITF item schema (identical to the other
# MatchMiner scrapers). Title-cased header to match the downloadable files.
COLUMNS = [
    "match_id", "ball_type", "id_type", "draw_bracket_value", "draw_name",
    "draw_team_type", "tournament_name", "date", "round", "score",
    "winner_1_name", "winner_1_gender", "winner_1_dob", "winner_1_third_party_id",
    "winner_1_city", "winner_1_state", "winner_1_country",
    "winner_2_name", "winner_2_gender", "winner_2_dob", "winner_2_third_party_id",
    "winner_2_city", "winner_2_state", "winner_2_country",
    "loser_1_name", "loser_1_gender", "loser_1_dob", "loser_1_third_party_id",
    "loser_1_city", "loser_1_state", "loser_1_country",
    "loser_2_name", "loser_2_gender", "loser_2_dob", "loser_2_third_party_id",
    "loser_2_city", "loser_2_state", "loser_2_country",
    "outcome", "draw_gender", "draw_bracket_type", "draw_type",
    "tournament_city", "tournament_state", "tournament_country_code",
    "tournament_host", "tournament_location_type", "tournament_surface",
    "tournament_event_category", "tournament_event_grade",
    "tournament_import_source", "tournament_sanction_body",
    "winner_2_college", "loser_2_college", "tournament_event_type",
    "winner_1_college", "loser_1_college",
    "tournament_url", "tournament_country", "tournament_start_date",
    "tournament_end_date",
]
HEADER = [c.replace("_", " ").title() for c in COLUMNS]

_RE_TRAIL_ID = re.compile(r"([A-Za-z0-9\-]+)$")
_RE_PROFILE_ID = re.compile(r"/(\d{6,})/")


# ======================================================================
# small helpers
# ======================================================================
def _ns(sel, xpath):
    """``normalize-space(xpath)`` → stripped string (mirrors fctcore.parse_field)."""
    value = sel.xpath(f"normalize-space({xpath})").get()
    return value.strip() if value else ""


def _to_mdy(text, fmt):
    """Parse ``text`` with ``fmt`` → ``MM/DD/YYYY``, or ``""``."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
    except ValueError:
        return ""


def _jsonld(sel):
    """The embedded JSON-LD ``<script>`` carrying the tournament dates, as a dict."""
    raw = sel.xpath("//script[contains(., 'startDate')]/text()").get()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("startDate"):
                return entry
        return {}
    return data if isinstance(data, dict) else {}


# ======================================================================
# discovery
# ======================================================================
def _discover_one(client, url, log):
    """Resolve a single tournament URL to one ``{id, url}`` dict (or ``[]``)."""
    sel = client.get_selector(url)
    if sel is None:
        log("WARN", "\u26a0\ufe0f Could not load the supplied tournament URL")
        return []
    tj = _jsonld(sel)
    tournament_url = urljoin(API_ROOT + "/", tj.get("url", "")) if tj.get("url") else url
    match = _RE_TRAIL_ID.search(tournament_url.strip("/"))
    tournament_id = match.group(1) if match else ""
    if not tournament_id:
        log("WARN", "\u26a0\ufe0f Supplied URL did not resolve to a tournament")
        return []
    return [{"tournament_id": tournament_id, "tournament_url": tournament_url}]


def _discover_range(client, cfg, start_date, end_date, log):
    """Page ``GetCalendar`` for the circuit between two ``YYYY-MM-DD`` dates."""
    index_link = f"{API_ROOT}/tennis/api/TournamentApi/GetCalendar"
    params = {
        "circuitCode": cfg.circuit_code,
        "searchString": "",
        "skip": "0",
        "take": str(PER_PAGE),
        "nationCodes": "",
        "zoneCodes": "",
        "dateFrom": start_date,
        "dateTo": end_date,
        "indoorOutdoor": "",
        "categories": "",
        "isOrderAscending": "true",
        "orderField": "startDate",
        "surfaceCodes": "",
    }
    tournaments = []
    seen = set()

    def collect(payload):
        for data in (payload.get("items") or []):
            tournament_id = data.get("tournamentKey", "") or ""
            if not tournament_id or tournament_id in seen:
                continue
            seen.add(tournament_id)
            tournaments.append(
                {
                    "tournament_id": tournament_id,
                    "tournament_url": urljoin(
                        API_ROOT + "/", data.get("tournamentLink", "")
                    ),
                }
            )

    first = client.get_json(index_link, params=params, headers=_API_HEADERS)
    if not first:
        log(
            "WARN",
            "\u26a0\ufe0f GetCalendar returned no JSON \u2014 the host likely "
            "served an anti-bot challenge (a residential proxy is required)",
        )
        return []
    count_result = first.get("totalItems", 0) or 0
    count_page = math.ceil(count_result / PER_PAGE) if count_result else 0
    log(
        "INFO",
        f"   \U0001f4c5 circuit {cfg.circuit_code}: {count_result} tournament(s), "
        f"{count_page or 1} page(s)",
    )
    collect(first)

    for page in range(1, count_page):
        params["skip"] = str(page * PER_PAGE)
        payload = client.get_json(index_link, params=params, headers=_API_HEADERS)
        if not payload:
            break
        collect(payload)
        log("INFO", f"   \U0001f50e page {page + 1}: {len(tournaments)} so far")
    return tournaments


# ======================================================================
# filters → draw matrix (pure recursion, ported verbatim)
# ======================================================================
def _parse_filters(results, parent=None, desc_map=None):
    """Flatten ``GetEventFilters`` into ``(code_list, desc_map)``.

    ``code_list`` is one draw-parameter dict per leaf filter combination;
    ``desc_map`` maps ``dataName -> {valueCode: valueDesc}`` for label lookups.
    """
    if parent is None:
        parent = {}
    if desc_map is None:
        desc_map = {}

    tournament_id = results.get("tournamentId")
    tour_type = results.get("tourType")
    filters = results.get("filters") or []
    week_number = 0
    code_list = []

    for f in filters:
        current = parent.copy()
        data_name = f.get("dataName")
        value_code = f.get("valueCode")
        value_desc = f.get("valueDesc")

        if data_name and value_code:
            current[data_name] = value_code
            desc_map.setdefault(data_name, {})
            if value_desc:
                desc_map[data_name][value_code] = value_desc

        sub = f.get("subFilter")
        if sub:
            sub_code_list, desc_map = _parse_filters(
                {"filters": sub, "tournamentId": tournament_id, "tourType": tour_type},
                current,
                desc_map,
            )
            code_list.extend(sub_code_list)
        else:
            code_list.append(
                {
                    "tournamentId": tournament_id,
                    "tourType": tour_type,
                    "weekNumber": week_number,
                    **current,
                }
            )
    return code_list, desc_map


# ======================================================================
# match-record extraction (ported verbatim from the framework's InfoParser)
# ======================================================================
def _get_gender(cfg, player_type_code):
    """Deterministic ``(draw_gender, player_gender)`` — no AI, pure rules."""
    title = cfg.circuit_title.lower()
    if title == "mens":
        return "Male", "M"
    if title == "womens":
        return "Female", "F"
    code = (player_type_code or "").lower()
    if code in ("m", "b"):
        return "Male", "M"
    if code in ("w", "g"):
        return "Female", "F"
    if code in ("u",):
        return "Mixed", ""
    return "", ""


def _format_name(player):
    fn = player.get("familyName") or ""
    gn = player.get("givenName") or ""
    return f"{fn}, {gn}".strip(", ")


def _player_id(player):
    if player.get("playerId"):
        return player["playerId"]
    link = player.get("profileLink") or ""
    m = _RE_PROFILE_ID.search(link)
    return int(m.group(1)) if m else ""


def _build_score(winner_scores, loser_scores):
    """Build ``"6-3, 6-4;"`` from the two teams' per-set score arrays."""
    parts = []
    n = min(len(winner_scores), len(loser_scores))
    for i in range(n):
        ws = winner_scores[i]
        ls = loser_scores[i]
        if ws is None and ls is None:
            continue
        wv = ws.get("score") if ws else None
        lv = ls.get("score") if ls else None
        if wv is None and lv is None:
            continue
        if ws and ws.get("losingScore") is not None and wv is None:
            wv = ws.get("score")
            lv = ws.get("losingScore")
        if wv is None:
            wv = 0
        if lv is None:
            lv = 0
        parts.append(f"{int(wv)}-{int(lv)}")
    return ", ".join(parts) + ";" if parts else ""


def _record_from_match(match, round_desc=None):
    """Extract one singles/doubles match into a flat record dict."""
    rec = {
        "score": "", "outcome": "", "matchId": match.get("matchId"),
        "round": round_desc or "",
        "winner_1_name": "", "winner_1_third_party_id": "", "winner_1_country": "",
        "winner_2_name": "", "winner_2_third_party_id": "", "winner_2_country": "",
        "loser_1_name": "", "loser_1_third_party_id": "", "loser_1_country": "",
        "loser_2_name": "", "loser_2_third_party_id": "", "loser_2_country": "",
    }
    teams = match.get("teams") or []
    if len(teams) < 2:
        return rec

    winner_idx = None
    for idx, t in enumerate(teams):
        if t.get("isWinner"):
            winner_idx = idx
            break
    if winner_idx is None:
        sums = []
        for t in teams:
            ssum = 0
            for s in (t.get("scores") or []):
                if s and isinstance(s.get("score"), (int, float)):
                    ssum += s.get("score")
            sums.append(ssum)
        winner_idx = 0 if sums and sums[0] >= sums[1] else 1

    loser_idx = 1 - winner_idx
    winner_team = teams[winner_idx]
    loser_team = teams[loser_idx]
    winner_players = [p for p in (winner_team.get("players") or []) if p]
    loser_players = [p for p in (loser_team.get("players") or []) if p]

    if len(winner_players) >= 1:
        rec["winner_1_name"] = _format_name(winner_players[0])
        rec["winner_1_third_party_id"] = _player_id(winner_players[0])
        rec["winner_1_country"] = winner_players[0].get("nationality") or ""
    if len(winner_players) >= 2:
        rec["winner_2_name"] = _format_name(winner_players[1])
        rec["winner_2_third_party_id"] = _player_id(winner_players[1])
        rec["winner_2_country"] = winner_players[1].get("nationality") or ""
    if len(loser_players) >= 1:
        rec["loser_1_name"] = _format_name(loser_players[0])
        rec["loser_1_third_party_id"] = _player_id(loser_players[0])
        rec["loser_1_country"] = loser_players[0].get("nationality") or ""
    if len(loser_players) >= 2:
        rec["loser_2_name"] = _format_name(loser_players[1])
        rec["loser_2_third_party_id"] = _player_id(loser_players[1])
        rec["loser_2_country"] = loser_players[1].get("nationality") or ""

    rec["score"] = _build_score(
        winner_team.get("scores", []), loser_team.get("scores", [])
    )
    rec["outcome"] = match.get("resultStatusDesc") or match.get("playStatusDesc") or ""
    # The ITF feed labels a finished singles/doubles match "Played and completed";
    # the production spider normalises that to "Completed" *before* the
    # (completed|retired) keep-filter in _scrape_tournament. Without this every
    # real match is rejected and the run writes 0 rows despite healthy discovery.
    if rec["outcome"] == "Played and completed":
        rec["outcome"] = "Completed"
    return rec


def _extract_records(data):
    """Walk a ``GetDrawsheet`` payload and return de-duplicated match records."""
    records = []
    if isinstance(data, dict) and "koGroups" in data:
        for group in data.get("koGroups") or []:
            for r in group.get("rounds") or []:
                rd = r.get("roundDesc")
                for m in r.get("matches") or []:
                    records.append(_record_from_match(m, rd))

    def walk(obj, current_round_desc=None):
        if isinstance(obj, dict):
            if "roundDesc" in obj:
                current_round_desc = obj.get("roundDesc")
            for k, v in obj.items():
                if k == "matches" and isinstance(v, list):
                    for m in v:
                        if isinstance(m, dict) and m.get("matchId"):
                            records.append(_record_from_match(m, current_round_desc))
                else:
                    walk(v, current_round_desc)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, current_round_desc)

    walk(data)

    seen = {}
    out = []
    for r in records:
        mid = r.get("matchId")
        if mid and mid not in seen:
            seen[mid] = True
            out.append(r)
    return out


# ======================================================================
# date of birth (player XML), cached per run
# ======================================================================
_DOB_NS = {"ns": "http://schemas.datacontract.org/2004/07/Itf.Tennis.Core.Models.Api"}

# ``GetHeadToHeadPlayerDetails`` is an ASP.NET endpoint that content-negotiates:
# asked with the browser ``fetch`` default ``Accept: */*`` it returns JSON, but
# the DOB parser below expects the XML document. So the lookup must explicitly
# prefer XML (mirroring the original reference scraper's headers) or every DOB
# silently comes back blank.
_DOB_REQUEST_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
}


def _dob_from_value(value):
    """Normalize an ITF ``DateOfBirth`` into ``MM/DD/YYYY`` (or "").

    Handles the ISO form (``2003-05-14T00:00:00``) and the WCF DataContract JSON
    epoch (``/Date(1052870400000-0000)/``) the same endpoint emits as JSON.
    """
    value = (value or "").strip()
    if not value:
        return ""
    m = re.match(r"/Date\((-?\d+)", value)
    if m:
        try:
            return (
                datetime(1970, 1, 1) + timedelta(milliseconds=int(m.group(1)))
            ).strftime("%m/%d/%Y")
        except (ValueError, OverflowError):
            return ""
    return _to_mdy(value[:19], "%Y-%m-%dT%H:%M:%S")


def _extract_dob(resp):
    """Pull ``DateOfBirth`` (→ m/d/Y) out of a ``GetHeadToHeadPlayerDetails``
    response. Prefers the XML document the endpoint returns when XML is
    requested, and falls back to the JSON shape if it negotiated that instead.
    Returns "" for a missing/blank DOB or an unparseable body."""
    body = getattr(resp, "content", b"") or b""
    try:
        tree = etree.fromstring(body)
        text = tree.xpath("string(./ns:DateOfBirth)", namespaces=_DOB_NS)
        if text:
            return _dob_from_value(text)
    except Exception:  # noqa: BLE001 - not XML; try the JSON shape next
        pass
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return _dob_from_value(
                data.get("DateOfBirth") or data.get("dateOfBirth") or ""
            )
    except Exception:  # noqa: BLE001 - bad body can't kill the run
        pass
    return ""


class _DobResolver:
    """Per-tournament player-DOB lookups, tuned for the Incapsula *rate*
    re-challenge that ``GetHeadToHeadPlayerDetails`` trips under a burst.

    The tournament's browser already holds Incapsula clearance (that's why the
    drawsheet API calls succeed), but dozens of DOB fetches per second from one
    session re-trigger a rate challenge. So:

    * **pace** each lookup (a short sleep) so the per-IP rate stays under the
      re-challenge threshold — most DOBs resolve in the *same* browser, fast;
    * when a lookup is **still** blocked, *rotate* — relaunch the browser for a
      fresh exit IP (on a rotating proxy) + re-solved clearance, then retry that
      one lookup, bounded to ``max_rotations`` relaunches;
    * DOB is **best-effort** — once the rotation budget is spent it yields ""
      rather than stalling the run, so a match row is never lost over a DOB.

    Results are cached per run (shared across the worker threads) so a repeat
    player costs no network call. One resolver owns one browser (one thread);
    relaunching it never touches another thread's browser.
    """

    def __init__(
        self, client, tournament_url, log, cache, lock, *, delay, max_rotations
    ):
        self.client = client
        self.tournament_url = tournament_url
        self.log = log
        self.cache = cache
        self.lock = lock
        self.delay = max(0.0, delay)
        self.max_rotations = max(0, max_rotations)

    def resolve(self, name, player_id):
        if not (name and player_id):
            return ""
        key = str(player_id)
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        dob = self._lookup(player_id)
        with self.lock:
            # Re-check under the lock: another worker thread may have resolved
            # this same player while we were looking it up. A best-effort blank
            # must never clobber a good DOB already cached by that thread (that
            # would poison every later row for the player), so only write when we
            # have a value or the slot is still empty.
            cached = self.cache.get(key, "")
            if cached and not dob:
                return cached
            self.cache[key] = dob
        return dob

    def _lookup(self, player_id):
        url = (
            f"{API_ROOT}/tennis/Api/PlayerApi/GetHeadToHeadPlayerDetails"
            f"?playerId={player_id}"
        )
        for rotation in range(self.max_rotations + 1):
            # Pace the burst: the clearance cookie stays valid, but too many DOB
            # fetches per second from one IP re-trigger the rate challenge.
            if self.delay:
                time.sleep(self.delay)
            resp = self.client.get(url, headers=_DOB_REQUEST_HEADERS)
            if resp is not None and 200 <= resp.status_code < 300:
                return _extract_dob(resp)
            # Still blocked (``get`` already retried the in-page fetch). Rotate to
            # a fresh IP + clearance and retry, unless the budget is spent.
            if rotation < self.max_rotations:
                self.log(
                    "INFO",
                    "   \U0001f504 DOB blocked \u2014 rotating browser "
                    "(fresh IP) and retrying",
                )
                try:
                    self.client.relaunch()
                    # Re-solve the Incapsula challenge on the fresh identity so
                    # the next in-page DOB fetch inherits clearance.
                    self.client.get_selector(self.tournament_url)
                except Exception as exc:  # noqa: BLE001 - best-effort rotation
                    self.log(
                        "WARN",
                        redact_secrets(
                            f"   \u26a0\ufe0f DOB rotation failed: "
                            f"{exc.__class__.__name__}: {exc}"
                        ),
                    )
        return ""  # best-effort: never stall a run on a stubborn DOB


# ======================================================================
# per-tournament crawl
# ======================================================================
def _scrape_tournament(client, cfg, tournament, emit, log, dob_cache, dob_lock):
    """Scrape one tournament: page → filters → drawsheets → rows via ``emit``."""
    tournament_url = tournament.get("tournament_url", "")
    tournament_id = tournament.get("tournament_id", "")
    # The page load (Incapsula challenge + full settle) is the slow step, so name
    # which tournament is loading *before* we block on it — otherwise a stall looks
    # like a hang. The friendly name replaces this once the page metadata parses.
    log("INFO", f"   \u23f3 loading {tournament_id or tournament_url} \u2026")
    sel = client.get_selector(tournament_url)
    if sel is None:
        return 0

    surface = _ns(sel, '//span[@id="ga__tournament-surface"]').split("-")[0].strip()
    name = _ns(sel, '//h1[@id="ga__tournament-name"]')
    tj = _jsonld(sel)
    start_date = _to_mdy(tj.get("startDate", ""), "%m/%d/%Y %I:%M:%S %p")
    end_date = _to_mdy(tj.get("endDate", ""), "%m/%d/%Y %I:%M:%S %p")
    location = tj.get("location") or {}
    city = location.get("name", "") if isinstance(location, dict) else ""
    country = ""
    if isinstance(location, dict):
        country = (location.get("address") or {}).get("addressCountry", "")

    if not (name and start_date and end_date):
        return 0

    filters = client.get_json(
        f"{API_ROOT}/tennis/api/TournamentApi/GetEventFilters"
        f"?tournamentKey={tournament_id.lower()}",
        headers=_API_HEADERS,
    )
    if not filters:
        return 0

    where = ", ".join(p for p in (city, country) if p)
    log(
        "INFO",
        f"   \U0001f3df\ufe0f {name}"
        + (f" \u2014 {where}" if where else "")
        + (f" \u00b7 {surface}" if surface else ""),
    )

    tctx = {
        "tournament_name": name,
        "tournament_url": tournament_url,
        "tournament_start_date": start_date,
        "tournament_end_date": end_date,
        "tournament_surface": surface,
        "tournament_city": city,
        "tournament_country": country,
        "tournament_country_code": (country or "")[:3].upper(),
    }

    dob_resolver = _DobResolver(
        client, tournament_url, log, dob_cache, dob_lock,
        delay=getattr(settings, "SCRAPER_ITF_DOB_DELAY_MS", 250) / 1000.0,
        max_rotations=getattr(settings, "SCRAPER_ITF_DOB_MAX_ROTATIONS", 2),
    )

    emitted = 0
    code_list, desc_map = _parse_filters(filters)
    for json_data in code_list:
        draw_team_type = desc_map.get("matchTypeCode", {}).get(
            json_data.get("matchTypeCode"), ""
        )
        player_type_code = json_data.get("playerTypeCode", "")
        draw_name = " ".join(
            d
            for d in [
                desc_map.get("ageCategoryCode", {}).get(
                    json_data.get("ageCategoryCode"), ""
                ),
                desc_map.get("playerTypeCode", {}).get(
                    json_data.get("playerTypeCode"), ""
                ),
                desc_map.get("matchTypeCode", {}).get(
                    json_data.get("matchTypeCode"), ""
                ),
                desc_map.get("eventClassificationCode", {}).get(
                    json_data.get("eventClassificationCode"), ""
                ),
            ]
            if d
        )
        draw_gender, player_gender = _get_gender(cfg, player_type_code)
        if not (draw_gender and player_gender):
            continue

        params = {k: str(v) for k, v in json_data.items()}
        drawsheet = client.get_json(
            f"{API_ROOT}/tennis/api/TournamentApi/GetDrawsheet",
            params=params,
            headers=_API_HEADERS,
        )
        if not drawsheet:
            continue

        for rec in _extract_records(drawsheet):
            if (rec.get("outcome", "") or "").lower() not in ("completed", "retired"):
                continue
            row = _build_row(
                dob_resolver, cfg, tctx, draw_name, draw_team_type, draw_gender,
                player_gender, rec,
            )
            if emit(row):
                emitted += 1

    log(
        "INFO",
        f"   \u2705 {name}: {emitted} match(es)"
        if emitted
        else f"   \u2205 {name}: no completed matches",
    )
    return emitted


def _build_row(
    dob_resolver, cfg, tctx, draw_name, draw_team_type, draw_gender,
    player_gender, rec,
):
    """Assemble one items row from a match record + DOB lookups."""
    w1_name = rec.get("winner_1_name", "")
    w2_name = rec.get("winner_2_name", "")
    l1_name = rec.get("loser_1_name", "")
    l2_name = rec.get("loser_2_name", "")

    return {
        "match_id": rec.get("matchId", ""),
        "ball_type": BALL_TYPE,
        "id_type": ID_TYPE,
        "draw_bracket_value": "",
        "draw_name": draw_name,
        "draw_team_type": draw_team_type,
        "tournament_name": tctx["tournament_name"],
        "date": tctx["tournament_start_date"],
        "round": rec.get("round", ""),
        "score": rec.get("score", ""),
        "winner_1_name": w1_name,
        "winner_1_gender": player_gender if w1_name else "",
        "winner_1_dob": dob_resolver.resolve(
            w1_name, rec.get("winner_1_third_party_id", "")
        ),
        "winner_1_third_party_id": rec.get("winner_1_third_party_id", ""),
        "winner_1_city": "",
        "winner_1_state": "",
        "winner_1_country": rec.get("winner_1_country", ""),
        "winner_2_name": w2_name,
        "winner_2_gender": player_gender if w2_name else "",
        "winner_2_dob": dob_resolver.resolve(
            w2_name, rec.get("winner_2_third_party_id", "")
        ),
        "winner_2_third_party_id": rec.get("winner_2_third_party_id", ""),
        "winner_2_city": "",
        "winner_2_state": "",
        "winner_2_country": rec.get("winner_2_country", ""),
        "loser_1_name": l1_name,
        "loser_1_gender": player_gender if l1_name else "",
        "loser_1_dob": dob_resolver.resolve(
            l1_name, rec.get("loser_1_third_party_id", "")
        ),
        "loser_1_third_party_id": rec.get("loser_1_third_party_id", ""),
        "loser_1_city": "",
        "loser_1_state": "",
        "loser_1_country": rec.get("loser_1_country", ""),
        "loser_2_name": l2_name,
        "loser_2_gender": player_gender if l2_name else "",
        "loser_2_dob": dob_resolver.resolve(
            l2_name, rec.get("loser_2_third_party_id", "")
        ),
        "loser_2_third_party_id": rec.get("loser_2_third_party_id", ""),
        "loser_2_city": "",
        "loser_2_state": "",
        "loser_2_country": rec.get("loser_2_country", ""),
        "outcome": rec.get("outcome", ""),
        "draw_gender": draw_gender,
        "draw_bracket_type": "",
        "draw_type": "",
        "tournament_city": tctx["tournament_city"],
        "tournament_state": "",
        "tournament_country_code": tctx["tournament_country_code"],
        "tournament_host": "",
        "tournament_location_type": "",
        "tournament_surface": tctx["tournament_surface"],
        "tournament_event_category": cfg.event_category,
        "tournament_event_grade": "",
        "tournament_import_source": IMPORT_SOURCE,
        "tournament_sanction_body": cfg.sanction_body,
        "winner_2_college": "",
        "loser_2_college": "",
        "tournament_event_type": EVENT_TYPE,
        "winner_1_college": "",
        "loser_1_college": "",
        "tournament_url": tctx["tournament_url"],
        "tournament_country": tctx["tournament_country"],
        "tournament_start_date": tctx["tournament_start_date"],
        "tournament_end_date": tctx["tournament_end_date"],
    }


# ======================================================================
# run
# ======================================================================
def _window(run_obj):
    today = timezone.localdate()
    start = run_obj.date_from or today
    end = run_obj.date_to or today
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _client_summary(scraper, rotate):
    """One-line browser-client description for the phase-2 header.

    The per-tournament browsers launch silently (``announce=False``) so this is
    emitted ONCE per run instead of once per launch. Mirrors
    ``BrowserClient.__enter__``'s wording and defers the direct-vs-proxy call to
    ``browser_proxy`` (the single source of truth, which never exposes the raw
    address).
    """
    channel = (getattr(settings, "SCRAPER_BROWSER_CHANNEL", "") or "").strip()
    engine = f"Google {channel.title()}" if channel else "Chromium"
    mode = (
        "headless" if getattr(settings, "SCRAPER_BROWSER_HEADLESS", True) else "headed"
    )
    # Mirror make_browser: a persistent profile is only used when rotation is OFF
    # *and* a profile dir is configured; otherwise every launch is ephemeral.
    profile_root = (getattr(settings, "SCRAPER_BROWSER_PROFILE_DIR", "") or "").strip()
    persist = (
        "persistent profile"
        if (not rotate and profile_root)
        else "ephemeral profile"
    )
    proxy = scraper.proxy
    if browser_proxy(proxy):
        kind = proxy.get_kind_display() if hasattr(proxy, "get_kind_display") else "?"
        conn = f"via {kind} proxy '{getattr(proxy, 'name', '?')}'"
        if rotate:
            conn += " (rotating IP)"
    else:
        conn = "direct \u2014 no proxy"
    return (
        f"\U0001f310 HTTP client: patchright {engine} ({mode}, {persist}) {conn}"
    )


def run(cfg, run_obj, log):
    """Execute one itftennis circuit scrape; return the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    params = run_obj.params or {}
    tournament_url = (params.get("tournament_url") or "").strip()

    if tournament_url:
        log("INFO", f"\U0001f3be {cfg.label} starting \u2014 single tournament URL")
    else:
        start_date, end_date = _window(run_obj)
        log("INFO", f"\U0001f3be {cfg.label} starting \u2014 {start_date} \u2192 {end_date}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)

    # ---- phase 1 · discovery ------------------------------------------
    # Date-range discovery uses curl_cffi GetCalendar. The single-URL path is
    # resolved *inside* the browser below, because the tournament page is the
    # very resource Incapsula challenges — curl_cffi would 0-row it.
    tournaments = []
    if not tournament_url:
        log(
            "INFO",
            "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering tournaments "
            "\u2500\u2500\u2500\u2500",
        )
        with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
            tournaments = _discover_range(discovery, cfg, start_date, end_date, log)
        log("INFO", f"\U0001f4cb {len(tournaments)} tournament(s) discovered")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}
    dob_cache = {}
    dob_lock = threading.Lock()

    def emit(row):
        # Each match id is globally unique on the ITF feed; dedupe on it (or a
        # content key when an id is missing) so a re-listed match isn't doubled.
        key = row.get("match_id") or (
            row.get("tournament_url", ""),
            row.get("draw_name", ""),
            row.get("round", ""),
            row.get("winner_1_name", ""),
            row.get("loser_1_name", ""),
            row.get("score", ""),
        )
        with lock:
            if key in seen:
                return False
            seen.add(key)
            writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
            counter["rows"] += 1
        log(
            "INFO",
            f"      \U0001f3be {row.get('draw_team_type', '')} "
            f"{row.get('round', '')}: "
            f"{row.get('winner_1_name') or '?'} def. "
            f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}]".rstrip(),
        )
        return True

    def crawl_one(browser, tournament):
        try:
            _scrape_tournament(
                browser, cfg, tournament, emit, log, dob_cache, dob_lock
            )
        except Exception as exc:  # noqa: BLE001 - a bad tournament can't kill the run
            tele.record_error(
                redact_secrets(
                    f"Tournament {tournament.get('tournament_url', '')} failed: {exc}"
                ),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f tournament failed: {exc.__class__.__name__}: {exc}"
                ),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(
                progress_done=F("progress_done") + 1
            )

    # ---- phase 2 · scraping (patchright browser) ----------------------
    # itftennis sits behind Imperva/Incapsula, so the per-tournament work runs
    # inside a real patchright Chromium: ``page.goto`` solves the JS challenge
    # that 0-rows a curl_cffi run behind a challenged proxy, and the API calls
    # reuse the solved cookies. A single-tournament URL is also *discovered*
    # here (its page is the challenged resource). The Playwright sync API is
    # single-thread bound, but in per-request rotation mode each tournament
    # already gets its *own* fresh browser, so the per-tournament loop fans out
    # across ``Scraper.threads`` worker threads \u2014 one independent browser
    # (own fingerprint + IP) per thread, up to ``workers`` Chromium instances
    # launched concurrently. The whole concurrent phase opts out of Django's
    # async-safety guard once (allow_async_unsafe), because that env var is
    # process-global and a per-browser set/restore would race across threads.
    # The non-rotate path reuses one shared browser and stays sequential.
    if tournament_url or tournaments:
        profile_root = getattr(settings, "SCRAPER_BROWSER_PROFILE_DIR", "")
        user_data_dir = (
            os.path.join(profile_root, scraper.slug) if profile_root else None
        )
        rotate = getattr(settings, "SCRAPER_BROWSER_ROTATE_PER_REQUEST", True)

        def make_browser():
            # In rotation mode every tournament gets a brand-new identity, so use
            # a throwaway ephemeral profile (a persistent dir would carry the very
            # cookie/fingerprint we are deliberately shedding) and rotate the
            # proxy IP. Off => one persistent session reused for the whole run.
            # manage_async_unsafe=False: the whole phase-2 block owns the
            # process-global DJANGO_ALLOW_ASYNC_UNSAFE via allow_async_unsafe(),
            # so each (possibly concurrent) client must not touch it itself.
            return BrowserClient(
                log=log,
                tele=tele,
                proxy=scraper.proxy,
                allowed_hosts=(_HOST,),
                headless=getattr(settings, "SCRAPER_BROWSER_HEADLESS", True),
                channel=getattr(settings, "SCRAPER_BROWSER_CHANNEL", "") or None,
                user_data_dir=None if rotate else user_data_dir,
                rotate_proxy_session=rotate,
                manage_async_unsafe=False,
                announce=False,
            )

        def announce_phase2():
            Run.objects.filter(pk=run_obj.pk).update(
                progress_total=len(tournaments), progress_done=0
            )
            log(
                "INFO",
                "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping tournaments "
                "(patchright) \u2500\u2500\u2500\u2500",
            )
            # The per-tournament browsers launch silently (announce=False) so the
            # identical client line isn't repeated once per tournament; say it once.
            log("INFO", _client_summary(scraper, rotate))

        def crawl_isolated(tournament):
            # One tournament in its *own* fresh browser. Used both serially and as
            # the ThreadPoolExecutor task, so a bad browser launch must never kill
            # the run (or take the pool down with it): record it, advance progress
            # once, and move on. ``crawl_one`` owns the per-tournament scrape error
            # plus the normal progress increment; this only covers the case where
            # the browser never opened (so progress is bumped here exactly once).
            try:
                with make_browser() as browser:
                    crawl_one(browser, tournament)
            except Exception as exc:  # noqa: BLE001 - one bad launch != dead run
                tele.record_error(
                    redact_secrets(
                        f"Browser launch failed for "
                        f"{tournament.get('tournament_url', '')}: {exc}"
                    ),
                    exc=exc,
                )
                log(
                    "WARN",
                    redact_secrets(
                        f"\u26a0\ufe0f browser launch failed: "
                        f"{exc.__class__.__name__}: {exc}"
                    ),
                )
                Run.objects.filter(pk=run_obj.pk).update(
                    progress_done=F("progress_done") + 1
                )

        try:
            # The browser phase makes ORM writes (log / telemetry / progress) while
            # a Playwright loop is live in each thread, so the whole block opts out
            # of Django's async-safety guard exactly once — the only safe scope when
            # the rotate path drives several browsers concurrently (that env var is
            # process-global; see allow_async_unsafe).
            with allow_async_unsafe():
                if rotate:
                    # Each tournament = a fresh browser (new fingerprint + IP), so
                    # the Incapsula challenge is re-solved per tournament and no
                    # single identity accumulates the signal that re-triggers the
                    # captcha "after a few records". Because every tournament is
                    # fully isolated, the loop parallelises cleanly across workers.
                    if tournament_url:
                        log(
                            "INFO",
                            "\u2500\u2500\u2500\u2500 phase 1 \u00b7 resolving tournament "
                            "(patchright) \u2500\u2500\u2500\u2500",
                        )
                        with make_browser() as browser:
                            tournaments = _discover_one(browser, tournament_url, log)
                        log(
                            "INFO",
                            f"\U0001f4cb {len(tournaments)} tournament(s) discovered",
                        )
                    if tournaments:
                        announce_phase2()
                        # One tournament can't be sped up by a pool; >1 fans out
                        # across up to ``workers`` concurrent browsers.
                        parallel = workers > 1 and len(tournaments) > 1
                        if parallel:
                            log(
                                "INFO",
                                f"\U0001f9f5 per-request rotation: up to {workers} "
                                f"browsers in parallel \u2014 a fresh browser + IP "
                                f"per tournament",
                            )
                            with ThreadPoolExecutor(max_workers=workers) as pool:
                                list(pool.map(crawl_isolated, tournaments))
                        else:
                            log(
                                "INFO",
                                "\U0001f504 per-request rotation: a fresh browser + "
                                "IP per tournament",
                            )
                            for tournament in tournaments:
                                crawl_isolated(tournament)
                else:
                    # Off => one persistent browser reused for the whole run. A
                    # single shared Playwright page can't be driven from many
                    # threads, so this path is inherently sequential whatever
                    # ``workers`` is set to.
                    with make_browser() as browser:
                        if tournament_url:
                            log(
                                "INFO",
                                "\u2500\u2500\u2500\u2500 phase 1 \u00b7 resolving "
                                "tournament (patchright) \u2500\u2500\u2500\u2500",
                            )
                            tournaments = _discover_one(browser, tournament_url, log)
                            log(
                                "INFO",
                                f"\U0001f4cb {len(tournaments)} tournament(s) discovered",
                            )
                        if tournaments:
                            announce_phase2()
                            if workers > 1:
                                log(
                                    "INFO",
                                    "\u2139\ufe0f rotation off \u2014 one shared "
                                    "browser, so this run is sequential",
                                )
                            for tournament in tournaments:
                                crawl_one(browser, tournament)
        except Exception as exc:  # noqa: BLE001 - launch/teardown failure
            tele.record_error(
                redact_secrets(f"Browser session failed: {exc}"), exc=exc
            )
            log(
                "ERROR",
                redact_secrets(
                    f"\U0001f6d1 patchright browser unavailable: "
                    f"{exc.__class__.__name__}: {exc}"
                ),
            )

    row_count = counter["rows"]
    log("INFO", "\u2500\u2500\u2500\u2500 summary \u2500\u2500\u2500\u2500")
    log("INFO", f"\U0001f4be Writing {row_count} row(s) to CSV")
    log(
        "INFO",
        f"\U0001f4ca Telemetry: {tele.request_count} request(s), "
        f"{tele.error_count} error(s)",
    )
    status = Run.Status.SUCCESS if row_count else Run.Status.FAILED
    icon = "\U0001f3c1" if status == Run.Status.SUCCESS else "\U0001f6d1"
    log("INFO", f"{icon} Run finished \u2014 status={status}, rows={row_count}")
    items_csv = buf.getvalue() if row_count else ""
    return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
