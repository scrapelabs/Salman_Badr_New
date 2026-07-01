"""Estonia tennis (Tournamentsoftware / etl.tournamentsoftware.com) scraper.

Ports the production ``estonia_tournament`` spider onto MatchMiner's shared HTTP
client (:mod:`accounts.live_scrapers._http`) + telemetry. Despite running on the
Tournamentsoftware platform, this is a **bespoke multi-stage HTML pipeline** —
not a thin ``_ts_tournament`` wrapper — because the source drives the legacy
``/sport/*.aspx`` and modern ``/tournament/.../Players`` pages directly and joins
a separately-built player table onto each match.

Input is a **date range** (``date_from`` / ``date_to``) *or* a single
``tournament_url`` (on ``etl.tournamentsoftware.com``); a URL skips discovery and
scrapes that one tournament. The flow mirrors the source's three stages:

1. **Discovery.** With a date range, POST the platform tournament finder
   (``/find/tournament/DoSearch``) page by page and keep every tournament whose
   start/end dates the finder returns inside the window. With a single URL, parse
   that tournament's header to resolve its id + dates instead.
   (The original source's discovery endpoint is this Tournamentsoftware finder —
   there is no ``www.tennis.ee`` call in the source, so the faithful port uses
   the finder.)
2. **Players.** Per tournament, build a player table keyed by ``third_party_id``:
   prefer the modern ``/tournament/{id}/Players/GetPlayersContent`` list, falling
   back to the legacy ``/sport/players.aspx?id={id}`` table. Each player's page
   (``/sport/player.aspx?...`` etc.) yields the licence id (the parenthesised
   ``media__title-aside``) — or, when absent, the deterministic
   ``sha256_id(player_name)`` fallback — and the linked profile page yields the
   DOB / YOB.
3. **Matches.** Per player, read their match list (modern ``match`` cards or the
   legacy ``table.matches`` rows), determine winner/loser sides + score, join
   each side back onto the player table for name/DOB, and emit one CSV row per
   played match.

**Name handling & gender.** The scraped name is reordered to
``"Lastname, Firstname"`` via :func:`accounts.live_scrapers._names.last_first`
(matching the format the source's Claude call produced). Player **gender is
inferred from the name via Claude** (:func:`accounts.live_scrapers._claude_gender.
resolve_gender`, cached per distinct name), faithfully restoring the source's
``helper.format_name_gender_claude`` behaviour — the winner's gender in turn
drives ``draw_gender``. This is **Claude-only with no fallback**: if no Anthropic
key is configured (per-scraper → workspace/Settings → env), the run **fails
immediately** and asks for the key rather than emitting genderless rows. The
deterministic ``sha256_id`` fallback for the licence id is **kept** (reproduced
locally with :mod:`hashlib`). One source inconsistency is
corrected so the player join actually works: the licence id is derived the same
way wherever a player page is read (the original derived the fallback id from an
always-empty local in one branch, which broke the join).

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import hashlib
import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

from django.db.models import F
from django.utils import timezone
from parsel import Selector

from accounts.models import Run

from ._claude_gender import resolve_claude_keys, resolve_gender
from ._http import ScraperClient, build_proxies
from ._names import last_first
from .telemetry import Telemetry, redact_secrets, sanitize_cell

BASE = "https://etl.tournamentsoftware.com/"
COOKIE_LANG_URL = "https://etl.tournamentsoftware.com/cookiewall?lcid=1033"
COOKIE_SAVE_URL = "https://etl.tournamentsoftware.com/cookiewall/Save"
SEARCH_URL = "https://etl.tournamentsoftware.com/find/tournament/DoSearch"

# Country constants (the source hard-codes Estonia everywhere).
COUNTRY = "Estonia"
COUNTRY_CODE = COUNTRY[0:3].upper()  # "EST"

# Items CSV columns — the shared MatchMiner items schema (copied verbatim from
# the sibling scrapers), so downloaded files stay uniform across scrapers.
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


# ---------------------------------------------------------------------------
# Deterministic helpers (ports of the source's helper / fctcore utilities)
# ---------------------------------------------------------------------------
def sha256_id(s):
    """Deterministic ``local_`` fallback id — verbatim from ``helper.sha256_id``.

    Not AI: a stable SHA-256 of the player name, used as the ``third_party_id``
    when no licence id is found on the profile.
    """
    digest = hashlib.sha256((s or "").encode("utf-8")).digest()
    return "local_" + str(int.from_bytes(digest[:8], "big"))


def _pf(node, query):
    """``fctcore.parse_field`` — first ``xpath`` match's normalised text, or ``""``."""
    try:
        return (node.xpath(f"normalize-space({query})").get() or "").strip()
    except Exception:  # noqa: BLE001 - a bad selector must not abort a page
        return ""


