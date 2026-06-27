"""Belgium (Tennis & Padel Vlaanderen) results scraper.

Ports the production ``belgium_results`` spider onto MatchMiner's shared HTTP
client (:mod:`accounts.live_scrapers._http`) + telemetry. The source scrapes
``www.tennisenpadelvlaanderen.be``, which sits behind a Zenedge anti-bot
interstitial (see :mod:`accounts.live_scrapers._belgium_captcha`). The pipeline:

1. **Discovery** — query the public tournament search
   (``zoek-een-tornooi?...&dateRange=<iso>x<iso>``) and keep tournaments whose
   date range falls **entirely** inside the run window.
2. **Series** — each tournament's poule page lists its draws ("Reeks") via a
   "Meer info" datatable; follow each to its series page.
3. **Matches** — a series page renders each match as a ``<table class="game-table">``
   whose ``<td class="match-winner">`` holds the winner + score; the competitor
   rows carry ``<a userId=...>`` profile links. One CSV row per played match.

Input is a **date range** (``date_from`` / ``date_to``) *or* a single
``tournament_url`` (validated against the ``tennisenpadelvlaanderen.be`` allowlist
at the view layer); a URL skips discovery and scrapes that one tournament.

**Deterministic port.** The source's AI name/gender guessing
(``format_name_gender_claude``) and its ``PlayersBelgiumResultsModel`` cache are
dropped: player gender falls back to the draw's gender, and the
``third_party_id`` uses the profile-page id with a stable ``sha256_id(name)``
fallback (verbatim from the production ``helper.sha256_id``). The emitted file
uses the shared 61-column MatchMiner items schema (same as Brazil/Czech).

Every page fetch is routed through the captcha solver: a challenged page is
cleared before parsing. When the solver is unavailable (TensorFlow / the model
not present), challenged pages can't be cleared, so the run **fails honestly**
(empty 5-tuple + a diagnostic error) — like the Stadion scrapers without a
residential proxy.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import hashlib
import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from urllib.parse import urljoin

from django.db.models import F
from django.utils import timezone
from parsel import Selector

from accounts.models import Run

from ._belgium_captcha import (
    CaptchaSolver,
    CaptchaSolverUnavailable,
    is_challenge,
    materialize_uploaded_model,
)
from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

BASE = "https://www.tennisenpadelvlaanderen.be"
ALLOWED_HOSTS = ("www.tennisenpadelvlaanderen.be", "tennisenpadelvlaanderen.be")

# Items CSV columns — the shared MatchMiner items schema (same as Brazil/Czech).
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

# Page-fetch headers; the UA is left to curl_cffi's Chrome impersonation.
_HEADERS = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "en-US,en;q=0.9,fr;q=0.8,en-GB;q=0.7,nl;q=0.6",
    "upgrade-insecure-requests": "1",
}


# ---------------------------------------------------------------------------
# Text / id helpers
# ---------------------------------------------------------------------------
def _clean(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _join_text(sel):
    """Whitespace-collapsed concatenation of all descendant text of ``sel``."""
    return _clean(" ".join(sel.xpath(".//text()").getall()))


def sha256_id(s):
    """Stable synthetic id for a player without a site id.

    Verbatim from the production ``helper.sha256_id``: the first 8 bytes of the
    SHA-256 digest as a big-endian unsigned int, ``local_``-prefixed.
    """
    digest = hashlib.sha256((s or "").encode()).digest()
    return "local_" + str(int.from_bytes(digest[:8], "big"))


# ---------------------------------------------------------------------------
# Date parsing (ported from the source's TournamentParser)
# ---------------------------------------------------------------------------
_DATE_PATTERNS = (
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), ("y", "m", "d")),       # ISO
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"), ("d", "m", "y")),       # D/M/Y
    (re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})"), ("d", "m", "y")),  # D. M. Y
)


def _extract_date(text):
    for pattern, order in _DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            parts = dict(zip(order, map(int, m.groups())))
            return date(parts["y"], parts["m"], parts["d"])
    return None


def _find_all_dates(text):
    matches = []
    for pattern, order in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            parts = dict(zip(order, map(int, m.groups())))
            matches.append((m.start(), date(parts["y"], parts["m"], parts["d"])))
    matches.sort(key=lambda pair: pair[0])
    return [d for _, d in matches]


def parse_range(range_str):
    """Parse a tournament date label into ``(start, end)`` date objects.

    Handles ISO / slash / European-dotted dates, single-day entries (with stray
    times), and the European 'year only on the end part' shorthand.
    """
    range_str = (range_str or "").strip()
    parts = re.split(r"\s*[-\u2013]\s*", range_str)
    if len(parts) == 2:
        start_part, end_part = parts[0].strip(), parts[1].strip()
        year_match = re.search(r"(\d{4})\s*$", end_part)
        start_has_full_date = _extract_date(start_part) is not None
        if year_match and not start_has_full_date and re.search(r"\d", start_part):
            start_part = start_part.rstrip(". ") + ". " + year_match.group(1)
            s, e = _extract_date(start_part), _extract_date(end_part)
            if s and e:
                return s, e
    dates = _find_all_dates(range_str)
    if len(dates) == 1:
        return dates[0], dates[0]
    if len(dates) >= 2:
        return dates[0], dates[-1]
    raise ValueError(f"Unrecognized range format: {range_str!r}")


def _is_fully_inside(range_str, window_start, window_end):
    """True only if the tournament range is entirely within the window
    (inclusive). ``window_start`` / ``window_end`` are ``date`` objects."""
    start, end = parse_range(range_str)
    return start >= window_start and end <= window_end


# ---------------------------------------------------------------------------
# Fetch helper (challenge-aware)
# ---------------------------------------------------------------------------
def _fetch_html(client, url, solver, log):
    """GET ``url`` and return cleared HTML, or ``""``.

    Detects the Zenedge interstitial and clears it via ``solver``. If a page is
    challenged but no solver is available, returns ``""`` (honest fail).
    """
    resp = client.get(url, headers=_HEADERS)
    if resp is None or not (200 <= resp.status_code < 300):
        return ""
    html = resp.text
    if is_challenge(html):
        if solver is None:
            log("WARN", f"\U0001f6e1\ufe0f challenge on {url} but no captcha solver")
            return ""
        html = solver.solve_challenge(client, url, html)
        if not html:
            log("WARN", f"\u26a0\ufe0f could not clear challenge on {url}")
            return ""
    return html


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _discover_tournaments(client, solver, start_d, end_d, log):
    """Query the public search and return ``[{tournament_name, tournament_url}]``."""
    index_url = (
        f"{BASE}/zoek-een-tornooi?sportId=1&matchFormatId=E"
        f"&dateRange={start_d:%Y-%m-%d}x{end_d:%Y-%m-%d}"
        "&itemsPerPage=1000&offset=0#searchResultStart"
    )
    html = _fetch_html(client, index_url, solver, log)
    if not html:
        return []
    sel = Selector(text=html)
    tournaments = []
    seen = set()
    cards = sel.xpath(
        '//form[@id="_quick_results_WAR_vtvportletportlet_:quickResultsForm"]'
        '//div[contains(@class, "result-card")]'
    )
    for card in cards:
        pre_link = (
            card.xpath(
                './/div[@class="qr-button"]/a[contains(text(), "Meer info")]/@href'
            ).get()
            or ""
        ).strip()
        tdate = _join_text(card.xpath('.//span[@class="tornooi-date-range"]'))
        tname = _join_text(card.xpath('.//span[@class="club-name"]/parent::node()'))
        if not (pre_link and tdate):
            continue
        turl = urljoin(BASE + "/", pre_link)
        try:
            included = _is_fully_inside(tdate, start_d, end_d)
        except ValueError:
            continue
        if included and turl not in seen:
            seen.add(turl)
            tournaments.append({"tournament_name": tname, "tournament_url": turl})
    log("INFO", f"\U0001f50e {len(tournaments)} tournament(s) in window")
    return tournaments


def _scrape_tournament(client, solver, tournament, log):
    """Follow a tournament's series links and return all match row dicts."""
    tournament_url = tournament.get("tournament_url", "")
    html = _fetch_html(client, tournament_url, solver, log)
    if not html:
        return []
    sel = Selector(text=html)
    rows = []
    series_links = sel.xpath(
        '//div[@class="ui-datatable-tablewrapper"]//table//tr[not(th)]'
        '//td//a[contains(text(), "Meer info")]'
    )
    for a in series_links:
        href = (a.xpath("./@href").get() or "").strip()
        if not href:
            continue
        serie_url = urljoin(BASE + "/", href)
        rows.extend(_scrape_serie(client, solver, serie_url, log))
    return rows


