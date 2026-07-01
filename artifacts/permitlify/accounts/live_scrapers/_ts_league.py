"""Shared engine for tournamentsoftware.com **league** (team) competitions.

Several national federations publish their team leagues on a
tournamentsoftware.com host (``hts.tournamentsoftware.com``,
``dtf.tournamentsoftware.com``, the Finnish ``www.tennisassa.fi``, …). They share
one markup and one set of endpoints, differing only by host and a few constant
fields (country, country code, sanction body). This module generalises the
production ``*_league`` spider family onto MatchMiner's shared HTTP client
(:mod:`accounts.live_scrapers._http`) + telemetry, parameterised by a
:class:`TSLeagueConfig` so each federation is a thin wrapper (mirroring how
:mod:`accounts.live_scrapers._ts_tournament` backs the individual-tournament
wrappers).

The real-time start form collects **either** a league URL **or** a date window
(``input_kind = date_range_or_url``):

* **league URL** — scrape that single league directly;
* **date range** — page the league search (``find/league/DoSearch``) between the
  two dates and scrape every league found.

For each league the crawl walks: league page → ``var DrawList`` groups →
``draw/<id>`` group pages → team-match pages → individual ``div.match`` blocks,
then follows each player's profile for their third-party id and date of birth.
Because each match is reachable from a single team-match page, rows are
de-duplicated by a content key.

Unlike the production spider this port is **deterministic and AI-free**: the
original used Claude to pretty-format new player names and infer gender. That is
dropped — names are emitted in deterministic ``"Lastname, Firstname"`` order
(cleaned of seedings, then reordered via :func:`accounts.live_scrapers._names.last_first`
to match the Claude formatter the source applied) and gender is left
empty (the markup carries no reliable gender signal), exactly mirroring the
Brazil port's choice. ``run(config, run_obj, log)`` returns ``(items_csv,
requests_csv, errors_csv, row_count, status)``.
"""

import csv
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlencode, urljoin

from django.db.models import F
from django.utils import timezone
from parsel import Selector

from accounts.models import Run

from ._gender import draw_gender_code, is_mixed_draw
from ._claude_gender import resolve_gender, resolve_claude_keys
from ._http import ScraperClient, build_proxies
from ._names import last_first
from .telemetry import Telemetry, redact_secrets, sanitize_cell

EVENT_TYPE = "League"
BALL_TYPE = "Yellow"


@dataclass(frozen=True)
class TSLeagueConfig:
    """Per-federation constants for a tournamentsoftware league site.

    ``base`` is the host root (no trailing slash), e.g.
    ``https://hts.tournamentsoftware.com``. ``country`` is the full country name
    (used for ``id_type`` / ``tournament_import_source`` / ``tournament_country``
    and the per-player country fields); ``country_code`` is the short code (used
    for ``tournament_country_code``). ``sanction_body`` is the governing body.
    ``lcid`` selects the cookiewall locale (2057 = English by default).
    """

    label: str
    base: str
    country: str
    country_code: str
    sanction_body: str
    lcid: str = "2057"
    # --- Claude name->gender mode ----------------------------------------
    # League / competition names often carry no gender word ("Prva liga"), so
    # the draw-name gender heuristic (:func:`_gender.draw_gender_code`) yields
    # nothing. Set ``claude_gender`` to infer each player's gender from their
    # name via Claude instead (cached; requires a Claude key, else gender
    # degrades to empty).
    claude_gender: bool = False


# Items CSV columns — the same ITF item schema used across MatchMiner scrapers
# (model field order, minus the internal spider_id / job_id). Title-cased header
# to match the framework's downloadable files (e.g. "Tournament Url").
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

_RE_DRAWLIST = re.compile(r"var\s+DrawList\s*=\s*(\[.*?\])\s*;", re.DOTALL)
_RE_PARENS = re.compile(r"[()]")
_RE_SEED = re.compile(r"\s*\[[^\]]+\]\s*$")
_RE_YEAR = re.compile(r"(20\d{2})")
_RANGE_SEP = re.compile(r"\s+(?:-|\u2013|\u2014|to)\s+")