def _strip_parens(value):
    """``fctcore.preg_repace`` for ``(`` / ``)`` — strip the licence-id brackets."""
    return re.sub(r"\(|\)", "", value or "").strip()


def _convert_date(date_str, in_format, out_format):
    """``fctcore.convert_string_to_date_format`` — reformat or ``""`` on failure."""
    try:
        return datetime.strptime((date_str or "").strip(), in_format).strftime(out_format)
    except Exception:  # noqa: BLE001 - missing/garbled date is non-fatal
        return ""


def _clean_name(name):
    """Drop a trailing ``[seed]`` annotation from a player name."""
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", name or "").strip()


def _parse_part_range_date(s):
    """Port of ``helper._parse_part_range_date`` (single calendar date token)."""
    formats = (
        "%b %d %Y", "%b %d", "%d %b %Y", "%d %b",
        "%B %d %Y", "%B %d", "%d %B %Y", "%d %B",
    )
    for f in formats:
        try:
            return datetime.strptime(s, f)
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {s}")


def _parse_tournament_range_date(s, fmt="%m/%d/%Y"):
    """Port of ``helper.parse_tournament_range_date`` — a calendar range to
    ``(start, end)`` in ``fmt`` (used by the single-URL header parser)."""
    s = (s or "").strip().replace(" - ", " to ")
    current_year = datetime.now().year

    if " to " not in s:
        dt = _parse_part_range_date(s)
        if dt.year == 1900:
            dt = dt.replace(year=current_year)
        return dt.strftime(fmt), dt.strftime(fmt)

    start_part, end_part = map(str.strip, s.split(" to "))
    start_dt = _parse_part_range_date(start_part)
    end_dt = _parse_part_range_date(end_part)

    if start_dt.year == 1900 and end_dt.year == 1900:
        if start_dt.month > end_dt.month:
            start_dt = start_dt.replace(year=current_year - 1)
            end_dt = end_dt.replace(year=current_year)
        else:
            start_dt = start_dt.replace(year=current_year)
            end_dt = end_dt.replace(year=current_year)
    elif start_dt.year == 1900:
        start_year = end_dt.year - 1 if start_dt.month > end_dt.month else end_dt.year
        start_dt = start_dt.replace(year=start_year)
    elif end_dt.year == 1900:
        end_dt = end_dt.replace(year=start_dt.year)

    return start_dt.strftime(fmt), end_dt.strftime(fmt)


# ---------------------------------------------------------------------------
# Cookie warm-up (English locale + accept cookies, like the source's Utils)
# ---------------------------------------------------------------------------
def _warm_up(client):
    """Set the English locale + accept the cookie wall so pages render in EN."""
    try:
        client.get(COOKIE_LANG_URL)
        client.post(
            COOKIE_SAVE_URL,
            data=[
                ("ReturnUrl", ""),
                ("SettingsOpen", "false"),
                ("CookiePurposes", "2"),
                ("CookiePurposes", "4"),
                ("CookiePurposes", "16"),
            ],
        )
    except Exception:  # noqa: BLE001 - warm-up failures are non-fatal
        pass


