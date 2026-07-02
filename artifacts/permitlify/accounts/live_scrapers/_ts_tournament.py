"""Shared engine for tournamentsoftware.com **individual** tournaments.

Many national/regional federations publish their individual (non-team)
tournaments on a tournamentsoftware.com host (``hts.tournamentsoftware.com``,
``denmark.tournamentsoftware.com``, …). They share one markup and one set of
endpoints, differing only by host and a few constant fields (country, country
code, sanction body). This module ports that production
``*_tournament`` spider family onto MatchMiner's shared HTTP client
(:mod:`accounts.live_scrapers._http`) + telemetry, parameterised by a
:class:`TSTournamentConfig` so each federation is a thin wrapper (mirroring how
:mod:`accounts.live_scrapers._stadion` backs the Billie Jean King / Davis Cup
wrappers).

The real-time start form collects **either** a tournament URL **or** a date
window (``input_kind = date_range_or_url``):

* **tournament URL** — scrape that single tournament directly;
* **date range** — page the tournament search (``find/tournament/DoSearch``)
  between the two dates and scrape every tournament found.

For each tournament the crawl walks: tournament page → ``Players/GetPlayersContent``
(the entry list) → each player's profile → that player's match list
(``div.match`` blocks), then follows every opponent's profile for their
third-party id and date of birth. Because each match is reachable from **both**
players' pages, rows are de-duplicated by a content key.

Names are emitted in deterministic ``"Lastname, Firstname"`` order (cleaned of
seedings, then reordered via :func:`accounts.live_scrapers._names.last_first`
to match the Claude formatter the source applied — the cosmetic pretty-formatting
itself is dropped). Gender comes from the draw name by default, or from Claude
name inference when the config sets ``claude_gender`` (see
:class:`TSTournamentConfig`). DOB comes from the player profile / Biography tab
by default, or from the site-wide ranking tab when the config sets
``ranking_dob`` (Tennis Europe). ``run(config, run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

import csv
import io
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from django.db.models import F
from django.utils import timezone
from parsel import Selector

from accounts.models import Run

from ._gender import draw_gender_code, is_mixed_draw
from ._claude_gender import resolve_gender, resolve_claude_keys
from ._http import ScraperClient, build_proxies
from ._names import last_first
from .telemetry import Telemetry, redact_secrets, sanitize_cell

BALL_TYPE = "Yellow"
EVENT_TYPE = "Tournament"


@dataclass(frozen=True)
class TSTournamentConfig:
    """Per-federation constants for a tournamentsoftware individual-tournament site.

    ``base`` is the host root (no trailing slash), e.g.
    ``https://hts.tournamentsoftware.com``. ``country`` is the full country name
    (used for ``id_type`` / ``tournament_import_source``); ``country_code`` is
    the short code (used for the per-player country fields and
    ``tournament_country`` / ``tournament_country_code``). ``lcid`` selects the
    cookiewall locale (2057 = English by default).
    """

    label: str
    base: str
    country: str
    country_code: str
    sanction_body: str
    lcid: str = "2057"
    # --- dynamic-country mode --------------------------------------------
    # Some hosts (GLTA, Tennis Europe, COSAT, ITF Juniors) aggregate
    # tournaments from many countries on one site. There ``country`` /
    # ``country_code`` / ``sanction_body`` are not constant: the country is read
    # per-tournament from the search location, the per-player country from the
    # profile flag, and ``id_type`` / import-source / sanction come from a fixed
    # org label instead of a country. Setting ``dynamic_country`` switches the
    # engine to that behaviour; ``id_type_label`` feeds ``id_type`` and
    # ``org_label`` feeds both ``tournament_import_source`` and
    # ``tournament_sanction_body`` (they can differ, e.g. id_type ``Europe`` vs
    # org ``Tennis Europe``).
    dynamic_country: bool = False
    id_type_label: str = ""
    org_label: str = ""
    # --- Claude name->gender mode ----------------------------------------
    # The page markup has no gender field. By default gender is inferred from
    # the draw name (:func:`_gender.draw_gender_code`). For sites whose draw
    # names don't reliably carry a gender word (Croatia), set ``claude_gender``
    # to infer each player's gender from their name via Claude instead (cached;
    # requires a Claude key, else gender degrades to empty).
    claude_gender: bool = False
    # When ``claude_gender`` is on, ``claude_gender_required`` makes a Claude key
    # mandatory (Claude-only, no fallback): if none is configured the run fails
    # immediately and asks for the key rather than degrading to draw-name gender.
    # Used by Finland, Croatia and Tennis Europe, matching Estonia's contract.
    claude_gender_required: bool = False
    # --- ranking-tab DOB mode ---------------------------------------------
    # Junior sites (Tennis Europe) hide DOB/YOB from both the profile head and
    # the Biography tab, so per-profile DOB lookups come back empty. The
    # production source instead walked the site-wide **ranking tab** up front
    # (first ranking on ``{base}/ranking/`` → every "More" category list,
    # 100 rows/page) and recorded each ranked player's ``1/1/<YOB>`` keyed by
    # their profile GUID; match players were then joined against that registry.
    # Setting ``ranking_dob`` reproduces that exactly: a pre-phase builds the
    # GUID → DOB map and ``_parse_player`` reads DOB from it **only** (no
    # profile/Biography fallback — unranked players keep a blank DOB, exactly
    # like the source's registry miss).
    ranking_dob: bool = False


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

_RE_PARENS = re.compile(r"[()]")
_RE_SEED = re.compile(r"\s*\[[^\]]+\]\s*$")
_RE_DMY = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")


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


def _split_location(text):
    """Split a ``… | City, Country`` subheading into ``(city, country)``.

    The dynamic-country tournamentsoftware sites encode the host country after a
    comma in the location subheading. Fixed-country sites usually carry only the
    city (no comma), so ``country`` comes back ``""`` there and is ignored.
    """
    text = (text or "").strip()
    if "|" not in text:
        return "", ""
    tail = text.split("|")[-1].strip()
    parts = [p.strip() for p in tail.split(",")]
    city = parts[0] if parts else ""
    country = parts[1] if len(parts) > 1 else ""
    return city, country


# Player nationality flag — the dynamic-country sites carry the player's country
# as the title of the ``img.profile-head__nat`` flag. It lives on either the
# entry-list "subhead" page or the deeper profile "page-head"; try both.
_NAT_XPATHS = (
    '//div[contains(@class, "page-subhead")]//div[@class="media__img"]'
    '//div[contains(@class, "profile-icon")]/img[@class="profile-head__nat"]/@title',
    '//header[contains(@class, "page-head")]//div[@class="media__img"]'
    '//span[contains(@class, "profile-icon")]/img[@class="profile-head__nat"]/@title',
)


def _nat(sel):
    """Player nationality from the profile flag, or ``""``."""
    for xpath in _NAT_XPATHS:
        value = _field(sel, xpath)
        if value:
            return value
    return ""


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
    """Resolve a single tournament URL to one tournament dict (or ``[]``)."""
    sel = client.get_selector(tournament_url)
    if sel is None:
        log("WARN", "\u26a0\ufe0f Could not load the supplied tournament URL")
        return []

    name = _field(
        sel,
        '//div[contains(@class, "page-head")]//div[@class="media__content"]'
        '//h2[contains(@class, "media__title")]//span[contains(@class, "nav-link")]'
        '/span[@class="nav-link__value"]/text()',
    )

    href = _field(
        sel,
        '//ul[contains(@class, "page-nav")]//li[contains(@class, "page-nav__item")]'
        '//a[@class="page-nav__link" and contains(text(), "Overview")]/@href',
    )
    url = urljoin(cfg.base + "/", href) if href else tournament_url
    tournament_id = ""
    try:
        parts = urlparse(url).path.strip("/").split("/")
        idx = parts.index("tournament")
        tournament_id = parts[idx + 1]
    except (ValueError, IndexError):
        tournament_id = ""

    start_date = end_date = city = country = ""
    for d1 in sel.xpath(
        '//div[@class="media__content"]//small[contains(@class, "media__subheading")]'
        '//span[@class="nav-link"]//span[@class="nav-link__value"]'
    ):
        # ``@xlink:href`` would raise "Undefined namespace prefix" on pages
        # that don't declare the xlink namespace (parsel evaluates the prefix
        # per-document); match the attribute by local name instead so both
        # ``xlink:href`` and plain ``href`` resolve everywhere.
        use = d1.xpath('./svg/use/@*[local-name()="href"]').get() or ""
        if "calendar" in use:
            range_text = _field(d1, "normalize-space(.)")
            parts = re.split(r"\s*-\s*", range_text, maxsplit=1)
            start_date = _to_mdy(parts[0], ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"))
            end_date = (
                _to_mdy(parts[1], ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"))
                if len(parts) > 1
                else start_date
            ) or start_date
        text = _field(d1, "./text()")
        if "|" in text:
            city, country = _split_location(text)

    if not (tournament_id and name):
        log("WARN", "\u26a0\ufe0f Supplied URL did not resolve to a tournament")
        return []
    return [
        {
            "tournament_id": tournament_id,
            "tournament_name": name,
            "tournament_url": url,
            "tournament_start_date": start_date,
            "tournament_end_date": end_date,
            "tournament_city": city,
            "tournament_country": country,
        }
    ]


def _search_payload(page, start_date, end_date):
    """The ``find/tournament/DoSearch`` form body for one page."""
    data = {
        "LoadMoreResults": "LoadMoreResults",
        "Page": str(page),
        "TournamentExtendedFilter.SportID": "0",
        "TournamentFilter.Q": "",
        "TournamentFilter.DateFilterType": "0",
        "TournamentFilter.StartDate": start_date,
        "TournamentFilter.EndDate": end_date,
        "TournamentFilter.PostalCode": "",
        "TournamentFilter.Distance": "15",
        "TournamentExtendedFilter.CountryCode": "",
        "TournamentExtendedFilter.StatusFilterID": "false",
        "X-Requested-With": "XMLHttpRequest",
    }
    for i in range(10):
        data[f"TournamentExtendedFilter.TournamentCategoryIDList[{i}]"] = "false"
    for i in range(6):
        data[f"TournamentExtendedFilter.OrganizationCourtSurfaceTypeList[{i}]"] = "false"
    for i in range(5):
        data[f"TournamentExtendedFilter.EventGameTypeIDList[{i}]"] = "false"
    return urlencode(data)


def _discover_range(client, cfg, start_date, end_date, log):
    """Page the tournament search between two ``YYYY-MM-DD`` dates."""
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }
    search_url = f"{cfg.base}/find/tournament/DoSearch"
    tournaments = []
    seen = set()
    page = 0
    while True:
        page += 1
        resp = client.post(
            search_url,
            data=_search_payload(page, start_date, end_date),
            headers=headers,
        )
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
            tournament_id = parse_qs(urlparse(url).query).get("id", [""])[0]
            if not tournament_id or tournament_id in seen:
                continue
            found = True
            seen.add(tournament_id)

            t_start = _to_mdy(
                _field(
                    d1,
                    './/small[contains(@class, "media__subheading")]'
                    '//span[@class="nav-link"]//span[@class="nav-link__value"]/time[1]/text()',
                ),
                ("%d/%m/%Y",),
            )
            t_end = _to_mdy(
                _field(
                    d1,
                    './/small[contains(@class, "media__subheading")]'
                    '//span[@class="nav-link"]//span[@class="nav-link__value"]/time[2]/text()',
                ),
                ("%d/%m/%Y",),
            ) or t_start

            city, country = _split_location(
                _field(
                    d1,
                    './/small[@class="media__subheading"]//span[@class="nav-link"]'
                    '/span[@class="nav-link__value"]/text()',
                )
            )

            tournaments.append(
                {
                    "tournament_id": tournament_id,
                    "tournament_name": name,
                    "tournament_url": url,
                    "tournament_start_date": t_start,
                    "tournament_end_date": t_end,
                    "tournament_city": city,
                    "tournament_country": country,
                }
            )
        log(
            "INFO",
            f"   \U0001f50e search page {page}: {len(tournaments)} tournament(s) so far",
        )
        if not found:
            break
    return tournaments


# ======================================================================
# Per-tournament crawl
# ======================================================================
def _list_players(client, cfg, tournament):
    """Return ``[(player_url, ctx)]`` for every entrant of one tournament."""
    tournament_id = tournament.get("tournament_id", "")
    if not tournament_id:
        return []
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }
    url = (
        f"{cfg.base}/tournament/{tournament_id.lower()}/Players/GetPlayersContent"
    )
    resp = client.post(url, data=urlencode({"X-Requested-With": "XMLHttpRequest"}), headers=headers)
    if resp is None or not (200 <= resp.status_code < 300):
        return []
    sel = Selector(text=resp.text)
    ctx = {
        "tournament_name": tournament.get("tournament_name", ""),
        "tournament_url": tournament.get("tournament_url", ""),
        "tournament_start_date": tournament.get("tournament_start_date", ""),
        "tournament_end_date": tournament.get("tournament_end_date", ""),
        "tournament_city": tournament.get("tournament_city", ""),
        "tournament_country": tournament.get("tournament_country", ""),
    }
    items = []
    for a in sel.xpath(
        '//div[@id="PlayersView"]//ol//li[contains(@class, "list__item")]//h5/a'
    ):
        href = a.xpath("./@href").get()
        if href:
            items.append((urljoin(cfg.base + "/", href.strip()), ctx))
    return items


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


def _parse_birth_year(sel):
    """Year of birth from a player's Biography tab → ``1/1/YYYY`` (or ``""``).

    Junior profiles (e.g. Tennis Europe) don't surface DOB/YOB in the profile
    page-head that :func:`_parse_dob` reads, but the Biography tab lists a
    ``"Year of birth"`` definition row.
    """
    value = _field(
        sel,
        'normalize-space(//dt[contains(normalize-space(.), "Year of birth")]'
        "/following-sibling::dd[1])",
    )
    match = re.search(r"(?:19|20)\d{2}", value or "")
    return f"1/1/{match.group()}" if match else ""


def _ranking_rows(sel):
    """Yield ``(profile_guid, "1/1/YOB")`` pairs from one ranking listing page.

    Mirrors the source's ranking parser: player link in ``td[4]`` (href
    ``../profile/default.aspx?id=<GUID>``, split on ``?id=`` and lowercased),
    year of birth in ``td[5]``.
    """
    for row in sel.xpath(
        '//div[@id="content"]//table[@class="ruler"]//tr[td[@class="rank"]]'
    ):
        href = row.xpath(".//td[4]/a/@href").get() or ""
        yob = (row.xpath("normalize-space(.//td[5])").get() or "")
        yob = yob.replace("\xa0", " ").strip()
        if "?id=" not in href or not yob.isdigit():
            continue
        guid = href.split("?id=")[-1].split("&")[0].strip().lower()
        if guid:
            yield guid, f"1/1/{yob}"


def _ranking_dob_seed(client, cfg, log):
    """Walk the ranking tab and seed the DOB registry (``ranking_dob`` mode).

    Follows the source's traversal exactly: ``{base}/ranking/`` → the **first**
    ranking's category page → every ``More`` category list with ``&ps=100``.
    Parses each list's first page here and returns ``(dob_map, page_urls)``
    where ``page_urls`` are the remaining paginated pages (``&p=2..N``, from
    the ``page_caption`` result count / 100) still to fetch — the caller
    fetches those concurrently and merges their rows into ``dob_map``.
    """
    index = cfg.base + "/ranking/"
    dob_map, page_urls = {}, []
    sel = client.get_selector(index)
    if sel is None:
        return dob_map, page_urls
    cat_href = _field(
        sel, '//div[@id="content"]//table[@class="ruler"]//tr[1]/td/h5/a/@href'
    )
    if not cat_href:
        return dob_map, page_urls
    cat_sel = client.get_selector(urljoin(index, cat_href))
    if cat_sel is None:
        return dob_map, page_urls
    mores = cat_sel.xpath(
        '//div[@id="content"]//table[@class="ruler"]//tr/th/a[text()="More"]/@href'
    ).getall()
    for more in mores:
        page_url = urljoin(index, more.strip() + "&ps=100")
        first = client.get_selector(page_url)
        if first is None:
            continue
        dob_map.update(_ranking_rows(first))
        caption = _field(
            first,
            'normalize-space(//div[@class="pagenumbers"]'
            '//span[@class="page_caption"])',
        )
        pages = 0
        try:
            # e.g. "Page 1 of 35 - 3493 results" → 3493 → ceil(/100) pages.
            # (comma-stripped in case the site ever thousands-separates counts)
            count = caption.split(" - ")[-1].strip().split()[0].replace(",", "")
            pages = math.ceil(int(count) / 100)
        except (ValueError, IndexError):
            pass
        page_urls.extend(f"{page_url}&p={page}" for page in range(2, pages + 1))
    log(
        "INFO",
        f"\U0001f4c7 Ranking tab: {len(mores)} categorie(s), "
        f"{len(dob_map)} player(s) from first pages, "
        f"{len(page_urls)} more page(s) to fetch",
    )
    return dob_map, page_urls


def _parse_player(client, cfg, name, url, dob_map=None):
    """Resolve a player's ``(name, third_party_id, dob, gender, country)``.

    Gender is left empty here and filled in by :func:`_build_row` — from the
    draw name by default, or (when ``cfg.claude_gender`` is set) inferred from
    the player's name via Claude. The name is cleaned of seedings then reordered
    to ``"Lastname, Firstname"`` via :func:`._names.last_first`. ``country`` is
    the player's nationality (from the profile flag) for dynamic-country sites,
    else ``""`` — fixed-country sites fill the per-player country from the
    federation constant in :func:`_build_row`.
    """
    name = last_first(name)
    if not (name and url):
        return name, "", "", "", ""
    sel = client.get_selector(url)
    if sel is None:
        return name, "", "", "", ""

    third_party_id = _field(
        sel,
        '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
        '//h4[contains(@class, "media__title")]/span[@class="media__title-aside"]/text()',
    )
    third_party_id = _RE_PARENS.sub("", third_party_id).strip()

    country = _nat(sel) if cfg.dynamic_country else ""

    dob = ""
    profile_href = _field(
        sel,
        '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
        '//h4[contains(@class, "media__title")]/a/@href',
    )
    if profile_href and cfg.ranking_dob:
        # Ranking-tab DOB mode: join this player to the pre-built ranking
        # registry by profile GUID (the ``/player-profile/<guid>`` tail) and
        # take the ``1/1/YOB`` recorded there — the source's registry join,
        # with no profile/Biography fallback. Unranked players stay blank.
        guid = profile_href.rstrip("/").split("/")[-1].strip().lower()
        dob = (dob_map or {}).get(guid, "")
        if cfg.dynamic_country and not country:
            profile_sel = client.get_selector(urljoin(cfg.base + "/", profile_href))
            if profile_sel is not None:
                country = _nat(profile_sel)
    elif profile_href:
        profile_url = urljoin(cfg.base + "/", profile_href)
        profile_sel = client.get_selector(profile_url)
        if profile_sel is not None:
            dob = _parse_dob(profile_sel)
            if cfg.dynamic_country and not country:
                country = _nat(profile_sel)
        if not dob:
            # Juniors hide DOB from the profile head but list a "Year of birth"
            # on the Biography tab — one extra request only where DOB is missing.
            bio_sel = client.get_selector(profile_url.rstrip("/") + "/biography")
            if bio_sel is not None:
                dob = _parse_birth_year(bio_sel)
    return name, third_party_id, dob, "", country


def _build_row(client, cfg, ctx, match_data):
    """Assemble one full items row from a parsed match + player lookups."""
    w1 = match_data.get("winner_1", {})
    w2 = match_data.get("winner_2", {})
    l1 = match_data.get("loser_1", {})
    l2 = match_data.get("loser_2", {})

    dob_map = ctx.get("dob_map")
    w1_name, w1_id, w1_dob, w1_g, w1_c = _parse_player(client, cfg, w1.get("name", ""), w1.get("profile_url", ""), dob_map)
    w2_name, w2_id, w2_dob, w2_g, w2_c = _parse_player(client, cfg, w2.get("name", ""), w2.get("profile_url", ""), dob_map)
    l1_name, l1_id, l1_dob, l1_g, l1_c = _parse_player(client, cfg, l1.get("name", ""), l1.get("profile_url", ""), dob_map)
    l2_name, l2_id, l2_dob, l2_g, l2_c = _parse_player(client, cfg, l2.get("name", ""), l2.get("profile_url", ""), dob_map)

    draw_name = ctx.get("draw_name", "")
    gcode = draw_gender_code(draw_name)
    claude_keys = ctx.get("claude_keys")
    if cfg.claude_gender and claude_keys:
        # The draw name here doesn't reliably carry a gender word, so infer each
        # player's gender from their name via Claude (cached per distinct name).
        w1_g = resolve_gender(client, claude_keys, w1_name) if w1_name else ""
        w2_g = resolve_gender(client, claude_keys, w2_name) if w2_name else ""
        l1_g = resolve_gender(client, claude_keys, l1_name) if l1_name else ""
        l2_g = resolve_gender(client, claude_keys, l2_name) if l2_name else ""
        # Draw-level gender: an explicit draw-name gender wins; a genuinely mixed
        # draw stays blank; otherwise fall back to the winner's inferred gender.
        if gcode:
            draw_gender = "Male" if gcode == "M" else "Female"
        elif is_mixed_draw(draw_name):
            draw_gender = ""
        else:
            draw_gender = "Male" if w1_g == "M" else ("Female" if w1_g == "F" else "")
    else:
        # Default: gender is carried by the draw name (e.g. "Boys Singles" /
        # "Juniorke pojedinačno"); every player in the match inherits it.
        w1_g = gcode if w1_name else ""
        w2_g = gcode if w2_name else ""
        l1_g = gcode if l1_name else ""
        l2_g = gcode if l2_name else ""
        draw_gender = "Male" if gcode == "M" else ("Female" if gcode == "F" else "")
    date = ctx.get("match_date", "") or ctx.get("tournament_start_date", "")

    if cfg.dynamic_country:
        # Country is per-tournament (from the search location) and per-player
        # (from the profile flag); the org labels are fixed.
        t_country = ctx.get("tournament_country", "")
        t_country_code = t_country[0:3].upper() if t_country else ""
        id_type = cfg.id_type_label
        import_source = cfg.org_label
        sanction = cfg.org_label
        w1_country, w2_country = w1_c, w2_c
        l1_country, l2_country = l1_c, l2_c
    else:
        # Fixed-country federation: every field is the federation constant.
        t_country = cfg.country_code
        t_country_code = cfg.country_code
        id_type = cfg.country
        import_source = cfg.country
        sanction = cfg.sanction_body
        w1_country = cfg.country_code if w1_name else ""
        w2_country = cfg.country_code if w2_name else ""
        l1_country = cfg.country_code if l1_name else ""
        l2_country = cfg.country_code if l2_name else ""

    return {
        "match_id": "",
        "ball_type": BALL_TYPE,
        "id_type": id_type,
        "draw_bracket_value": "",
        "draw_name": ctx.get("draw_name", ""),
        "draw_team_type": match_data.get("draw_team_type", ""),
        "tournament_name": ctx.get("tournament_name", ""),
        "date": date,
        "round": ctx.get("match_round", ""),
        "score": match_data.get("score", ""),
        "winner_1_name": w1_name,
        "winner_1_gender": w1_g,
        "winner_1_dob": w1_dob,
        "winner_1_third_party_id": w1_id,
        "winner_1_city": "",
        "winner_1_state": "",
        "winner_1_country": w1_country,
        "winner_2_name": w2_name,
        "winner_2_gender": w2_g,
        "winner_2_dob": w2_dob,
        "winner_2_third_party_id": w2_id,
        "winner_2_city": "",
        "winner_2_state": "",
        "winner_2_country": w2_country,
        "loser_1_name": l1_name,
        "loser_1_gender": l1_g,
        "loser_1_dob": l1_dob,
        "loser_1_third_party_id": l1_id,
        "loser_1_city": "",
        "loser_1_state": "",
        "loser_1_country": l1_country,
        "loser_2_name": l2_name,
        "loser_2_gender": l2_g,
        "loser_2_dob": l2_dob,
        "loser_2_third_party_id": l2_id,
        "loser_2_city": "",
        "loser_2_state": "",
        "loser_2_country": l2_country,
        "outcome": match_data.get("outcome", ""),
        "draw_gender": draw_gender,
        "draw_bracket_type": "",
        "draw_type": "",
        "tournament_city": ctx.get("tournament_city", ""),
        "tournament_state": "",
        "tournament_country_code": t_country_code,
        "tournament_host": "",
        "tournament_location_type": "",
        "tournament_surface": "",
        "tournament_event_category": "",
        "tournament_event_grade": "",
        "tournament_import_source": import_source,
        "tournament_sanction_body": sanction,
        "winner_2_college": "",
        "loser_2_college": "",
        "tournament_event_type": EVENT_TYPE,
        "winner_1_college": "",
        "loser_1_college": "",
        "tournament_url": ctx.get("tournament_url", ""),
        "tournament_country": t_country,
        "tournament_start_date": ctx.get("tournament_start_date", ""),
        "tournament_end_date": ctx.get("tournament_end_date", ""),
    }


def _parse_player_matches(client, cfg, ctx, player_url):
    """Fetch one player's profile and return parsed rows for their matches."""
    sel = client.get_selector(player_url)
    if sel is None:
        return []

    rows = []
    for d1 in sel.xpath(
        '//div[@class="module-container"]/ul/li[@class="match-group__item"]'
        '/div[@class="match"]'
    ):
        body = d1.xpath('.//div[@class="match__body"]').get()
        if not body:
            continue
        # Only completed matches carry a points list; skip walkovers/byes.
        if not d1.xpath(
            './/div[@class="match__body"]//div[contains(@class,"match__result")]'
            '//ul[@class="points"]'
        ):
            continue

        match_round = _field(
            d1,
            './div[@class="match__header"]/ul[@class="match__header-title"]'
            '//li[@class="match__header-title-item"][1]/span[@class="nav-link"]'
            '/span[@class="nav-link__value"]/text()',
        )
        draw_name = _field(
            d1,
            './div[@class="match__header"]/ul[@class="match__header-title"]'
            '//li[@class="match__header-title-item"][2]/a[@class="nav-link"]'
            '/span[@class="nav-link__value"]/text()',
        )
        if not draw_name and len(
            d1.xpath(
                './div[@class="match__header"]/ul[@class="match__header-title"]'
                '//li[@class="match__header-title-item"]'
            )
        ) == 1:
            draw_name = _field(
                d1,
                './div[@class="match__header"]/ul[@class="match__header-title"]'
                '//li[@class="match__header-title-item"][1]/a[@class="nav-link"]'
                '/span[@class="nav-link__value"]/text()',
            )

        match_date = ""
        footer = _field(
            d1,
            './div[@class="match__footer"]/ul[@class="match__footer-list"]'
            '//li[@class="match__footer-list-item"][1]/span[@class="nav-link"]'
            '/span[@class="nav-link__value"]/text()',
        )
        m = _RE_DMY.search(footer)
        if m:
            match_date = _to_mdy(m.group(), ("%d/%m/%Y",))

        match_data = _parse_match(Selector(text=body), cfg)
        if not (match_data and match_data.get("score")):
            continue

        match_ctx = dict(ctx)
        match_ctx.update(
            {"match_round": match_round, "match_date": match_date, "draw_name": draw_name}
        )
        rows.append(_build_row(client, cfg, match_ctx, match_data))
    return rows