# Date formats the tournament range / labels can appear in. lcid 2057 renders
# textual months ("26 April"), often without a year, so we also try year-less
# formats and backfill the year from the league name / run window.
_DATE_FORMATS_YEAR = (
    "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d",
    "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y",
    "%B %d, %Y", "%b %d, %Y",
)
_DATE_FORMATS_NO_YEAR = ("%d %B", "%d %b", "%B %d", "%b %d")


def _field(sel, xpath):
    """First xpath match, stripped, or ``""`` (mirrors fctcore.parse_field)."""
    value = sel.xpath(xpath).get()
    return value.strip() if value else ""


def _clean_name(name):
    """Drop a trailing ``[seed]`` marker and surrounding whitespace."""
    return _RE_SEED.sub("", (name or "")).strip()


def _to_mdy(text, in_formats):
    """Parse ``text`` with the first matching ``in_formats`` → ``MM/DD/YYYY``."""
    text = (text or "").strip()
    if not text:
        return ""
    for fmt in in_formats:
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return ""


def _parse_one_date(token, default_year):
    """Parse a single date token, backfilling ``default_year`` when absent."""
    token = (token or "").strip().strip(",")
    if not token:
        return None
    for fmt in _DATE_FORMATS_YEAR:
        try:
            return datetime.strptime(token, fmt)
        except ValueError:
            continue
    if default_year:
        for fmt in _DATE_FORMATS_NO_YEAR:
            try:
                return datetime.strptime(token, fmt).replace(year=default_year)
            except ValueError:
                continue
    return None


def _parse_range_dates(text, default_year=None):
    """Split a ``"<start> - <end>"`` tag into ``(start, end)`` as MM/DD/YYYY.

    Handles both numeric (``26/04/2025``) and textual (``26 April``) months. The
    tournamentsoftware locale (lcid 2057) often omits the year, so it is
    backfilled from ``default_year`` (derived from the league name / run window).
    """
    text = (text or "").strip()
    if not text:
        return "", ""
    parts = _RANGE_SEP.split(text, maxsplit=1)
    start = _parse_one_date(parts[0], default_year)
    end = _parse_one_date(parts[1] if len(parts) > 1 else parts[0], default_year)
    start_s = start.strftime("%m/%d/%Y") if start else ""
    end_s = end.strftime("%m/%d/%Y") if end else start_s
    return start_s, end_s