def _get_sel(client, url, cache):
    """GET ``url`` once per tournament, caching the parsel selector (or ``None``)."""
    if url in cache:
        return cache[url]
    sel = client.get_selector(url)
    cache[url] = sel
    return sel


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _discover_single(client, url, log):
    """Resolve a single tournament URL to one tournament record (or ``[]``)."""
    sel = client.get_selector(url)
    if sel is None:
        return []

    name = _pf(
        sel,
        '//div[contains(@class, "page-head")]//div[@class="media__content"]'
        '//h2[contains(@class, "media__title")]//span[contains(@class, "nav-link")]'
        '/span[@class="nav-link__value"]',
    )
    t_url, tid = "", ""
    pre = _pf(
        sel,
        '//ul[contains(@class, "page-nav")]//li[contains(@class, "page-nav__item")]'
        '//a[@class="page-nav__link" and contains(text(), "Overview")]/@href',
    )
    if pre:
        t_url = urljoin(BASE, pre)
        try:
            parts = urlparse(t_url).path.strip("/").split("/")
            idx = parts.index("tournament")
            tid = parts[idx + 1]
        except Exception:  # noqa: BLE001 - id is recovered in the variant below
            tid = ""

    city = start = end = ""
    for d1 in sel.xpath(
        '//div[@class="media__content"]//small[contains(@class, "media__subheading")]'
        '//span[@class="nav-link"]//span[@class="nav-link__value"]'
    ):
        use = d1.xpath("./svg/use").get()
        if use and "calendar.svg" in use:
            try:
                start, end = _parse_tournament_range_date(_pf(d1, "./."), "%m/%d/%Y")
            except Exception:  # noqa: BLE001 - unparseable range -> blank dates
                start, end = "", ""
        full = _pf(d1, "./.")
        if " | " in full:
            city = full.split("|")[-1].strip()

    # Newer markup variant (header.page-head + /time elements).
    if not name:
        name = _pf(
            sel,
            '//header[contains(@class, "page-head")]//div[@class="media__content"]'
            '//h2[contains(@class, "media__title")]//span[contains(@class, "media__link")]'
            '/span[@class="nav-link__value"]',
        )
        t_url, tid = "", ""
        pre = _pf(
            sel,
            '//ul[contains(@class, "page-nav")]//li[contains(@class, "page-nav__item")]'
            '//a[@class="page-nav__link" and contains(text(), "Overview")]/@href',
        )
        if pre:
            t_url = urljoin(BASE, pre)
            try:
                tid = parse_qs(urlparse(t_url).query)["id"][0]
            except Exception:  # noqa: BLE001 - leave id blank if absent
                tid = ""
        city = start = end = ""
        for d1 in sel.xpath(
            '//div[@class="media__content"]//small[contains(@class, "media__subheading")]'
            '//span[@class="nav-link"]//span[@class="nav-link__value"]'
        ):
            t1 = _pf(
                sel,
                '//div[@class="media__content"]//small[contains(@class, "media__subheading")]'
                '//span[@class="nav-link"]//span[@class="nav-link__value"]/time[1]',
            )
            t2 = _pf(
                sel,
                '//div[@class="media__content"]//small[contains(@class, "media__subheading")]'
                '//span[@class="nav-link"]//span[@class="nav-link__value"]/time[2]',
            )
            if t1:
                start = _convert_date(t1, "%d/%m/%Y", "%m/%d/%Y")
            if t2:
                end = _convert_date(t2, "%d/%m/%Y", "%m/%d/%Y")
            full = _pf(d1, "./.")
            if " | " in full:
                city = full.split("|")[-1].strip()

    if tid and name and t_url:
        return [{
            "tournament_id": tid,
            "tournament_name": name,
            "tournament_url": t_url,
            "tournament_start_date": start,
            "tournament_end_date": end,
            "tournament_city": city,
        }]
    return []


def _search_payload():
    """The Tournamentsoftware finder POST body (verbatim from the source)."""
    return {
        "LoadMoreResults": "LoadMoreResults",
        "Page": "1",
        "TournamentExtendedFilter.SportID": "0",
        "TournamentFilter.Q": "",
        "TournamentFilter.DateFilterType": "0",
        "TournamentFilter.StartDate": "2000-01-02",
        "TournamentFilter.EndDate": "2026-03-23",
        "TournamentFilter.PostalCode": "",
        "TournamentFilter.Distance": "15",
        "TournamentExtendedFilter.StatusFilterID": "false",
        "TournamentExtendedFilter.TournamentCategoryIDList[0]": "false",
        "TournamentExtendedFilter.TournamentCategoryIDList[1]": "false",
        "TournamentExtendedFilter.TournamentCategoryIDList[2]": "false",
        "TournamentExtendedFilter.TournamentCategoryIDList[3]": "false",
        "TournamentExtendedFilter.TournamentCategoryIDList[4]": "false",
        "TournamentExtendedFilter.TournamentCategoryIDList[5]": "false",
        "TournamentExtendedFilter.EventGameTypeIDList[0]": "false",
        "TournamentExtendedFilter.EventGameTypeIDList[1]": "false",
        "TournamentExtendedFilter.EventGameTypeIDList[2]": "false",
        "TournamentExtendedFilter.EventGameTypeIDList[3]": "false",
        "TournamentExtendedFilter.EventGameTypeIDList[4]": "false",
        "X-Requested-With": "XMLHttpRequest",
    }