def _window(run_obj):
    """Resolve the ``(start, end)`` YYYY-MM-DD search window from the run."""
    today = timezone.localdate()
    start = run_obj.date_from or today
    end = run_obj.date_to or today
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def run(cfg, run_obj, log):
    """Execute one tournamentsoftware individual-tournament scrape.

    Returns the standard 5-tuple. Work is parallelised the way the Croatia
    League port handles team-matches: discovery is a single warm session, then
    every entrant of every tournament is fetched concurrently by a pool of
    ``worker_count`` warmed sessions (one per thread). Opponent-profile lookups
    within a player page stay serial on that thread, and rows are de-duplicated
    because each match is reachable from both players.
    """
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
    claude_keys = resolve_claude_keys(scraper) if cfg.claude_gender else []
    if cfg.claude_gender:
        if claude_keys:
            log("INFO", "\U0001f9e0 Gender: Claude name inference enabled (cached)")
        elif cfg.claude_gender_required:
            # Claude-only gender with no fallback: without a key, fail the run
            # and ask for one rather than emitting genderless rows.
            msg = (
                f"Anthropic API key required \u2014 {cfg.label} infers player "
                "gender from names via Claude and has no fallback. Add a key on "
                "the Settings page (workspace-wide) or this scraper's Settings "
                "tab, then re-run."
            )
            tele.record_error(msg)
            log("ERROR", "\U0001f6d1 " + msg)
            return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED
        else:
            log(
                "WARN",
                "\u26a0\ufe0f claude_gender set but no Claude key configured "
                "\u2014 falling back to draw-name gender only "
                "(per-player gender will be blank for genderless draws)",
            )

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering tournaments \u2500\u2500\u2500\u2500")
    dob_map, ranking_pages = {}, []
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        _warmup(discovery, cfg)
        if tournament_url:
            tournaments = _discover_one(discovery, cfg, tournament_url, log)
        else:
            tournaments = _discover_range(discovery, cfg, start_date, end_date, log)
        if cfg.ranking_dob and tournaments:
            # ---- phase 1b · ranking-tab DOB registry (see TSTournamentConfig)
            log(
                "INFO",
                "\u2500\u2500\u2500\u2500 phase 1b \u00b7 ranking-tab DOB registry "
                "\u2500\u2500\u2500\u2500",
            )
            dob_map, ranking_pages = _ranking_dob_seed(discovery, cfg, log)
    log("INFO", f"\U0001f4cb {len(tournaments)} tournament(s) discovered")

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

    def list_one(tournament):
        try:
            return _list_players(client_for(), cfg, tournament)
        except Exception as exc:  # noqa: BLE001 - a bad tournament can't kill the run
            tele.record_error(
                redact_secrets(
                    f"List players {tournament.get('tournament_url', '')} failed: {exc}"
                ),
                exc=exc,
            )
            return []

    rank_lock = threading.Lock()

    def rank_one(url):
        try:
            sel = client_for().get_selector(url)
            if sel is None:
                return
            pairs = list(_ranking_rows(sel))
            with rank_lock:
                dob_map.update(pairs)
        except Exception as exc:  # noqa: BLE001 - a bad page can't kill the run
            tele.record_error(
                redact_secrets(f"Ranking page {url} failed: {exc}"), exc=exc
            )

    def crawl_one(item):
        player_url, ctx = item
        if claude_keys:
            ctx = {**ctx, "claude_keys": claude_keys}
        if cfg.ranking_dob:
            ctx = {**ctx, "dob_map": dob_map}
        try:
            rows = _parse_player_matches(client_for(), cfg, ctx, player_url)
            for row in rows:
                # Each match is reachable from both players' pages, so dedupe on
                # a content key without collapsing genuine rematches (same
                # players/score in a different draw, round or date).
                key = (
                    row.get("tournament_url", ""),
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
        except Exception as exc:  # noqa: BLE001 - a bad player can't kill the run
            tele.record_error(
                redact_secrets(f"Player {player_url} failed: {exc}"), exc=exc
            )
            log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f player failed: {exc.__class__.__name__}: {exc}"
                ),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(
                progress_done=F("progress_done") + 1
            )

    try:
        if tournaments:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                # ---- phase 1b (cont.) · fetch remaining ranking pages ----
                if ranking_pages:
                    list(executor.map(rank_one, ranking_pages))
                if cfg.ranking_dob:
                    if dob_map:
                        log(
                            "INFO",
                            f"\U0001f4c7 Ranking DOB registry ready \u2014 "
                            f"{len(dob_map)} ranked player(s)",
                        )
                    else:
                        log(
                            "WARN",
                            "\u26a0\ufe0f Ranking DOB registry is empty \u2014 "
                            "DOBs will be blank this run",
                        )

                # ---- phase 2 · list every entrant (light) ----
                log(
                    "INFO",
                    "\u2500\u2500\u2500\u2500 phase 2 \u00b7 listing entrants \u2500\u2500\u2500\u2500",
                )
                nested = executor.map(list_one, tournaments)
                work = [item for sub in nested for item in sub]
                Run.objects.filter(pk=run_obj.pk).update(
                    progress_total=len(work), progress_done=0
                )
                log("INFO", f"\U0001f5fa\ufe0f {len(work)} entrant(s) to scrape")

                # ---- phase 3 · scrape each entrant's matches concurrently ----
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