def _scrape_serie(client, solver, serie_url, log):
    """Parse one series page's game-tables into match row dicts."""
    html = _fetch_html(client, serie_url, solver, log)
    if not html:
        return []
    sel = Selector(text=html)
    parser = Parser(sel, client, solver, log)
    rows = []
    cells = sel.xpath(
        '//table[contains(@class,"game-table")]//td[contains(@class,"match-winner")]'
    )
    for cell in cells:
        data = parser.parse_match(cell)
        if data:
            rows.append(data)
    return rows


# ---------------------------------------------------------------------------
# Match parser (ported from the source's Parser)
# ---------------------------------------------------------------------------
class Parser:
    """Parse Tennis & Padel Vlaanderen poule/tabel pages into match rows.

    Each knockout draw is a series of ``<table class="game-table">``. A match is
    one game-table whose ``<td class="match-winner">`` cell holds the winner's
    name + the score (already from the winner's perspective); the competitor
    rows list players via ``<a userId=...>`` and ``<td class="winner">`` marks
    the side that advanced.
    """

    BALL_TYPE = "Yellow"
    COUNTRY = "Belgium"
    COUNTRY_CODE = "BEL"
    ID_TYPE = "Belgium"
    IMPORT_SOURCE = "Belgium"
    SANCTION_BODY = "Tennis Vlaanderen"
    EVENT_TYPE = "Tournament"

    _RE_SET = re.compile(r"\d{1,2}\s*/\s*\d{1,2}(?:\s*\(\d+\))?")
    _RE_SET_PAIR = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})")
    _RE_WALKOVER = re.compile(r"walk\s*-?\s*over|w\.?o\.?", re.IGNORECASE)
    _RE_RETIRED = re.compile(r"\b(opgave|abandon|retir|ret\.?)\b", re.IGNORECASE)
    # Ranking/points suffix trailing a player name. The site exposes several
    # shapes — "- 9.1 (25 ptn)", "- 35, ptn", "- 100 ptn nr., 207" — that all
    # begin with " - ", so strip from the first " - " to the end (mirrors the
    # source's correct_name()). A parens-only regex left the rest intact and
    # mangled the name (e.g. "Goranov Alexandar - 35,, ptn").
    _RE_PLAYER_SUFFIX = re.compile(r"\s*-\s.*$")
    _RE_SEED = re.compile(r"^\s*\(\s*\d+\s*\)\s*")
    _RE_USERID = re.compile(r"userId=([^&]+)")
    _RE_PERIOD = re.compile(
        r"(\d{1,2}/\d{1,2}/\d{4})\s*(?:-|t/?m|tot)\s*(\d{1,2}/\d{1,2}/\d{4})",
        re.IGNORECASE,
    )
    _RE_SINGLE_DATE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")

    # Detail pages (dates/city) are fetched once per tournament and shared.
    _detail_cache = {}
    _detail_lock = threading.Lock()

    def __init__(self, selector, client, solver, log):
        self.client = client
        self.solver = solver
        self.log = log
        self._profile_id_cache = {}
        self._extract_tournament(selector)
        self._extract_draw(selector)

    # -- formatting helpers --------------------------------------------
    @classmethod
    def _format_date(cls, d):
        """DD/MM/YYYY (site) -> M/D/YYYY (spec)."""
        d = _clean(d)
        if not d:
            return ""
        try:
            dt = datetime.strptime(d, "%d/%m/%Y")
        except ValueError:
            return d
        return f"{dt.month}/{dt.day}/{dt.year}"

    @classmethod
    def _clean_player(cls, raw):
        """'( 1 ) Vermeerbergen Ayan - 9.1 (25 ptn)' -> 'Vermeerbergen Ayan'."""
        name = _clean(raw)
        name = cls._RE_SEED.sub("", name)
        name = cls._RE_PLAYER_SUFFIX.sub("", name)
        return name.strip()

    @classmethod
    def _last_first(cls, full_name):
        """'Lastname Firstname' -> 'Lastname, Firstname' (final token = first)."""
        name = cls._clean_player(full_name)
        if not name:
            return ""
        parts = name.split()
        if len(parts) == 1:
            return parts[0]
        return f"{' '.join(parts[:-1])}, {parts[-1]}"

    @classmethod
    def _userid_from_href(cls, href):
        if not href:
            return ""
        m = cls._RE_USERID.search(href)
        return m.group(1) if m else ""

    # -- tournament-level extraction -----------------------------------
    def _extract_tournament(self, selector):
        self.tournament_name = _clean(
            selector.xpath('.//h1[@class="page-title"]/text()').get() or ""
        )

        tornooi_id = ""
        href = selector.xpath('.//a[contains(@href,"tornooiId=")]/@href').get()
        if href:
            mid = re.search(r"tornooiId=(\d+)", href)
            tornooi_id = mid.group(1) if mid else ""
        self.tournament_url = (
            f"{BASE}/tornooi-detail?tornooiId={tornooi_id}" if tornooi_id else ""
        )

        self.tournament_start_date = ""
        self.tournament_end_date = ""
        self.tournament_city = ""

        details_text = self._join_details(selector) or _join_text(selector)
        m = self._RE_PERIOD.search(details_text)
        if m:
            self.tournament_start_date = self._format_date(m.group(1))
            self.tournament_end_date = self._format_date(m.group(2))

        if self.tournament_url and (
            not self.tournament_start_date or not self.tournament_city
        ):
            self._extract_tournament_detail(tornooi_id)

        self.tournament_state = ""
        self.tournament_country = self.COUNTRY
        self.tournament_country_code = self.COUNTRY_CODE
        self.tournament_host = ""
        self.tournament_location_type = ""
        self.tournament_surface = ""
        self.tournament_event_category = ""
        self.tournament_event_grade = ""
        self.tournament_event_type = self.EVENT_TYPE
        self.tournament_import_source = self.IMPORT_SOURCE
        self.tournament_sanction_body = self.SANCTION_BODY

    @staticmethod
    def _join_details(selector):
        node = selector.xpath('.//div[contains(@class,"tournament-details")]')
        return _join_text(node) if node else ""

    def _extract_tournament_detail(self, tornooi_id):
        """Fetch the tornooi-detail page for the period (dates) and city."""
        if not self.tournament_url:
            return
        with self._detail_lock:
            cached = self._detail_cache.get(tornooi_id)
        if cached is not None:
            start, end, city = cached
            self.tournament_start_date = self.tournament_start_date or start
            self.tournament_end_date = self.tournament_end_date or end
            self.tournament_city = self.tournament_city or city
            return

        start = end = city = ""
        html = _fetch_html(self.client, self.tournament_url, self.solver, self.log)
        if html:
            page = Selector(text=html)
            detail_text = _join_text(page.xpath("//body"))
            m = self._RE_PERIOD.search(detail_text)
            if m:
                start = self._format_date(m.group(1))
                end = self._format_date(m.group(2))
            else:
                m1 = self._RE_SINGLE_DATE.search(detail_text)
                if m1:
                    start = end = self._format_date(m1.group(1))
            for li in page.xpath('//li[span[contains(@class,"list-label")]]'):
                label = _clean(
                    li.xpath('./span[contains(@class,"list-label")]/text()').get() or ""
                ).lower()
                if any(k in label for k in ("plaats", "gemeente", "locatie", "stad")):
                    city = _clean(
                        li.xpath(
                            './span[contains(@class,"list-value")]//text()'
                        ).get()
                        or ""
                    )
                    if city:
                        break

        with self._detail_lock:
            self._detail_cache[tornooi_id] = (start, end, city)
        self.tournament_start_date = self.tournament_start_date or start
        self.tournament_end_date = self.tournament_end_date or end
        self.tournament_city = self.tournament_city or city

    # -- draw (Reeks) extraction ---------------------------------------
    def _extract_draw(self, selector):
        reeks = ""
        for li in selector.xpath('.//li[span[@class="list-label"]]'):
            label = _clean(li.xpath('./span[@class="list-label"]/text()').get() or "")
            if label.lower().startswith("reeks"):
                reeks = _clean(li.xpath('./span[@class="list-value"]/text()').get() or "")
                break
        self.draw_name = reeks
        low = reeks.lower()

        if "dubbel" in low:
            self.draw_team_type = "Doubles"
        elif "enkel" in low:
            self.draw_team_type = "Singles"
        else:
            self.draw_team_type = ""

        if re.search(r"\bj/?m\b|\bheren\b|\bmannen\b|\bjongens\b", low):
            self.draw_gender = "Male"
        elif re.search(r"\bj/?v\b|\bdames\b|\bvrouwen\b|\bmeisjes\b", low):
            self.draw_gender = "Female"
        else:
            self.draw_gender = ""

        self.draw_bracket_value = ""
        self.draw_bracket_type = ""
        self.draw_type = ""

    # -- per-match helpers ---------------------------------------------
    def _player_from_link(self, link, is_winner_row):
        raw_name = _join_text(link)
        if not raw_name:
            return None
        player_name = self._last_first(raw_name)
        href = link.xpath("./@href").get()
        user_id = self._userid_from_href(href)
        player_link = (
            f"{BASE}/nl/dashboard/resultaten?userId={user_id}" if user_id else ""
        )
        profile_id, player_name, player_gender = self._fetch_profile_id(
            user_id, player_link, player_name
        )
        third_party_id = profile_id or user_id
        return (player_name, third_party_id, is_winner_row, player_link, player_gender)

    def _players_from_td(self, td):
        is_winner_row = "winner" in (td.attrib.get("class") or "").lower()
        out = []
        for link in td.xpath('.//a[contains(@href,"userId")]'):
            p = self._player_from_link(link, is_winner_row)
            if p:
                out.append(p)
        return out

    def _fetch_profile_id(self, user_id, player_link, player_name):
        """Return ``(third_party_id, player_name, player_gender)`` from the
        profile page. Cached per ``userId`` for this Parser's lifetime.

        Deterministic: no AI name/gender lookup and no player DB cache — gender
        is left blank (the caller falls back to the draw gender) and a missing
        site id falls back to ``sha256_id(profile_name)``.
        """
        if not player_link:
            return "", player_name, ""
        if user_id in self._profile_id_cache:
            return self._profile_id_cache[user_id]

        profile_id = ""
        player_gender = ""
        html = _fetch_html(self.client, player_link, self.solver, self.log)
        if html:
            page = Selector(text=html)
            profile_id = _join_text(
                page.xpath(
                    '//div[contains(@class, "section--speler")]'
                    '//div[contains(@class, "section--speler__content")]'
                    '//div[@class="section--speler__info"]/ul[1]/li[1]'
                )
            )
            if not profile_id:
                profile_name = _join_text(
                    page.xpath(
                        '//div[contains(@class, "section--speler")]'
                        '//div[contains(@class, "section--speler__content")]'
                        '//div[@class="section--speler__name"]'
                    )
                )
                if profile_name:
                    profile_id = sha256_id(profile_name)

        result = (profile_id, player_name, player_gender)
        self._profile_id_cache[user_id] = result
        return result

    def _match_players(self, winner_cell):
        players = []
        seen = set()
        own_row = winner_cell.xpath("./parent::tr")
        next_row = winner_cell.xpath("./parent::tr/following-sibling::tr[1]")
        for row in (own_row, next_row):
            for td in row.xpath(
                './td[not(contains(@class,"match-winner"))]'
                '[.//a[contains(@href,"userId")]]'
            ):
                for p in self._players_from_td(td):
                    key = p[1] or p[0]
                    if key in seen:
                        continue
                    seen.add(key)
                    players.append(p)
        return players

    @staticmethod
    def _winner_cell_data(winner_cell):
        winner_raw = winner_cell.xpath(".//strong//text()").get() or ""
        score_raw = " ".join(
            winner_cell.xpath('.//span[contains(@class,"score")]//text()').getall()
        )
        return winner_raw, score_raw

    @classmethod
    def _build_score(cls, raw_score, outcome):
        if outcome == "Walkover":
            return "W.O.;"
        sets = cls._RE_SET.findall(raw_score or "")
        pairs = []
        for s in sets:
            m = cls._RE_SET_PAIR.search(s)
            if not m:
                continue
            pairs.append(f"{m.group(1)}-{m.group(2)}")
        score = ", ".join(pairs)
        if outcome == "retired" and score:
            score += " ret."
        return f"{score};" if score else ";"

    @classmethod
    def _outcome_from_score(cls, raw_score):
        if cls._RE_WALKOVER.search(raw_score or ""):
            return "Walkover"
        if cls._RE_RETIRED.search(raw_score or ""):
            return "retired"
        return "Completed"

    # -- parse one match -----------------------------------------------
    def parse_match(self, winner_cell):
        winner_raw, raw_score = self._winner_cell_data(winner_cell)
        winner_name = self._last_first(winner_raw)
        if not winner_name:
            return {}

        players = self._match_players(winner_cell)
        if not players:
            return {}

        outcome = self._outcome_from_score(raw_score)
        score = self._build_score(raw_score, outcome)

        def norm(n):
            return _clean(n).lower()

        is_doubles = self.draw_team_type == "Doubles"
        if is_doubles:
            winners = [p for p in players if p[2]]
            losers = [p for p in players if not p[2]]
            if not winners:
                winners = [p for p in players if norm(p[0]) == norm(winner_name)]
                losers = [p for p in players if norm(p[0]) != norm(winner_name)]
        else:
            winners = [p for p in players if norm(p[0]) == norm(winner_name)]
            losers = [p for p in players if norm(p[0]) != norm(winner_name)]
            if not winners:
                winners = [p for p in players if p[2]]
                losers = [p for p in players if not p[2]]

        if not losers:
            return {}

        def at(lst, i):
            return lst[i] if i < len(lst) else ("", "", False, "", "")

        w1_name, w1_id, _, w1_link, w1_gender = at(winners, 0)
        w2_name, w2_id, _, w2_link, w2_gender = (
            at(winners, 1) if is_doubles else ("", "", False, "", "")
        )
        l1_name, l1_id, _, l1_link, l1_gender = at(losers, 0)
        l2_name, l2_id, _, l2_link, l2_gender = (
            at(losers, 1) if is_doubles else ("", "", False, "", "")
        )

        g = self.draw_gender

        return {
            "match_id": "",  # not exposed in this markup
            "ball_type": self.BALL_TYPE,
            "draw_bracket_value": self.draw_bracket_value,
            "draw_name": self.draw_name,
            "draw_team_type": self.draw_team_type,
            "tournament_name": self.tournament_name,
            "date": self.tournament_start_date,
            "round": "",  # round label lives in the wizard nav, not the table
            "score": score,
            "winner_1_name": w1_name,
            "winner_1_gender": w1_gender or g,
            "winner_1_third_party_id": w1_id,
            "winner_1_link": w1_link,
            "winner_1_city": "",
            "winner_1_country": self.COUNTRY,
            "winner_1_state": "",
            "winner_2_name": w2_name,
            "winner_2_gender": (w2_gender or g) if w2_name else "",
            "winner_2_third_party_id": w2_id,
            "winner_2_link": w2_link,
            "winner_2_city": "",
            "winner_2_state": "",
            "loser_1_name": l1_name,
            "loser_1_gender": l1_gender or g,
            "loser_1_third_party_id": l1_id,
            "loser_1_link": l1_link,
            "loser_1_city": "",
            "loser_1_state": "",
            "loser_1_country": self.COUNTRY,
            "loser_2_name": l2_name,
            "loser_2_gender": (l2_gender or g) if l2_name else "",
            "loser_2_third_party_id": l2_id,
            "loser_2_link": l2_link,
            "loser_2_city": "",
            "loser_2_state": "",
            "outcome": outcome,
            "id_type": self.ID_TYPE,
            "draw_gender": self.draw_gender,
            "draw_bracket_type": self.draw_bracket_type,
            "draw_type": self.draw_type,
            "tournament_city": self.tournament_city,
            "tournament_state": self.tournament_state,
            "tournament_country_code": self.tournament_country_code,
            "tournament_host": self.tournament_host,
            "tournament_location_type": self.tournament_location_type,
            "tournament_surface": self.tournament_surface,
            "tournament_event_category": self.tournament_event_category,
            "tournament_event_grade": self.tournament_event_grade,
            "tournament_import_source": self.tournament_import_source,
            "tournament_sanction_body": self.tournament_sanction_body,
            "winner_2_country": self.COUNTRY if w2_name else "",
            "winner_2_college": "",
            "loser_2_country": self.COUNTRY if l2_name else "",
            "loser_2_college": "",
            "tournament_event_type": self.tournament_event_type,
            "winner_1_college": "",
            "loser_1_college": "",
            "tournament_url": self.tournament_url,
            "winner_1_dob": "",
            "winner_2_dob": "",
            "loser_1_dob": "",
            "loser_2_dob": "",
            "tournament_country": self.tournament_country,
            "tournament_start_date": self.tournament_start_date,
            "tournament_end_date": self.tournament_end_date,
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def _dedup_key(row):
    return row.get("match_id") or (
        row.get("tournament_name", ""),
        row.get("draw_name", ""),
        row.get("round", ""),
        row.get("date", ""),
        row.get("winner_1_name", ""),
        row.get("loser_1_name", ""),
        row.get("winner_2_name", ""),
        row.get("loser_2_name", ""),
        row.get("score", ""),
    )


def run(run_obj, log):
    """Execute the Belgium scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    params = run_obj.params or {}
    tournament_url = (params.get("tournament_url") or "").strip()

    if tournament_url:
        log("INFO", "\U0001f3be Belgium (Tennis Vlaanderen) \u2014 single tournament URL")
        start_d = end_d = None
    else:
        start_d = run_obj.date_from or timezone.localdate()
        end_d = run_obj.date_to or timezone.localdate()
        log("INFO", f"\U0001f3be Belgium (Tennis Vlaanderen) \u2014 {start_d} \u2192 {end_d}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)

    # Best-effort load the captcha solver. An admin can upload the model via the
    # Settings tab (stored in the DB) — drop it onto disk first so the loader finds
    # it. Without TensorFlow + the model, challenged pages can't be cleared and the
    # run fails honestly.
    solver = None
    try:
        materialize_uploaded_model(scraper, log)
        solver = CaptchaSolver(log)
        log("INFO", "\U0001f9e0 captcha solver ready")
    except CaptchaSolverUnavailable as exc:
        tele.record_error(
            f"Captcha solver unavailable (TensorFlow + captcha_model.keras "
            f"required): {exc}"
        )
        log(
            "WARN",
            "\u26a0\ufe0f captcha solver unavailable \u2014 Zenedge-challenged "
            f"pages cannot be cleared: {exc}",
        )

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering tournaments \u2500\u2500\u2500\u2500")
    with ScraperClient(
        log=log, tele=tele, proxies=proxies, allowed_hosts=ALLOWED_HOSTS
    ) as discovery:
        if tournament_url:
            tournaments = [{"tournament_name": "", "tournament_url": tournament_url}]
        else:
            tournaments = _discover_tournaments(discovery, solver, start_d, end_d, log)

    total = len(tournaments)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} tournament(s) to scrape")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def process(tournament):
        client = ScraperClient(
            log=log, tele=tele, proxies=proxies, allowed_hosts=ALLOWED_HOSTS
        )
        try:
            rows = _scrape_tournament(client, solver, tournament, log)
            for row in rows:
                key = _dedup_key(row)
                with lock:
                    if key in seen:
                        continue
                    seen.add(key)
                    writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
                    counter["rows"] += 1
                log(
                    "INFO",
                    f"   \U0001f3c6 {row.get('draw_team_type', '')}: "
                    f"{row.get('winner_1_name') or '?'} def. "
                    f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                    f"@ {row.get('tournament_name') or 'Tennis Vlaanderen'}",
                )
        except Exception as exc:  # noqa: BLE001 - one bad tournament can't kill the run
            tele.record_error(
                redact_secrets(
                    f"Tournament {tournament.get('tournament_url', '')} failed: {exc}"
                ),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(f"\u26a0\ufe0f tournament failed: {exc.__class__.__name__}: {exc}"),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(
                progress_done=F("progress_done") + 1
            )
            client.close()

    if tournaments:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping tournaments \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, tournaments))

    row_count = counter["rows"]
    log("INFO", "\u2500\u2500\u2500\u2500 summary \u2500\u2500\u2500\u2500")
    log("INFO", f"\U0001f4be Writing {row_count} row(s) to CSV")
    log(
        "INFO",
        f"\U0001f4ca Telemetry: {tele.request_count} request(s), {tele.error_count} error(s)",
    )
    status = Run.Status.SUCCESS if row_count else Run.Status.FAILED
    icon = "\U0001f3c1" if status == Run.Status.SUCCESS else "\U0001f6d1"
    log("INFO", f"{icon} Run finished \u2014 status={status}, rows={row_count}")
    items_csv = buf.getvalue() if row_count else ""
    return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