def _discover_range(client, start_str, end_str, log):
    """Page the finder for every tournament whose dates fall in the window."""
    headers = {
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-requested-with": "XMLHttpRequest",
    }
    tournaments = []
    seen = set()
    page = 0
    while True:
        results_found = False
        page += 1
        data = _search_payload()
        data["Page"] = str(page)
        data["TournamentFilter.StartDate"] = start_str
        data["TournamentFilter.EndDate"] = end_str

        resp = client.post(SEARCH_URL, data=data, headers=headers)
        if resp is None or not (200 <= resp.status_code < 300):
            break
        sel = client.selector(resp)

        for d1 in sel.xpath('//li[@class="list__item"]//div[@class="media__content"]'):
            results_found = True
            name = _pf(d1, './/h4[@class="media__title"]/a/@title')
            t_url = urljoin(BASE, _pf(d1, './/h4[@class="media__title"]/a/@href'))
            tid = parse_qs(urlparse(t_url).query).get("id", [""])[0]
            start = _convert_date(
                _pf(
                    d1,
                    './/small[contains(@class, "media__subheading")]//span[@class="nav-link"]'
                    '//span[@class="nav-link__value"]/time[1]',
                ),
                "%m/%d/%Y", "%m/%d/%Y",
            )
            end = _convert_date(
                _pf(
                    d1,
                    './/small[contains(@class, "media__subheading")]//span[@class="nav-link"]'
                    '//span[@class="nav-link__value"]/time[2]',
                ),
                "%m/%d/%Y", "%m/%d/%Y",
            )
            if not end:
                end = start
            city = ""
            c = _pf(
                d1,
                './/small[@class="media__subheading"]//span[@class="nav-link"]'
                '/span[@class="nav-link__value"]',
            )
            if c:
                city = c.split("|")[-1].strip()

            if tid and name and t_url and tid not in seen:
                seen.add(tid)
                tournaments.append({
                    "tournament_id": tid,
                    "tournament_name": name,
                    "tournament_url": t_url,
                    "tournament_start_date": start,
                    "tournament_end_date": end,
                    "tournament_city": city,
                })

        if not results_found or page > 200:
            break

    log("INFO", f"\U0001f50e {len(tournaments)} tournament(s) in window")
    return tournaments


# ---------------------------------------------------------------------------
# Player list (modern -> legacy fallback)
# ---------------------------------------------------------------------------
def _tournament_player_links(client, tid):
    """Modern player list via ``/tournament/{id}/Players/GetPlayersContent``."""
    url = f"https://etl.tournamentsoftware.com/tournament/{(tid or '').lower()}/Players/GetPlayersContent"
    resp = client.post(
        url,
        headers={
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "x-requested-with": "XMLHttpRequest",
        },
        data={"X-Requested-With": "XMLHttpRequest"},
    )
    links = []
    if resp is not None and 200 <= resp.status_code < 300:
        sel = client.selector(resp)
        for a in sel.xpath(
            '//div[@id="PlayersView"]//ol//li[contains(@class, "list__item")]//h5/a'
        ):
            name = _pf(a, './span[@class="nav-link__value"]')
            href = _pf(a, "./@href")
            if href:
                links.append((name, urljoin(BASE, href)))
    return links