# ======================================================================
# Session warm-up — accept the cookie wall + switch the UI to English so the
# subsequent pages render the expected labels/markup.
# ======================================================================
def _warmup(client, cfg):
    client.get(
        f"{cfg.base}/cookiewall?returnurl=%2Ftournament%2F&lcid={cfg.lcid}"
    )
    body = urlencode(
        [
            ("ReturnUrl", "/tournament/"),
            ("SettingsOpen", "false"),
            ("CookiePurposes", "2"),
            ("CookiePurposes", "4"),
            ("CookiePurposes", "16"),
        ]
    )
    client.post(
        f"{cfg.base}/cookiewall/Save",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


# ======================================================================
# Discovery
# ======================================================================
def _discover_one(client, cfg, tournament_url, log):
    """Resolve a single league URL to ``[{name, url}]`` (or ``[]``)."""
    sel = client.get_selector(tournament_url)
    if sel is None:
        log("WARN", "\u26a0\ufe0f Could not load the supplied league URL")
        return []
    name = _field(
        sel,
        '//header[contains(@class, "page-head")]//div[@class="media__content"]'
        '//h2[contains(@class, "media__title")]/a/span[@class="nav-link__value"]/text()',
    )
    href = _field(
        sel,
        '//header[contains(@class, "page-head")]//div[@class="media__content"]'
        '//h2[contains(@class, "media__title")]/a/@href',
    )
    url = urljoin(cfg.base + "/", href) if href else tournament_url
    if not name:
        name = cfg.label
    return [{"tournament_name": name, "tournament_url": url}]


def _discover_range(client, cfg, start_date, end_date, log):
    """Page the league search between two ``YYYY-MM-DD`` dates."""
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }
    search_url = f"{cfg.base}/find/league/DoSearch"
    tournaments = []
    seen = set()
    page = 0
    while True:
        page += 1
        body = urlencode(
            {
                "LoadMoreResults": "LoadMoreResults",
                "Page": str(page),
                "LeagueFilter.Q": "",
                "LeagueFilter.StartDate": start_date,
                "LeagueFilter.EndDate": end_date,
                "LeagueFilter.CountryCode": "",
                "LeagueFilter.StatusFilterID": "false",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        resp = client.post(search_url, data=body, headers=headers)
        if resp is None or not (200 <= resp.status_code < 300):
            break
        sel = Selector(text=resp.text)
        found = False
        for d1 in sel.xpath('//li[@class="list__item"]//div[@class="media__content"]'):
            name = _field(d1, './/h4[@class="media__title"]/a/@title')
            href = _field(d1, './/h4[@class="media__title"]/a/@href')
            if not (name and href):
                continue
            url = urljoin(cfg.base + "/", href)
            if url in seen:
                continue
            found = True
            seen.add(url)
            tournaments.append({"tournament_name": name, "tournament_url": url})
        log("INFO", f"   \U0001f50e search page {page}: {len(tournaments)} league(s) so far")
        if not found:
            break
    return tournaments


# ======================================================================
# Per-tournament crawl
# ======================================================================
def _parse_match(sel, cfg):
    """Parse one ``div.match__body`` block into a winners/losers/score dict."""
    outcome = "Completed"
    if sel.xpath('.//*[contains(normalize-space(.),"Retired")]'):
        outcome = "Retired"

    rows = sel.xpath(
        './/div[contains(@class,"match__row-wrapper")]'
        '/div[contains(@class,"match__row")]'
    )
    row_players = []
    winner_row_index = None
    for idx, row in enumerate(rows):
        players = []
        for a in row.xpath('.//a[contains(@class,"nav-link")]'):
            name = a.xpath('.//span[@class="nav-link__value"]/text()').get()
            href = a.xpath("./@href").get()
            if name and href:
                players.append(
                    {
                        "name": _clean_name(name),
                        "profile_url": urljoin(cfg.base + "/", href.strip()),
                    }
                )
        row_players.append(players)
        if "has-won" in (row.attrib.get("class", "") or ""):
            winner_row_index = idx

    if winner_row_index is None or len(row_players) < 2:
        return None
    loser_row_index = 1 - winner_row_index
    winners = row_players[winner_row_index]
    losers = row_players[loser_row_index]

    scores = []
    for ul in sel.xpath('//div[contains(@class,"match__result")]//ul[@class="points"]'):
        cells = [c.xpath("normalize-space(text())").get() for c in ul.xpath("./li")]
        if len(cells) != 2:
            continue
        scores.append(f"{cells[winner_row_index]}-{cells[loser_row_index]}")

    return {
        "draw_team_type": "Doubles" if len(winners) == 2 else "Singles",
        "outcome": outcome,
        "score": ", ".join(scores) + ";" if scores else "",
        "winner_1": winners[0] if len(winners) > 0 else {},
        "winner_2": winners[1] if len(winners) > 1 else {},
        "loser_1": losers[0] if len(losers) > 0 else {},
        "loser_2": losers[1] if len(losers) > 1 else {},
    }


def _parse_dob(sel):
    """Read DOB/YOB from a player profile page → ``MM/DD/YYYY``."""
    for key in ("DOB:", "YOB:"):
        text = _field(
            sel,
            '//div[contains(@class, "page-head")]//div[@class="media__content"]'
            '//div[contains(@class, "media__content-subinfo")]'
            '//small[contains(@class, "media__subheading")]/span[@class="nav-link"]'
            '/span[@class="nav-link__value" and contains(text(), "' + key + '")]/text()',
        )
        if not text:
            continue
        value = text.replace(key, "").strip()
        if key == "DOB:":
            dob = _to_mdy(value, ("%d/%m/%Y",))
            if dob:
                return dob
        elif key == "YOB:" and value:
            return f"1/1/{value}"
    return ""


def _parse_player(client, cfg, name, url):
    """Resolve a player's ``(name, third_party_id, dob, gender)``.

    Gender is left empty here and filled in by :func:`_build_row` — from the
    league / competition name by default, or (when ``cfg.claude_gender`` is set)
    inferred from the player's name via Claude. The name is cleaned of seedings
    then reordered to ``"Lastname, Firstname"`` via :func:`._names.last_first`.
    """
    name = last_first(name)
    if not (name and url):
        return name, "", "", ""
    sel = client.get_selector(url)
    if sel is None:
        return name, "", "", ""

    third_party_id = _field(
        sel,
        '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
        '//h4[contains(@class, "media__title")]/span[@class="media__title-aside"]/text()',
    )
    third_party_id = _RE_PARENS.sub("", third_party_id).strip()

    dob = ""
    profile_href = _field(
        sel,
        '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
        '//h4[contains(@class, "media__title")]/a/@href',
    )
    if profile_href:
        profile_sel = client.get_selector(urljoin(cfg.base + "/", profile_href))
        if profile_sel is not None:
            dob = _parse_dob(profile_sel)
    return name, third_party_id, dob, ""


def _build_row(client, cfg, ctx, match_data):
    """Assemble one full items row from a parsed match + player lookups."""
    w1 = match_data.get("winner_1", {})
    w2 = match_data.get("winner_2", {})
    l1 = match_data.get("loser_1", {})
    l2 = match_data.get("loser_2", {})

    w1_name, w1_id, w1_dob, w1_g = _parse_player(client, cfg, w1.get("name", ""), w1.get("profile_url", ""))
    w2_name, w2_id, w2_dob, w2_g = _parse_player(client, cfg, w2.get("name", ""), w2.get("profile_url", ""))
    l1_name, l1_id, l1_dob, l1_g = _parse_player(client, cfg, l1.get("name", ""), l1.get("profile_url", ""))
    l2_name, l2_id, l2_dob, l2_g = _parse_player(client, cfg, l2.get("name", ""), l2.get("profile_url", ""))

    draw_name = ctx.get("draw_name", "")
    gcode = draw_gender_code(draw_name)
    claude_keys = ctx.get("claude_keys")
    if cfg.claude_gender and claude_keys:
        # League / competition names often carry no gender word, so infer each
        # player's gender from their name via Claude (cached per distinct name).
        w1_g = resolve_gender(client, claude_keys, w1_name) if w1_name else ""
        w2_g = resolve_gender(client, claude_keys, w2_name) if w2_name else ""
        l1_g = resolve_gender(client, claude_keys, l1_name) if l1_name else ""
        l2_g = resolve_gender(client, claude_keys, l2_name) if l2_name else ""
        # Draw-level gender: an explicit gender word wins; a genuinely mixed draw
        # stays blank; otherwise fall back to the winner's inferred gender.
        if gcode:
            draw_gender = "Male" if gcode == "M" else "Female"
        elif is_mixed_draw(draw_name):
            draw_gender = ""
        else:
            draw_gender = "Male" if w1_g == "M" else ("Female" if w1_g == "F" else "")
    else:
        # Default: gender is carried by the league / competition name (e.g.
        # "...za seniorke" = women, "...seniorska liga" = men); all inherit it.
        w1_g = gcode if w1_name else ""
        w2_g = gcode if w2_name else ""
        l1_g = gcode if l1_name else ""
        l2_g = gcode if l2_name else ""
        draw_gender = "Male" if gcode == "M" else ("Female" if gcode == "F" else "")

    return {
        "match_id": "",
        "ball_type": BALL_TYPE,
        "id_type": cfg.country,
        "draw_bracket_value": "",
        "draw_name": ctx.get("draw_name", ""),
        "draw_team_type": match_data.get("draw_team_type", ""),
        "tournament_name": ctx.get("tournament_name", ""),
        "date": ctx.get("match_date", ""),
        "round": ctx.get("match_round", ""),
        "score": match_data.get("score", ""),
        "winner_1_name": w1_name,
        "winner_1_gender": w1_g,
        "winner_1_dob": w1_dob,
        "winner_1_third_party_id": w1_id,
        "winner_1_city": "",
        "winner_1_state": "",
        "winner_1_country": cfg.country if w1_name else "",
        "winner_2_name": w2_name,
        "winner_2_gender": w2_g,
        "winner_2_dob": w2_dob,
        "winner_2_third_party_id": w2_id,
        "winner_2_city": "",
        "winner_2_state": "",
        "winner_2_country": cfg.country if w2_name else "",
        "loser_1_name": l1_name,
        "loser_1_gender": l1_g,
        "loser_1_dob": l1_dob,
        "loser_1_third_party_id": l1_id,
        "loser_1_city": "",
        "loser_1_state": "",
        "loser_1_country": cfg.country if l1_name else "",
        "loser_2_name": l2_name,
        "loser_2_gender": l2_g,
        "loser_2_dob": l2_dob,
        "loser_2_third_party_id": l2_id,
        "loser_2_city": "",
        "loser_2_state": "",
        "loser_2_country": cfg.country if l2_name else "",
        "outcome": match_data.get("outcome", ""),
        "draw_gender": draw_gender,
        "draw_bracket_type": "",
        "draw_type": "",
        "tournament_city": "",
        "tournament_state": "",
        "tournament_country_code": cfg.country_code,
        "tournament_host": "",
        "tournament_location_type": "",
        "tournament_surface": "",
        "tournament_event_category": "",
        "tournament_event_grade": "",
        "tournament_import_source": cfg.country,
        "tournament_sanction_body": cfg.sanction_body,
        "winner_2_college": "",
        "loser_2_college": "",
        "tournament_event_type": EVENT_TYPE,
        "winner_1_college": "",
        "loser_1_college": "",
        "tournament_url": ctx.get("tournament_url", ""),
        "tournament_country": cfg.country,
        "tournament_start_date": ctx.get("tournament_start_date", ""),
        "tournament_end_date": ctx.get("tournament_end_date", ""),
    }


def _parse_matches(client, cfg, ctx, match_url):
    """Fetch one team-match page and return its parsed rows."""
    sel = client.get_selector(match_url)
    if sel is None:
        return []

    match_round = _field(
        sel,
        '(//div[@id="js-league-team-match-index"]'
        '//div[contains(@class, "team-match-header")]//div[@class="module-container"]'
        '//div[contains(@class, "text--center")]//time)[1]/parent::node()/text()[1]',
    ).replace("\u2022", "").strip()

    match_date = ""
    raw_date = _field(
        sel,
        '(//div[@id="js-league-team-match-index"]'
        '//div[contains(@class, "team-match-header")]//div[@class="module-container"]'
        '//div[contains(@class, "text--center")]//time)[1]/@datetime',
    )
    if raw_date:
        try:
            match_date = datetime.strptime(raw_date, "%Y-%m-%d %H:%M").strftime("%m/%d/%Y")
        except ValueError:
            match_date = ""

    draw_name = _field(
        sel,
        '//div[@id="js-league-team-match-index"]'
        '//div[contains(@class, "team-match-header")]//div[@class="module-container"]'
        '//div[contains(@class, "text--center")]/a[@class="nav-link"]'
        '/span[@class="nav-link__value"]/text()',
    )

    rows = []
    match_ctx = dict(ctx)
    match_ctx.update(
        {"match_round": match_round, "match_date": match_date, "draw_name": draw_name}
    )
    for d1 in sel.xpath(
        '//div[@class="module-container"]/ul/li[@class="match-group__item"]'
        '/div[@class="match"]'
    ):
        body = d1.xpath('.//div[@class="match__body"]').get()
        if not body:
            continue
        match_data = _parse_match(Selector(text=body), cfg)
        if match_data:
            rows.append(_build_row(client, cfg, match_ctx, match_data))
    return rows


def _enumerate_group(client, cfg, ctx, group_url):
    """Return ``[(match_url, group_ctx)]`` for one draw/group (a light request)."""
    sel = client.get_selector(group_url)
    if sel is None:
        return []

    range_text = _field(
        sel,
        '//header[contains(@class, "page-head")]//div[@class="media__content"]'
        '//ul[contains(@class, "list")]/li[@class="list__item"][1]'
        '/span[contains(@class, "tag--mono")]/text()',
    )
    start_date, end_date = _parse_range_dates(range_text, ctx.get("default_year"))
    group_ctx = dict(ctx)
    group_ctx.update(
        {"tournament_start_date": start_date, "tournament_end_date": end_date}
    )

    items = []
    for divider in sel.xpath(
        '//div[@class="module-container"]/h4[@class="module-divider"]'
    ):
        for d2 in divider.xpath(
            './following-sibling::ul[@class="match-group"][1]'
            '/li[@class="match-group__item"]//a[@class="team-match__wrapper"]'
        ):
            href = d2.xpath("./@href").get()
            if not href:
                continue
            items.append((urljoin(cfg.base + "/", href.strip()), group_ctx))
    return items


def _enumerate_tournament(client, cfg, tournament, fallback_year=None):
    """Return ``[(match_url, group_ctx)]`` across all of a league's draws."""
    tournament_url = tournament.get("tournament_url", "")
    sel = client.get_selector(tournament_url)
    if sel is None:
        return []

    script = sel.xpath('//script[contains(., "var DrawList")]').get() or ""
    match = _RE_DRAWLIST.search(script)
    if not match:
        return []
    try:
        draw_list = json.loads(match.group(1))
    except (ValueError, TypeError):
        return []

    name = tournament.get("tournament_name", "")
    year_match = _RE_YEAR.search(name)
    default_year = int(year_match.group(1)) if year_match else fallback_year
    ctx = {
        "tournament_name": name,
        "tournament_url": tournament_url,
        "default_year": default_year,
    }
    items = []
    for draw_res in draw_list:
        group_id = draw_res.get("XTPID")
        if group_id:
            items.extend(
                _enumerate_group(client, cfg, ctx, f"{tournament_url}/draw/{group_id}")
            )
    return items


def _window(run_obj):
    """Resolve the ``(start, end)`` YYYY-MM-DD search window from the run."""
    today = timezone.localdate()
    start = run_obj.date_from or today
    end = run_obj.date_to or today
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def run(cfg, run_obj, log):
    """Execute one tournamentsoftware league scrape. Returns the standard 5-tuple.

    Work is parallelised the way the Stadion scraper handles ties: discovery is a
    single warm session, then every team-match across every league is fetched
    concurrently by a pool of ``worker_count`` warmed sessions (one per thread).
    Player-profile lookups within a team-match stay serial on that thread.
    """
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    params = run_obj.params or {}
    tournament_url = (params.get("tournament_url") or "").strip()

    if tournament_url:
        log("INFO", f"\U0001f3be {cfg.label} starting \u2014 single league URL")
    else:
        start_date, end_date = _window(run_obj)
        log(
            "INFO",
            f"\U0001f3be {cfg.label} starting \u2014 {start_date} \u2192 {end_date}",
        )
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)
    fallback_year = (run_obj.date_to or run_obj.date_from or timezone.localdate()).year
    claude_keys = resolve_claude_keys(scraper) if cfg.claude_gender else []
    if cfg.claude_gender:
        if claude_keys:
            log("INFO", "\U0001f9e0 Gender: Claude name inference enabled (cached)")
        else:
            log(
                "WARN",
                "\u26a0\ufe0f claude_gender set but no Claude key configured "
                "\u2014 falling back to draw-name gender only "
                "(per-player gender will be blank for genderless draws)",
            )

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering leagues \u2500\u2500\u2500\u2500")
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        _warmup(discovery, cfg)
        if tournament_url:
            tournaments = _discover_one(discovery, cfg, tournament_url, log)
        else:
            tournaments = _discover_range(discovery, cfg, start_date, end_date, log)
    log("INFO", f"\U0001f4cb {len(tournaments)} league(s) discovered")

    # Per-thread warmed clients, tracked so we can close them all at the end.
    local = threading.local()
    clients = []
    clients_lock = threading.Lock()

    def client_for():
        cli = getattr(local, "client", None)
        if cli is None:
            cli = ScraperClient(log=log, tele=tele, proxies=proxies)
            _warmup(cli, cfg)
            with clients_lock:
                clients.append(cli)
            local.client = cli
        return cli

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def enumerate_one(tournament):
        try:
            return _enumerate_tournament(client_for(), cfg, tournament, fallback_year)
        except Exception as exc:  # noqa: BLE001 - a bad league can't kill the run
            tele.record_error(
                redact_secrets(
                    f"Enumerate {tournament.get('tournament_url', '')} failed: {exc}"
                ),
                exc=exc,
            )
            return []

    def crawl_one(item):
        match_url, group_ctx = item
        if claude_keys:
            group_ctx = {**group_ctx, "claude_keys": claude_keys}
        try:
            rows = _parse_matches(client_for(), cfg, group_ctx, match_url)
            for row in rows:
                # Source-identified key: dedupes the exact same rubber if a
                # team-match page is ever enumerated twice, without collapsing
                # genuine rematches (same players/score in a different match,
                # round or date).
                key = (
                    match_url,
                    row.get("draw_name", ""),
                    row.get("round", ""),
                    row.get("date", ""),
                    row.get("winner_1_name", ""),
                    row.get("loser_1_name", ""),
                    row.get("winner_2_name", ""),
                    row.get("loser_2_name", ""),
                    row.get("score", ""),
                )
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
                    f"@ {row.get('tournament_name') or cfg.label}",
                )
        except Exception as exc:  # noqa: BLE001 - a bad match can't kill the run
            tele.record_error(
                redact_secrets(f"Team-match {match_url} failed: {exc}"), exc=exc
            )
            log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f team-match failed: {exc.__class__.__name__}: {exc}"
                ),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(
                progress_done=F("progress_done") + 1
            )

    try:
        if tournaments:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                # ---- phase 2 · enumerate every team-match (light) ----
                log(
                    "INFO",
                    "\u2500\u2500\u2500\u2500 phase 2 \u00b7 mapping team-matches \u2500\u2500\u2500\u2500",
                )
                nested = executor.map(enumerate_one, tournaments)
                work = [item for sub in nested for item in sub]
                Run.objects.filter(pk=run_obj.pk).update(
                    progress_total=len(work), progress_done=0
                )
                log("INFO", f"\U0001f5fa\ufe0f {len(work)} team-match(es) to scrape")

                # ---- phase 3 · scrape each team-match concurrently ----
                if work:
                    log(
                        "INFO",
                        "\u2500\u2500\u2500\u2500 phase 3 \u00b7 scraping matches \u2500\u2500\u2500\u2500",
                    )
                    list(executor.map(crawl_one, work))
    finally:
        for cli in clients:
            cli.close()

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