def _sport_player_links(client, tid):
    """Legacy player list via ``/sport/players.aspx?id={id}``."""
    url = f"https://etl.tournamentsoftware.com/sport/players.aspx?id={tid}"
    sel = client.get_selector(url)
    links = []
    if sel is not None:
        for a in sel.xpath(
            '//table[@class="players"]//tr//td//a[contains(@href, "/sport/player.aspx")]'
        ):
            name = _pf(a, "./text()")
            href = _pf(a, "./@href")
            if href:
                links.append((name, urljoin(BASE, href)))
    return links


# ---------------------------------------------------------------------------
# Player identity / DOB (from a player page + linked profile page)
# ---------------------------------------------------------------------------
def _player_id(sel, fallback_name):
    """Licence id from a player page (parenthesised aside), else ``sha256_id``.

    The id is derived the same way wherever a player page is read so the
    players-table key built in stage 2 matches the lookup key in stage 3.
    """
    if sel is None:
        return sha256_id(fallback_name)
    tpid = _strip_parens(_pf(
        sel,
        '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
        '//h4[contains(@class, "media__title")]/span[@class="media__title-aside"]',
    ))
    if not tpid:
        tpid = _strip_parens(_pf(
            sel,
            '//*[contains(@class, "page-head")]//div[@class="media__content"]'
            '//h2[contains(@class, "media__title")]/span[@class="media__title-aside"]',
        ))
    if not tpid:
        name = _pf(
            sel,
            '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
            '//h4[contains(@class, "media__title")]//span[@class="nav-link__value"]',
        ) or fallback_name
        tpid = sha256_id(name)
    return tpid


def _parse_dob(sel):
    """DOB (``dd/mm/yyyy`` -> ``mm/dd/yyyy``) or YOB (``1/1/yyyy``) from a profile."""
    for key in ("DOB:", "YOB:"):
        text = _pf(
            sel,
            '//div[contains(@class, "page-head")]//div[@class="media__content"]'
            '//div[contains(@class, "media__content-subinfo")]'
            '//small[contains(@class, "media__subheading")]/span[@class="nav-link"]'
            '/span[@class="nav-link__value" and contains(text(), "' + key + '")]',
        )
        if text:
            value = text.replace(key, "").strip()
            if key == "DOB:":
                got = _convert_date(value, "%d/%m/%Y", "%m/%d/%Y")
            else:
                got = f"1/1/{value}" if value else ""
            if got:
                return got
    return ""


def _player_dob(client, sel, cache):
    """Follow a player page's profile link and read the DOB/YOB, or ``""``."""
    href = _pf(
        sel,
        '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
        '//h4[contains(@class, "media__title")]/a/@href',
    )
    if not href:
        href = _pf(
            sel,
            '//div[@id="content"]//div[@class="subtitle"]//h2/a[@class="button"]/@href',
        )
    if not href:
        return ""
    psel = _get_sel(client, urljoin(BASE, href), cache)
    if psel is None:
        return ""
    return _parse_dob(psel)


# ---------------------------------------------------------------------------
# Match parsing (modern cards -> legacy table)
# ---------------------------------------------------------------------------
def _parse_match_body_tournament(sel):
    """Parse a modern ``match__body`` block into a match dict (or ``{}``)."""
    outcome = "Completed"
    if sel.xpath('.//*[contains(normalize-space(.),"Retired")]'):
        outcome = "Retired"

    rows = sel.xpath(
        './/div[contains(@class,"match__row-wrapper")]/div[contains(@class,"match__row")]'
    )
    row_players = []
    winner_row_index = None
    for idx, row in enumerate(rows):
        players = []
        for a in row.xpath('.//a[contains(@class,"nav-link")]'):
            name = a.xpath('.//span[@class="nav-link__value"]/text()').get()
            href = a.xpath("./@href").get()
            if name and href:
                players.append({
                    "name": _clean_name(name),
                    "profile_url": urljoin(BASE, href.strip()),
                })
        row_players.append(players)
        if "has-won" in (row.attrib.get("class", "") or ""):
            winner_row_index = idx

    if winner_row_index is None or len(row_players) < 2:
        return {}
    loser_row_index = 1 - winner_row_index
    winners = row_players[winner_row_index]
    losers = row_players[loser_row_index]

    scores = []
    for ul in sel.xpath('//div[contains(@class,"match__result")]//ul[@class="points"]'):
        cells = [c.xpath("normalize-space(text())").get() for c in ul.xpath("./li")]
        if len(cells) != 2:
            continue
        scores.append(f"{cells[winner_row_index]}-{cells[loser_row_index]}")

    draw_team_type = "Doubles" if len(winners) == 2 else "Singles"
    return {
        "draw_team_type": draw_team_type,
        "outcome": outcome,
        "score": ", ".join(scores) + ";" if scores else "",
        "winner_1": winners[0] if len(winners) > 0 else {},
        "winner_2": winners[1] if len(winners) > 1 else {},
        "loser_1": losers[0] if len(losers) > 0 else {},
        "loser_2": losers[1] if len(losers) > 1 else {},
    }


def _parse_matches_tournament(sel):
    """Yield modern match-card dicts (with score) for one player page."""
    matches = []
    for d1 in sel.xpath(
        '//div[@class="module-container"]/ul/li[@class="match-group__item"]/div[@class="match"]'
    ):
        if not d1.xpath(
            './/div[@class="match__body"]//div[contains(@class,"match__result")]//ul[@class="points"]'
        ).get():
            continue
        match_round = _pf(
            d1,
            './div[@class="match__header"]/ul[@class="match__header-title"]'
            '//li[@class="match__header-title-item"][1]/span[@class="nav-link"]'
            '/span[@class="nav-link__value"]',
        )
        draw_name = _pf(
            d1,
            './div[@class="match__header"]/ul[@class="match__header-title"]'
            '//li[@class="match__header-title-item"][2]/a[@class="nav-link"]'
            '/span[@class="nav-link__value"]',
        )
        if not draw_name and len(d1.xpath(
            './div[@class="match__header"]/ul[@class="match__header-title"]'
            '//li[@class="match__header-title-item"]'
        )) == 1:
            draw_name = _pf(
                d1,
                './div[@class="match__header"]/ul[@class="match__header-title"]'
                '//li[@class="match__header-title-item"][1]/a[@class="nav-link"]'
                '/span[@class="nav-link__value"]',
            )

        match_date = ""
        date_pre = _pf(
            d1,
            './div[@class="match__footer"]/ul[@class="match__footer-list"]'
            '//li[@class="match__footer-list-item"][1]/span[@class="nav-link"]'
            '/span[@class="nav-link__value"]',
        )
        m = re.search(r"\b\d{2}/\d{2}/\d{4}\b", date_pre)
        if m:
            match_date = _convert_date(m.group(), "%d/%m/%Y", "%m/%d/%Y")

        body_html = d1.xpath('.//div[@class="match__body"]').get()
        data = _parse_match_body_tournament(Selector(text=body_html)) if body_html else {}
        if data.get("score"):
            data.update({
                "draw_name": draw_name,
                "match_round": match_round,
                "match_date": match_date,
            })
            matches.append(data)
    return matches


def _parse_match_body_sport(row):
    """Parse one legacy ``table.matches`` row into a match dict."""
    def extract_players(td):
        players = []
        for a in td.xpath(".//a[contains(@href, 'player.aspx')]"):
            players.append({
                "name": a.xpath("normalize-space(.)").get(""),
                "profile_url": a.xpath("@href").get(""),
                "highlighted": "highlighted" in (a.xpath("@class").get("") or ""),
            })
        return players

    left = extract_players(row.xpath("td[4]"))
    right = extract_players(row.xpath("td[6]"))
    left_wins = any(p["highlighted"] for p in left)
    winners = left if left_wins else right
    losers = right if left_wins else left

    draw_team_type = "Doubles" if len(winners) == 2 else "Singles"
    raw_scores = row.xpath(".//span[@class='score']/span/text()").getall()
    score = ", ".join(raw_scores) + ";" if raw_scores else ""

    outcome = "Completed"
    if row.xpath('.//*[contains(normalize-space(.), "Retired")]'):
        outcome = "Retired"
        raw_scores = [s for s in raw_scores if "retired" not in s.lower()]
        score = ", ".join(raw_scores) + ";" if raw_scores else ""

    def _player(lst, idx):
        if idx < len(lst):
            pre = lst[idx]["profile_url"]
            if pre:
                return {
                    "name": _clean_name(lst[idx]["name"]),
                    "profile_url": f"https://etl.tournamentsoftware.com/sport/{pre}",
                }
        return {}

    return {
        "draw_team_type": draw_team_type,
        "outcome": outcome,
        "score": score,
        "winner_1": _player(winners, 0),
        "winner_2": _player(winners, 1),
        "loser_1": _player(losers, 0),
        "loser_2": _player(losers, 1),
    }


def _parse_matches_sport(sel):
    """Yield legacy match-row dicts (with score) for one player page."""
    matches = []
    for tr in sel.xpath(
        '//div[@id="content"]//table[contains(@class, "matches")]//tbody//tr'
    ):
        draw_name = _pf(tr, "./td[3]")
        match_date = ""
        date_pre = _pf(tr, "./td[1]")
        m = re.search(r"\b\d{2}/\d{2}/\d{4}\b", date_pre)
        if m:
            match_date = _convert_date(m.group(), "%d/%m/%Y", "%m/%d/%Y")
        data = _parse_match_body_sport(tr)
        if data.get("score"):
            data.update({
                "draw_name": draw_name,
                "match_round": "",
                "match_date": match_date,
            })
            matches.append(data)
    return matches


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
def _resolve(client, side, players_db, url_to_id, page_cache):
    """Resolve a match side onto the players table -> name/dob/gender/id."""
    side = side or {}
    url = side.get("profile_url", "") or ""
    name = side.get("name", "") or ""
    if not url:
        return {"name": "", "third_party_id": "", "dob": "", "gender": ""}
    tpid = url_to_id.get(url)
    if tpid is None:
        sel = _get_sel(client, url, page_cache)
        tpid = _player_id(sel, name) if sel is not None else sha256_id(name)
        url_to_id[url] = tpid
    rec = players_db.get(tpid)
    if rec:
        return {
            "name": rec["name"] or name,
            "third_party_id": tpid,
            "dob": rec["dob"],
            "gender": rec["gender"],
        }
    return {"name": name, "third_party_id": tpid, "dob": "", "gender": ""}


def _export_id(player):
    """Blank the ``local_`` (sha-fallback) ids in the exported row."""
    value = player["third_party_id"]
    return "" if "local_" in (value or "") else value


def _build_row(client, tournament, match, players_db, url_to_id, page_cache):
    """Assemble one CSV row dict from a parsed match + the players table."""
    match_date = match.get("match_date") or tournament.get("tournament_start_date", "")

    w1 = _resolve(client, match.get("winner_1"), players_db, url_to_id, page_cache)
    w2 = _resolve(client, match.get("winner_2"), players_db, url_to_id, page_cache)
    l1 = _resolve(client, match.get("loser_1"), players_db, url_to_id, page_cache)
    l2 = _resolve(client, match.get("loser_2"), players_db, url_to_id, page_cache)

    draw_gender = ""
    if w1["gender"] == "M":
        draw_gender = "Male"
    elif w1["gender"] == "F":
        draw_gender = "Female"

    row = {c: "" for c in COLUMNS}
    row.update({
        "match_id": "",
        "ball_type": "Yellow",
        "id_type": "Estonia",
        "draw_name": match.get("draw_name", ""),
        "draw_team_type": match.get("draw_team_type", ""),
        "tournament_name": tournament.get("tournament_name", ""),
        "date": match_date,
        "round": match.get("match_round", ""),
        "score": match.get("score", ""),
        "winner_1_name": last_first(w1["name"]), "winner_1_gender": w1["gender"],
        "winner_1_dob": w1["dob"], "winner_1_third_party_id": _export_id(w1),
        "winner_1_country": COUNTRY,
        "winner_2_name": last_first(w2["name"]), "winner_2_gender": w2["gender"],
        "winner_2_dob": w2["dob"], "winner_2_third_party_id": _export_id(w2),
        "winner_2_country": COUNTRY,
        "loser_1_name": last_first(l1["name"]), "loser_1_gender": l1["gender"],
        "loser_1_dob": l1["dob"], "loser_1_third_party_id": _export_id(l1),
        "loser_1_country": COUNTRY,
        "loser_2_name": last_first(l2["name"]), "loser_2_gender": l2["gender"],
        "loser_2_dob": l2["dob"], "loser_2_third_party_id": _export_id(l2),
        "loser_2_country": COUNTRY,
        "outcome": match.get("outcome", ""),
        "draw_gender": draw_gender,
        "tournament_city": tournament.get("tournament_city", ""),
        "tournament_country_code": COUNTRY_CODE,
        "tournament_import_source": COUNTRY,
        "tournament_sanction_body": COUNTRY,
        "tournament_event_type": "Tournament",
        "tournament_url": tournament.get("tournament_url", ""),
        "tournament_country": COUNTRY,
        "tournament_start_date": tournament.get("tournament_start_date", ""),
        "tournament_end_date": tournament.get("tournament_end_date", ""),
    })
    return row


# ---------------------------------------------------------------------------
# Per-tournament orchestration
# ---------------------------------------------------------------------------
def _scrape_tournament(client, tournament, claude_keys):
    """Build the players table then emit one row per played match."""
    tid = tournament.get("tournament_id", "")
    if not tid:
        return []

    page_cache = {}

    # Stage A: player list — modern first, legacy fallback.
    links = _tournament_player_links(client, tid)
    if links:
        match_parser = _parse_matches_tournament
    else:
        links = _sport_player_links(client, tid)
        match_parser = _parse_matches_sport
    if not links:
        return []

    # Stage B: players table keyed by third_party_id.
    players_db = {}
    url_to_id = {}
    for listing_name, purl in links:
        sel = _get_sel(client, purl, page_cache)
        if sel is None:
            continue
        tpid = _player_id(sel, listing_name)
        url_to_id[purl] = tpid
        if tpid not in players_db:
            players_db[tpid] = {
                "name": listing_name,
                "dob": _player_dob(client, sel, page_cache),
                # Claude-only gender (no fallback), faithfully restoring the
                # source's name→gender LLM call; cached per distinct name.
                "gender": resolve_gender(client, claude_keys, listing_name),
            }

    # Stage C: matches -> rows.
    rows = []
    for _listing_name, purl in links:
        sel = _get_sel(client, purl, page_cache)
        if sel is None:
            continue
        for match in match_parser(sel):
            rows.append(_build_row(client, tournament, match, players_db, url_to_id, page_cache))
    return rows


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run(run_obj, log):
    """Execute the Estonia tennis scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    params = run_obj.params or {}

    # Gender is inferred from player names via Claude with NO fallback, so a
    # Claude key is mandatory. Without one, fail before any scraping and ask for
    # the key rather than emitting genderless rows.
    claude_keys = resolve_claude_keys(scraper)
    if not claude_keys:
        msg = (
            "Anthropic API key required \u2014 the Estonia scraper infers player "
            "gender from names via Claude and has no fallback. Add a key on the "
            "Settings page (workspace-wide) or this scraper's Settings tab, then "
            "re-run."
        )
        tele.record_error(msg)
        log("ERROR", "\U0001f6d1 " + msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    tournament_url = (params.get("tournament_url") or "").strip()

    if tournament_url:
        log("INFO", "\U0001f3be Estonia tennis starting \u2014 single tournament URL")
        start_d = end_d = None
    else:
        start_d = run_obj.date_from or timezone.localdate()
        end_d = run_obj.date_to or timezone.localdate()
        log("INFO", f"\U0001f3be Estonia tennis starting \u2014 {start_d} \u2192 {end_d}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering tournaments \u2500\u2500\u2500\u2500")
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        _warm_up(discovery)
        if tournament_url:
            tournaments = _discover_single(discovery, tournament_url, log)
        else:
            tournaments = _discover_range(
                discovery,
                start_d.strftime("%Y-%m-%d"),
                end_d.strftime("%Y-%m-%d"),
                log,
            )

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
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            _warm_up(client)
            rows = _scrape_tournament(client, tournament, claude_keys)
            for row in rows:
                # The source dedups on the player names + score within a run.
                key = (
                    row.get("tournament_url", ""),
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
                    f"@ {row.get('tournament_name') or 'Estonia tennis'}",
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
            Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
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
