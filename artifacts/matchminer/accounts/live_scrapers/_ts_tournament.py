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
itself is dropped). Gender comes from the draw/tournament name by default, with
optional Claude name inference for sources whose names do not always carry a
gender signal (see :class:`TSTournamentConfig`). DOB comes from the player profile / Biography tab
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
from datetime import datetime, timedelta
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urldefrag

from django.db.models import F
from django.utils import timezone
from parsel import Selector

from accounts.models import Run

from ._gender import draw_gender_code, is_mixed_draw
from ._claude_gender import resolve_gender, resolve_claude_keys
from ._country_codes import resolve_country_code
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
    # Fixed-country sources normally use ``country`` as their import source.
    # Set this when the source label differs from the ID-type country label.
    import_source: str = ""
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
    # Softer variant for sources such as Ireland: trust a deterministic draw or
    # tournament gender when present, and use cached Claude name inference only
    # for genderless/ambiguous draws.
    claude_gender_fallback: bool = False
    # --- Claude country-code mode (dynamic-country sites) ------------------
    # How ``tournament_country_code`` is derived from the per-tournament
    # country name differs between the dynamic-country sources: COSAT's took
    # ``country[0:3].upper()`` (the engine default), while GLTA's looked the
    # name up in a fixed known-codes table and asked **Claude** for anything
    # not in it (``Utils.convert_full_country``) — and since GLTA renders
    # ``"U.S.A."`` (not a table key), Claude is its common case. Setting
    # ``claude_country`` reproduces that: table first, Claude for the rest,
    # cached per run, no other fallback. A Claude key is mandatory (the run
    # fails up front without one, like ``claude_gender_required``).
    claude_country: bool = False
    # --- ranking-tab DOB mode ---------------------------------------------
    # Some sites expose DOB via the site-wide **ranking tab**, walked up front:
    # every ranking on ``{base}/ranking/`` → each ranking overview's per-category
    # "More" links → each category's server-rendered ``ranking.aspx`` preview
    # (see :func:`_ranking_dob_seed`). Each ranked player's DOB is recorded by
    # profile GUID and match players are joined against that registry
    # (``_parse_player`` reads it **only**, no profile/Biography fallback).
    # Setting ``ranking_dob`` turns this on. **Only COSAT uses it today**, always
    # paired with ``ranking_dob_full_date`` below (its ranking carries a full
    # date). Tennis Europe used the bare year-only ``ranking_dob`` branch but
    # moved to ``biography_dob``: TE's ``/ranking/`` has TWO listings — a singles
    # one with a "Year of birth" column and a doubles one WITHOUT (points in the
    # same slot) — so the bare reader recorded doubles points as birth years. Its
    # Biography tab has YOB for every player, so that non-full-date reader path is
    # now unused (kept only for the full-date COSAT layout).
    ranking_dob: bool = False
    # COSAT's ranking lists a **full DOB**, not a YOB: its More links sit on
    # the ``/ranking/`` index itself, the profile link is in ``td[5]``
    # (``/player-profile/<GUID>``) and ``td[6]`` carries a complete date.
    # COSAT ignores the default en-GB cookiewall locale (2057) and stays
    # Spanish, so the config also sets ``lcid="1033"`` (en-US) exactly like
    # the source did — English "More" labels, ``m/d/Y`` dates parsed with the
    # source's ``%m/%d/%Y``. Setting ``ranking_dob_full_date`` switches
    # :func:`_ranking_rows` to that layout, normalising to ``MM/DD/YYYY`` —
    # the exact value the source's registry stored.
    ranking_dob_full_date: bool = False
    # --- biography DOB mode -----------------------------------------------
    # Tennis Europe juniors hide DOB/YOB from the profile head, but every
    # player's **Biography tab** (``/player-profile/<GUID>/biography``) lists a
    # "Year of birth" — for ranked AND unranked players alike. Setting
    # ``biography_dob`` makes ``_parse_player`` read that YOB (as ``1/1/YYYY``)
    # per player, lazily and cached per run by profile GUID (the shared
    # ``dob_map``) so each unique player's biography is fetched at most once even
    # though players recur across many matches. This gives full DOB coverage,
    # unlike the ranked-only ``ranking_dob`` registry it replaced.
    biography_dob: bool = False
    # --- GUID third-party id ---------------------------------------------
    # On some sites (Tennis Europe juniors, COSAT) the player subhead shows a
    # member-id ``media__title-aside`` (a national-federation id, e.g. COSAT's
    # ``NIN…`` number) rather than the tournamentsoftware player id the framework
    # wants. Setting ``guid_third_party_id`` takes the id from the subhead's
    # player-profile link (``/player-profile/<GUID>``) instead — the genuine
    # source-platform id.
    guid_third_party_id: bool = False
    # GUID case: Tennis Europe's canonical profile URLs are upper-case, so its
    # id is emitted upper-cased (the default). COSAT's profile URLs are
    # lower-case, so it sets this False to emit the GUID in the site's own case.
    guid_third_party_id_upper: bool = True
    # Some TournamentSoftware hosts search by tournament start date only. For
    # tournaments that start before the requested window but continue into it,
    # discover from an earlier date and filter emitted rows by match date.
    discovery_lookback_days: int = 0
    # Tournament names containing any of these case-insensitive terms are skipped.
    # Used for federations whose TournamentSoftware search mixes tennis with
    # other racket sports (e.g. Luxembourg padel events).
    exclude_name_terms: tuple = ()
    # Opt-in discovery for TournamentSoftware team-event pages linked from
    # tournament/draw pages. Default-off to avoid changing other wrappers.
    discover_team_matches: bool = False


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
    '//*[contains(@class, "page-head")]//img[contains(@class, "profile-head__nat")]/@title',
    '//img[contains(@class, "profile-head__nat")]/@title',
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
    if not name:
        title = _field(sel, "normalize-space(//title)") or _field(
            sel, "normalize-space(//h2)"
        )
        title_parts = [part.strip() for part in title.split(" - ") if part.strip()]
        if len(title_parts) > 2:
            name = " - ".join(title_parts[1:-1])
        elif title_parts:
            name = title_parts[-1]

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
    if not tournament_id:
        tournament_id = parse_qs(urlparse(url).query).get("id", [""])[0]

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
        # Full string of the span, not just its first text node: the location
        # value is ``Org | <flag img> City, Country`` — the flag ``<img>``
        # splits the text into two nodes, so ``./text()`` alone would stop at
        # ``"Org | "`` and lose the city/country (the source's parse_field
        # read the node's full text: ``parse_field('./.', d1)``).
        text = _field(d1, "normalize-space(.)")
        if "|" in text:
            city, country = _split_location(text)

    # Modern overview pages expose locale-independent tournament boundaries in
    # their timeline even when the visible calendar label omits the year.
    start_iso = _field(
        sel,
        '(//li[contains(concat(" ", normalize-space(@class), " "), " is-started ")]'
        '//time)[1]/@datetime',
    )
    end_iso = _field(
        sel,
        '(//li[contains(concat(" ", normalize-space(@class), " "), " is-finished ")]'
        '//time)[1]/@datetime',
    )
    start_date = _to_mdy(start_iso.partition("T")[0], ("%Y-%m-%d",)) or start_date
    end_date = (
        _to_mdy(end_iso.partition("T")[0], ("%Y-%m-%d",))
        or end_date
        or start_date
    )

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

            # Search-result <time> dates render in the cookiewall locale:
            # d/m/Y under the default en-GB (2057); m/d/Y under en-US (1033,
            # COSAT) — whose source parsed exactly ['%m/%d/%Y', '%d/%m/%Y'].
            t_formats = (
                ("%m/%d/%Y", "%d/%m/%Y") if cfg.lcid == "1033" else ("%d/%m/%Y",)
            )
            t_start = _to_mdy(
                _field(
                    d1,
                    './/small[contains(@class, "media__subheading")]'
                    '//span[@class="nav-link"]//span[@class="nav-link__value"]/time[1]/text()',
                ),
                t_formats,
            )
            t_end = _to_mdy(
                _field(
                    d1,
                    './/small[contains(@class, "media__subheading")]'
                    '//span[@class="nav-link"]//span[@class="nav-link__value"]/time[2]/text()',
                ),
                t_formats,
            ) or t_start

            # normalize-space of the whole span (XPath takes the first matched
            # node), not ``/text()``: the location renders as
            # ``Org | <flag img> City, Country`` and the flag ``<img>`` splits
            # the text into two nodes — the first alone is just ``"Org | "``,
            # which silently blanked city/country on every dynamic-country
            # site. The source's parse_field read the full node text.
            city, country = _split_location(
                _field(
                    d1,
                    'normalize-space(.//small[@class="media__subheading"]'
                    '//span[@class="nav-link"]/span[@class="nav-link__value"])',
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


def _is_team_match_url(url):
    """Whether a supplied TournamentSoftware URL points at a team-match page."""
    return urlparse(url or "").path.lower().endswith("/sport/teammatch.aspx")


def _is_team_match_listing_url(url):
    """Whether a TournamentSoftware URL may list team-match page links."""
    path = urlparse(url or "").path.lower()
    return path.endswith(
        (
            "/sport/draw.aspx",
            "/sport/draws.aspx",
            "/sport/legacymatches.aspx",
        )
    )


def _normalize_ts_anchor_url(cfg, page_url, href):
    """Normalize TournamentSoftware sport-page anchors to absolute URLs."""
    href = (href or "").strip()
    if not href:
        return ""
    lower = href.lower()
    if lower.startswith(("teammatch.aspx", "draw.aspx", "draws.aspx", "legacymatches.aspx")):
        href = "/sport/" + href
    elif lower.startswith(("./teammatch.aspx", "./draw.aspx", "./draws.aspx", "./legacymatches.aspx")):
        href = "/sport/" + href[2:]
    elif lower.startswith("sport/"):
        href = "/" + href
    return urldefrag(urljoin(page_url or (cfg.base + "/"), href))[0]


def _url_dedupe_key(url):
    """Stable key for URL de-duplication while preserving the first URL seen."""
    parts = urlparse(url or "")
    return (
        parts.scheme.lower(),
        parts.netloc.lower(),
        parts.path.rstrip("/").lower(),
        tuple(sorted(parse_qsl(parts.query, keep_blank_values=True))),
    )


def _same_team_match_scope(match_url, cfg, tournament):
    """Whether a discovered team-match URL belongs to this host/tournament."""
    parts = urlparse(match_url or "")
    base_parts = urlparse(cfg.base or "")
    if parts.netloc and base_parts.netloc and parts.netloc.lower() != base_parts.netloc.lower():
        return False
    tournament_id = (tournament.get("tournament_id") or "").strip().lower()
    if not tournament_id:
        return True
    match_tournament_id = parse_qs(parts.query).get("id", [""])[0].strip().lower()
    return bool(match_tournament_id) and match_tournament_id == tournament_id


def _discover_team_match_items(client, cfg, tournament):
    """Return ``[(match_url, ctx)]`` for team matches linked by one tournament."""
    tournament_url = tournament.get("tournament_url", "")
    if not tournament_url:
        return []
    sel = client.get_selector(tournament_url)
    if sel is None:
        return []

    ctx = {
        "tournament_name": tournament.get("tournament_name", ""),
        "tournament_url": tournament_url,
        "tournament_start_date": tournament.get("tournament_start_date", ""),
        "tournament_end_date": tournament.get("tournament_end_date", ""),
        "tournament_city": tournament.get("tournament_city", ""),
        "tournament_country": tournament.get("tournament_country", ""),
    }
    items = []
    seen_matches = set()
    seen_listing_pages = set()
    listing_urls = []

    def add_match(href, page_url):
        match_url = _normalize_ts_anchor_url(cfg, page_url, href)
        if not _is_team_match_url(match_url):
            return
        if not _same_team_match_scope(match_url, cfg, tournament):
            return
        key = _url_dedupe_key(match_url)
        if key in seen_matches:
            return
        seen_matches.add(key)
        items.append((match_url, ctx))

    def add_listing_page(href, page_url):
        listing_url = _normalize_ts_anchor_url(cfg, page_url, href)
        if not _is_team_match_listing_url(listing_url):
            return
        key = _url_dedupe_key(listing_url)
        if key in seen_listing_pages:
            return
        seen_listing_pages.add(key)
        listing_urls.append(listing_url)

    for href in sel.xpath("//a/@href").getall():
        add_match(href, tournament_url)
        add_listing_page(href, tournament_url)

    for listing_url in listing_urls:
        listing_sel = client.get_selector(listing_url)
        if listing_sel is None:
            continue
        for href in listing_sel.xpath("//a/@href").getall():
            add_match(href, listing_url)
    return items


def _dedupe_team_match_items(items):
    """De-dupe team-match work across all discovered tournaments."""
    deduped = []
    seen = set()
    for match_url, ctx in items:
        key = _url_dedupe_key(match_url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((match_url, ctx))
    return deduped


def _filter_tournaments(cfg, tournaments, log):
    """Apply source-specific tournament exclusions after discovery."""
    terms = tuple(term.lower() for term in (cfg.exclude_name_terms or ()) if term)
    if not terms:
        return tournaments
    kept = []
    skipped = 0
    for tournament in tournaments:
        name = (tournament.get("tournament_name") or "").lower()
        if any(term in name for term in terms):
            skipped += 1
            continue
        kept.append(tournament)
    if skipped:
        log("INFO", f"Skipped {skipped} excluded tournament(s)")
    return kept


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


def _ranking_rows(sel, cfg):
    """Yield ``(profile_guid, dob)`` pairs from one ranking listing page.

    Default (Tennis Europe): player link in ``td[4]`` (href
    ``../profile/default.aspx?id=<GUID>``, split on ``?id=`` and lowercased),
    year of birth in ``td[5]`` → ``"1/1/YOB"``. With
    ``cfg.ranking_dob_full_date`` (COSAT): profile link in ``td[5]``
    (``/player-profile/<GUID>`` tail, lowercased) and a full DOB in ``td[6]``
    → ``MM/DD/YYYY``.
    """
    for row in sel.xpath(
        '//div[@id="content"]//table[@class="ruler"]//tr[td[@class="rank"]]'
    ):
        if cfg.ranking_dob_full_date:
            href = row.xpath(".//td[5]/a/@href").get() or ""
            if "/player-profile/" not in href:
                continue
            guid = href.rstrip("/").split("/")[-1].strip().lower()
            raw = (row.xpath("string(.//td[6])").get() or "")
            raw = raw.replace("\xa0", " ").strip()
            # Rendered m/d/Y under the en-US cookiewall locale (lcid 1033) —
            # the source's exact ``in_format='%m/%d/%Y'`` registry parse.
            dob = _to_mdy(raw, ("%m/%d/%Y",))
            if guid and dob:
                yield guid, dob
            continue
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

    Two site layouts are handled:

    * **Tennis Europe** (``ranking_dob`` only): the ``{base}/ranking/`` index
      lists rankings only, and the full ``category.aspx`` listings are now
      client-side rendered (the server-side ``table.ruler`` is an empty shell —
      zero profile links — so plain HTTP can't read them). Each ranking's
      overview page (``ranking.aspx?rid=…``) still carries the per-category
      ``More`` links, and the server-rendered top of every category is reachable
      at ``ranking.aspx?id=<pub>&category=<cat>&ps=100`` (same rows, the preview
      page — it ignores ``&p=N`` paging, so coverage is the ranked top of each
      age category across every published ranking; unranked players keep a blank
      DOB, exactly as before). Those per-category listing URLs are handed back in
      ``page_urls`` for the caller to fetch + merge concurrently.
    * **COSAT** (``ranking_dob_full_date``): the ``More`` links sit on the
      ``/ranking/`` index itself and the ``category.aspx`` listings stay
      server-rendered and paginated (``&ps=100`` + ``&p=2..N`` from the
      ``page_caption`` result count / 100). First pages are parsed here; the
      rest are returned in ``page_urls`` for concurrent fetching.

    Returns ``(dob_map, page_urls)``.
    """
    index = cfg.base + "/ranking/"
    more_xpath = (
        '//div[@id="content"]//table[@class="ruler"]//tr/th'
        '/a[normalize-space(.)="More"]/@href'
    )
    dob_map, page_urls = {}, []
    sel = client.get_selector(index)
    if sel is None:
        return dob_map, page_urls

    if not cfg.ranking_dob_full_date:
        # Tennis Europe: enumerate every ranking, then read each ranking's
        # per-category "More" links off its overview page and rewrite them to
        # the server-rendered ranking.aspx preview (category.aspx is JS-only).
        rid_hrefs = sel.xpath(
            '//div[@id="content"]//table[@class="ruler"]//td/h5/a/@href'
        ).getall()
        seen = set()
        for rid_href in rid_hrefs:
            rid_sel = client.get_selector(urljoin(index, rid_href))
            if rid_sel is None:
                continue
            for more in rid_sel.xpath(more_xpath).getall():
                listing = more.strip().replace("category.aspx", "ranking.aspx")
                listing = urljoin(
                    index, listing + ("&" if "?" in listing else "?") + "ps=100"
                )
                if listing not in seen:
                    seen.add(listing)
                    page_urls.append(listing)
        log(
            "INFO",
            f"\U0001f4c7 Ranking tab: {len(rid_hrefs)} ranking(s), "
            f"{len(page_urls)} category listing(s) to fetch",
        )
        return dob_map, page_urls

    # COSAT (ranking_dob_full_date): More links on the index; server-rendered
    # paginated category.aspx listings.
    mores = sel.xpath(more_xpath).getall()
    if not mores:
        cat_href = _field(
            sel, '//div[@id="content"]//table[@class="ruler"]//tr[1]/td/h5/a/@href'
        )
        if not cat_href:
            return dob_map, page_urls
        cat_sel = client.get_selector(urljoin(index, cat_href))
        if cat_sel is None:
            return dob_map, page_urls
        mores = cat_sel.xpath(more_xpath).getall()
    for more in mores:
        page_url = urljoin(index, more.strip() + "&ps=100")
        first = client.get_selector(page_url)
        if first is None:
            continue
        dob_map.update(_ranking_rows(first, cfg))
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


def _guid_from_profile_url(url):
    """The tournamentsoftware player GUID embedded in a profile URL, or ``""``.

    Handles the ``/player-profile/<GUID>`` path form and the legacy
    ``.../profile/default.aspx?id=<GUID>`` query form. The ``id`` query is only
    honoured on a *profile* path, so a tournament-scoped
    ``/sport/player.aspx?id=<TOURNAMENT_GUID>`` URL can never leak the tournament
    id through here. GUID case is preserved as passed.
    """
    if not url:
        return ""
    parts = urlparse(url)
    path = parts.path.rstrip("/")
    if "/player-profile/" in path:
        return path.rsplit("/player-profile/", 1)[-1].split("/")[0].strip()
    if "profile" in path.lower():
        return parse_qs(parts.query).get("id", [""])[0].strip()
    return ""


def _profile_href_from_url(url):
    guid = _guid_from_profile_url(url)
    return f"/player-profile/{guid}" if guid else ""


def _parse_player(
    client, cfg, name, url, dob_map=None, player_enrichment=None
):
    """Resolve a player's ``(name, third_party_id, dob, gender, country)``.

    Gender comes from an exact-ID enrichment registry when supplied; otherwise
    it is filled in by :func:`_build_row` from the draw name or Claude. The name
    is cleaned of seedings then reordered to ``"Lastname, Firstname"`` via
    :func:`._names.last_first`. ``country`` is the player's nationality (from
    the profile flag) for dynamic-country sites, else ``""`` — fixed-country
    sites fill the per-player country from the federation constant in
    :func:`_build_row`.
    """
    name = last_first(name)
    if not (name and url):
        return name, "", "", "", ""
    sel = client.get_selector(url)
    if sel is None:
        return name, "", "", "", ""

    country = _nat(sel) if cfg.dynamic_country else ""

    # The subhead's player-profile link (``/player-profile/<GUID>``) is the
    # genuine tournamentsoftware player id and the same href the ranking_dob
    # join reads below. The crawl reaches players via tournament-scoped
    # ``/sport/player.aspx?id=<TOURNAMENT_GUID>`` URLs, so the id must come from
    # this href, never from the requested ``url``.
    profile_href = _field(
        sel,
        '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
        '//h4[contains(@class, "media__title")]/a/@href',
    )
    if not profile_href:
        profile_href = _profile_href_from_url(url)
    if not profile_href:
        # Legacy /sport/player.aspx pages expose the canonical modern profile as
        # a button rather than the page-subhead link used by modern match cards.
        profile_href = _field(
            sel,
            '//a[contains(@href, "/player-profile/") '
            'and not(contains(substring-after(@href, "/player-profile/"), "/"))]/@href',
        )

    if cfg.guid_third_party_id:
        # These sites (Tennis Europe, COSAT) show a member-id ``media__title-aside``
        # (a federation id, not this id); use the profile GUID instead, in the
        # site's own canonical case (Tennis Europe upper-cases, COSAT lower-cases).
        guid = _guid_from_profile_url(profile_href)
        third_party_id = guid.upper() if cfg.guid_third_party_id_upper else guid
    else:
        third_party_id = _field(
            sel,
            '//div[contains(@class, "page-subhead")]//div[@class="media__content"]'
            '//h4[contains(@class, "media__title")]/span[@class="media__title-aside"]/text()',
        )
        third_party_id = _RE_PARENS.sub("", third_party_id).strip()

    enrichment = (player_enrichment or {}).get(third_party_id, {})
    dob = enrichment.get("dob", "")
    gender = enrichment.get("gender", "")

    if not dob and profile_href and cfg.biography_dob:
        # Biography-tab DOB mode: the "Year of birth" on
        # ``/player-profile/<GUID>/biography`` (→ ``1/1/YYYY``), present for
        # ranked and unranked players alike. Cached per run by profile GUID
        # (``dob_map``) so each unique player's biography — including a blank
        # "" negative result — is fetched at most once despite players recurring
        # across many matches.
        guid = _guid_from_profile_url(profile_href).lower()
        cache = dob_map if dob_map is not None else {}
        if guid and guid in cache:
            dob = cache[guid]
        else:
            bio_url = urljoin(cfg.base + "/", profile_href).rstrip("/") + "/biography"
            bio_sel = client.get_selector(bio_url)
            if bio_sel is not None:
                dob = _parse_birth_year(bio_sel)
                if cfg.dynamic_country and not country:
                    country = _nat(bio_sel)
                # Cache only a real fetch (incl. a genuine blank YOB). A failed
                # fetch (bio_sel is None, after the client's retry budget) stays
                # uncached so a later match for this player can retry, rather than
                # permanently blanking their DOB from one transient failure.
                if guid:
                    cache[guid] = dob
    elif not dob and profile_href and cfg.ranking_dob:
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
    elif not dob and profile_href:
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
    return name, third_party_id, dob, gender, country


def _build_row(client, cfg, ctx, match_data):
    """Assemble one full items row from a parsed match + player lookups."""
    w1 = match_data.get("winner_1", {})
    w2 = match_data.get("winner_2", {})
    l1 = match_data.get("loser_1", {})
    l2 = match_data.get("loser_2", {})

    dob_map = ctx.get("dob_map")
    player_enrichment = ctx.get("player_enrichment")
    w1_name, w1_id, w1_dob, w1_g, w1_c = _parse_player(
        client,
        cfg,
        w1.get("name", ""),
        w1.get("profile_url", ""),
        dob_map,
        player_enrichment,
    )
    w2_name, w2_id, w2_dob, w2_g, w2_c = _parse_player(
        client,
        cfg,
        w2.get("name", ""),
        w2.get("profile_url", ""),
        dob_map,
        player_enrichment,
    )
    l1_name, l1_id, l1_dob, l1_g, l1_c = _parse_player(
        client,
        cfg,
        l1.get("name", ""),
        l1.get("profile_url", ""),
        dob_map,
        player_enrichment,
    )
    l2_name, l2_id, l2_dob, l2_g, l2_c = _parse_player(
        client,
        cfg,
        l2.get("name", ""),
        l2.get("profile_url", ""),
        dob_map,
        player_enrichment,
    )

    draw_name = ctx.get("draw_name", "")
    gcode = draw_gender_code(draw_name)
    mixed_draw = is_mixed_draw(draw_name)
    if not gcode and not mixed_draw:
        tournament_name = ctx.get("tournament_name", "")
        gcode = draw_gender_code(tournament_name)
        mixed_draw = is_mixed_draw(tournament_name)
    claude_keys = ctx.get("claude_keys")
    if cfg.claude_gender and claude_keys:
        # The draw name here doesn't reliably carry a gender word, so infer each
        # player's gender from their name via Claude (cached per distinct name).
        w1_g = w1_g or (resolve_gender(client, claude_keys, w1_name) if w1_name else "")
        w2_g = w2_g or (resolve_gender(client, claude_keys, w2_name) if w2_name else "")
        l1_g = l1_g or (resolve_gender(client, claude_keys, l1_name) if l1_name else "")
        l2_g = l2_g or (resolve_gender(client, claude_keys, l2_name) if l2_name else "")
        # Draw-level gender: an explicit draw-name gender wins; a genuinely mixed
        # draw is labelled Mixed; otherwise fall back to the winner's inference.
        if gcode:
            draw_gender = "Male" if gcode == "M" else "Female"
        elif mixed_draw:
            draw_gender = "Mixed"
        else:
            draw_gender = "Male" if w1_g == "M" else ("Female" if w1_g == "F" else "")
    else:
        # Default: gender is carried by the draw name (e.g. "Boys Singles" /
        # "Juniorke pojedinačno"); every player in the match inherits it.
        w1_g = (w1_g or gcode) if w1_name else ""
        w2_g = (w2_g or gcode) if w2_name else ""
        l1_g = (l1_g or gcode) if l1_name else ""
        l2_g = (l2_g or gcode) if l2_name else ""
        if gcode:
            draw_gender = "Male" if gcode == "M" else "Female"
        elif mixed_draw:
            draw_gender = "Mixed"
        else:
            draw_gender = ""
        if cfg.claude_gender_fallback and claude_keys and not gcode:
            # Ireland has club/box/group draws where only player names can supply
            # gender. Mixed draws get player genders plus a Mixed draw label.
            w1_g = w1_g or (resolve_gender(client, claude_keys, w1_name) if w1_name else "")
            w2_g = w2_g or (resolve_gender(client, claude_keys, w2_name) if w2_name else "")
            l1_g = l1_g or (resolve_gender(client, claude_keys, l1_name) if l1_name else "")
            l2_g = l2_g or (resolve_gender(client, claude_keys, l2_name) if l2_name else "")
            known = {g for g in (w1_g, w2_g, l1_g, l2_g) if g}
            if not mixed_draw and len(known) == 1:
                draw_gender = "Male" if "M" in known else "Female"
            elif not mixed_draw and len(known) > 1:
                draw_gender = "Mixed"
    date = ctx.get("match_date", "") or ctx.get("tournament_start_date", "")

    if cfg.dynamic_country:
        # Country is per-tournament (from the search location) and per-player
        # (from the profile flag); the org labels are fixed.
        t_country = ctx.get("tournament_country", "")
        if cfg.claude_country:
            # GLTA's source: known-codes table first, Claude for the rest
            # (cached per run) — see _country_codes.resolve_country_code.
            t_country_code = (
                resolve_country_code(client, ctx.get("claude_keys") or [], t_country)
                if t_country
                else ""
            )
        else:
            # COSAT's source: the first three letters of the country name.
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
        import_source = cfg.import_source or cfg.country
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


def _normalize_claude_fallback_genders(rows):
    """Normalize fallback-inferred genders using the whole draw group.

    Ireland-style fallback draws are often generic groups, so row-by-row name
    inference can leave a player blank or make one row disagree with the rest of
    the group. For non-explicit-mixed, non-deterministic groups, use the unique
    player gender evidence across the group before writing the CSV.
    """
    player_fields = (
        ("winner_1_name", "winner_1_gender"),
        ("winner_2_name", "winner_2_gender"),
        ("loser_1_name", "loser_1_gender"),
        ("loser_2_name", "loser_2_gender"),
    )
    groups = {}
    for row in rows:
        draw_name = row.get("draw_name", "")
        tournament_name = row.get("tournament_name", "")
        gcode = draw_gender_code(draw_name)
        mixed_draw = is_mixed_draw(draw_name)
        if not gcode and not mixed_draw:
            gcode = draw_gender_code(tournament_name)
            mixed_draw = is_mixed_draw(tournament_name)
        if gcode or mixed_draw:
            continue
        key = (
            row.get("tournament_url", ""),
            tournament_name,
            draw_name,
            row.get("draw_team_type", ""),
        )
        groups.setdefault(key, []).append(row)

    changed = 0
    for group_rows in groups.values():
        by_player = {}
        for row in group_rows:
            for name_field, gender_field in player_fields:
                name = (row.get(name_field) or "").strip()
                gender = (row.get(gender_field) or "").strip()
                if name and gender in {"M", "F"}:
                    by_player.setdefault(name, set()).add(gender)
        counts = {"M": 0, "F": 0}
        for genders in by_player.values():
            if len(genders) == 1:
                counts[next(iter(genders))] += 1
        code = ""
        if counts["M"] and not counts["F"]:
            code = "M"
        elif counts["F"] and not counts["M"]:
            code = "F"
        elif counts["M"] or counts["F"]:
            majority = "M" if counts["M"] > counts["F"] else "F"
            minority = "F" if majority == "M" else "M"
            if counts[majority] >= counts[minority] + 2:
                code = majority

        if not code:
            continue
        draw_gender = "Male" if code == "M" else "Female"
        for row in group_rows:
            if row.get("draw_gender") != draw_gender:
                row["draw_gender"] = draw_gender
                changed += 1
            for name_field, gender_field in player_fields:
                if row.get(name_field) and row.get(gender_field) != code:
                    row[gender_field] = code
                    changed += 1
    return changed


def _parse_player_matches(client, cfg, ctx, player_url):
    """Fetch one player's profile and return parsed rows for their matches."""
    sel = client.get_selector(player_url)
    if sel is None:
        return []

    rows = []
    for d1 in sel.xpath(
        '//div[@class="module-container"]/ul/li[@class="match-group__item"]'
        '/div[contains(concat(" ", normalize-space(@class), " "), " match ")]'
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


def _legacy_side_players(cell, match_url):
    players = []
    for a in cell.xpath('.//a[contains(@href, "player.aspx")]'):
        name = a.xpath("normalize-space(.)").get()
        href = a.xpath("./@href").get()
        if name and href:
            players.append(
                {
                    "name": _clean_name(name),
                    "profile_url": urljoin(match_url, href.strip()),
                }
            )
    return players


def _legacy_winner_side(left_cell, right_cell, point_cell):
    if left_cell.css("strong a"):
        return 0
    if right_cell.css("strong a"):
        return 1
    point_text = point_cell.xpath("normalize-space(.)").get() if point_cell else ""
    match = re.search(r"(\d+)\s*-\s*(\d+)", point_text or "")
    if match:
        left_points, right_points = int(match.group(1)), int(match.group(2))
        if left_points != right_points:
            return 0 if left_points > right_points else 1
    return None


def _legacy_score(scores, winner_side):
    ordered = []
    for score in scores:
        score = (score or "").strip()
        match = re.match(r"^(\d+)\s*-\s*(\d+)(.*)$", score)
        if match and winner_side == 1:
            ordered.append(f"{match.group(2)}-{match.group(1)}{match.group(3)}")
        elif score:
            ordered.append(score)
    return ", ".join(ordered) + ";" if ordered else ""


def _parse_legacy_team_match_page(client, cfg, ctx, match_url, sel):
    """Parse legacy TournamentSoftware team-match tables.

    Tennis Europe team events still render ``/sport/teammatch.aspx`` with the
    old ``table.ruler.matches`` layout. Each direct row is one rubber; nested
    tables inside the side cells contain the players.
    """
    title = _field(sel, "normalize-space(//title)")
    title_parts = [part.strip() for part in title.split(" - ") if part.strip()]
    title_name = " - ".join(title_parts[1:-1]) if len(title_parts) > 2 else ""

    time_text = _field(
        sel,
        'normalize-space(//div[@id="content"]//th[normalize-space(.)="Time:"]'
        "/following-sibling::td[1])",
    )
    match_date = ""
    date_match = _RE_DMY.search(time_text)
    if date_match:
        match_date = _to_mdy(date_match.group(), ("%d/%m/%Y", "%m/%d/%Y"))

    draw_name = _field(
        sel,
        'normalize-space(//div[@id="content"]//th[normalize-space(.)="Draw:"]'
        "/following-sibling::td[1])",
    )
    match_ctx = dict(ctx)
    match_ctx.update(
        {
            "tournament_name": title_name or ctx.get("tournament_name") or cfg.label,
            "tournament_url": ctx.get("tournament_url") or match_url,
            "match_date": match_date,
            "draw_name": draw_name,
        }
    )

    rows = []
    for row in sel.css("table.ruler.matches > tbody > tr"):
        cells = row.xpath("./td")
        if len(cells) < 6:
            continue
        left_cell = cells[2]
        right_cell = cells[4]
        score_cell = cells[5]
        point_cell = cells[6] if len(cells) > 6 else None
        left_players = _legacy_side_players(left_cell, match_url)
        right_players = _legacy_side_players(right_cell, match_url)
        if not (left_players and right_players):
            continue
        winner_side = _legacy_winner_side(left_cell, right_cell, point_cell)
        if winner_side is None:
            continue
        score = _legacy_score(score_cell.css("span.score span::text").getall(), winner_side)
        if not score:
            continue
        event_name = cells[1].xpath("normalize-space(.)").get() or ""
        winners = left_players if winner_side == 0 else right_players
        losers = right_players if winner_side == 0 else left_players
        rows.append(
            _build_row(
                client,
                cfg,
                match_ctx,
                {
                    "draw_team_type": (
                        "Doubles"
                        if len(winners) == 2 or "D" in event_name.upper()
                        else "Singles"
                    ),
                    "outcome": (
                        "Retired"
                        if "retired" in (row.xpath("normalize-space(.)").get() or "").lower()
                        else "Completed"
                    ),
                    "score": score,
                    "winner_1": winners[0] if len(winners) > 0 else {},
                    "winner_2": winners[1] if len(winners) > 1 else {},
                    "loser_1": losers[0] if len(losers) > 0 else {},
                    "loser_2": losers[1] if len(losers) > 1 else {},
                },
            )
        )
    return rows


def _parse_team_match_page(client, cfg, ctx, match_url):
    """Fetch one TournamentSoftware team-match page and return parsed rows.

    Some Tennis Europe team competitions expose match results only through
    ``/sport/teammatch.aspx?id=...&match=...``. Those pages use the same match
    card markup as the league engine, so parse them directly when a user supplies
    that URL instead of trying to resolve it as an individual tournament page.
    """
    sel = client.get_selector(match_url)
    if sel is None:
        return []

    raw_round = _field(
        sel,
        '(//div[@id="js-league-team-match-index"]'
        '//div[contains(@class, "team-match-header")]//div[@class="module-container"]'
        '//div[contains(@class, "text--center")]//time)[1]/parent::node()/text()[1]',
    )
    match_round = raw_round.replace("•", "").strip()

    match_date = ""
    raw_date = _field(
        sel,
        '(//div[@id="js-league-team-match-index"]'
        '//div[contains(@class, "team-match-header")]//div[@class="module-container"]'
        '//div[contains(@class, "text--center")]//time)[1]/@datetime',
    )
    if raw_date:
        try:
            match_date = datetime.strptime(raw_date, "%Y-%m-%d %H:%M").strftime(
                "%m/%d/%Y"
            )
        except ValueError:
            match_date = ""

    draw_name = _field(
        sel,
        '//div[@id="js-league-team-match-index"]'
        '//div[contains(@class, "team-match-header")]//div[@class="module-container"]'
        '//div[contains(@class, "text--center")]/a[@class="nav-link"]'
        '/span[@class="nav-link__value"]/text()',
    )
    tournament_name = ctx.get("tournament_name") or _field(
        sel,
        '//div[contains(@class, "page-head")]//div[@class="media__content"]'
        '//h2[contains(@class, "media__title")]//span[contains(@class, "nav-link")]'
        '/span[@class="nav-link__value"]/text()',
    )
    tournament_city = ctx.get("tournament_city", "")
    tournament_country = ctx.get("tournament_country", "")
    if not tournament_country:
        for subheading in sel.xpath(
            '//div[@class="media__content"]//small[contains(@class, "media__subheading")]'
            '//span[@class="nav-link"]//span[@class="nav-link__value"]'
        ):
            text = _field(subheading, "normalize-space(.)")
            if "|" in text:
                tournament_city, tournament_country = _split_location(text)
                break

    match_ctx = dict(ctx)
    match_ctx.update(
        {
            "tournament_name": tournament_name or cfg.label,
            "tournament_url": ctx.get("tournament_url") or match_url,
            "tournament_city": tournament_city,
            "tournament_country": tournament_country,
            "match_round": match_round,
            "match_date": match_date,
            "draw_name": draw_name,
        }
    )

    rows = []
    for d1 in sel.xpath(
        '//div[@class="module-container"]/ul/li[@class="match-group__item"]'
        '/div[contains(concat(" ", normalize-space(@class), " "), " match ")]'
    ):
        body = d1.xpath('.//div[@class="match__body"]').get()
        if not body:
            continue
        match_data = _parse_match(Selector(text=body), cfg)
        if match_data and match_data.get("score"):
            rows.append(_build_row(client, cfg, match_ctx, match_data))
    if rows:
        return rows
    return _parse_legacy_team_match_page(client, cfg, ctx, match_url, sel)


def _window(run_obj):
    """Resolve the ``(start, end)`` YYYY-MM-DD search window from the run."""
    today = timezone.localdate()
    start = run_obj.date_from or today
    end = run_obj.date_to or today
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def _date_in_window(value, start_date, end_date):
    """Whether a row date (MM/DD/YYYY) sits inside the requested YYYY-MM-DD window."""
    if not value:
        return True
    try:
        row_date = datetime.strptime(value, "%m/%d/%Y").date()
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return True
    return start <= row_date <= end


def _tournament_overlaps_window(tournament, start_date, end_date):
    """Whether a discovered tournament's date range overlaps the requested window."""
    t_start = tournament.get("tournament_start_date") or ""
    t_end = tournament.get("tournament_end_date") or t_start
    if not t_start:
        return True
    try:
        tournament_start = datetime.strptime(t_start, "%m/%d/%Y").date()
        tournament_end = datetime.strptime(t_end, "%m/%d/%Y").date()
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return True
    return tournament_start <= end and tournament_end >= start


def run(cfg, run_obj, log, *, player_enrichment_loader=None):
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
    direct_team_match_url = _is_team_match_url(tournament_url)

    if direct_team_match_url:
        log("INFO", f"\U0001f3be {cfg.label} starting \u2014 direct team-match URL")
    elif tournament_url:
        log("INFO", f"\U0001f3be {cfg.label} starting \u2014 single tournament URL")
    else:
        start_date, end_date = _window(run_obj)
        log("INFO", f"\U0001f3be {cfg.label} starting \u2014 {start_date} \u2192 {end_date}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    player_enrichment = {}
    if player_enrichment_loader is not None:
        log("INFO", "Loading player DOB/gender registry")
        try:
            player_enrichment = player_enrichment_loader(log, tele) or {}
        except Exception as exc:  # noqa: BLE001 - curated loader errors fail honestly
            msg = f"{cfg.label} player registry unavailable: {exc}"
            tele.record_error(msg)
            log("ERROR", msg)
            return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    proxies = build_proxies(scraper, log)
    claude_keys = (
        resolve_claude_keys(scraper)
        if (cfg.claude_gender or cfg.claude_gender_fallback or cfg.claude_country)
        else []
    )
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
    elif cfg.claude_gender_fallback:
        if claude_keys:
            log(
                "INFO",
                "\U0001f9e0 Gender: draw-name gender plus cached Claude fallback "
                "for genderless draws",
            )
        else:
            log(
                "WARN",
                "\u26a0\ufe0f claude_gender_fallback set but no Claude key configured "
                "\u2014 per-player gender will be blank for genderless draws",
            )
    if cfg.claude_country:
        if claude_keys:
            log(
                "INFO",
                "\U0001f30d Country codes: known-codes table + Claude for "
                "unlisted countries (cached per run)",
            )
        else:
            # Claude-backed country codes with no fallback: without a key,
            # fail the run and ask for one rather than emitting blank codes.
            msg = (
                f"Anthropic API key required \u2014 {cfg.label} resolves "
                "tournament country codes via Claude for countries not in "
                "its known-codes table and has no fallback. Add a key on "
                "the Settings page (workspace-wide) or this scraper's "
                "Settings tab, then re-run."
            )
            tele.record_error(msg)
            log("ERROR", "\U0001f6d1 " + msg)
            return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering tournaments \u2500\u2500\u2500\u2500")
    dob_map, ranking_pages = {}, []
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        _warmup(discovery, cfg)
        if direct_team_match_url:
            tournaments = [
                {"tournament_name": cfg.label, "tournament_url": tournament_url}
            ]
        elif tournament_url:
            tournaments = _discover_one(discovery, cfg, tournament_url, log)
        else:
            search_start = start_date
            if cfg.discovery_lookback_days > 0:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
                search_start = (start_dt - timedelta(days=cfg.discovery_lookback_days)).strftime(
                    "%Y-%m-%d"
                )
                log(
                    "INFO",
                    f"\U0001f50e Discovery lookback: searching {search_start} \u2192 {end_date} "
                    f"and keeping rows dated {start_date} \u2192 {end_date}",
                )
            tournaments = _discover_range(discovery, cfg, search_start, end_date, log)
            if cfg.discovery_lookback_days > 0:
                before = len(tournaments)
                tournaments = [
                    t for t in tournaments if _tournament_overlaps_window(t, start_date, end_date)
                ]
                skipped = before - len(tournaments)
                if skipped:
                    log(
                        "INFO",
                        f"\U0001f9f9 Skipped {skipped} non-overlapping tournament(s) "
                        "from the lookback discovery window",
                    )
        if not direct_team_match_url:
            tournaments = _filter_tournaments(cfg, tournaments, log)
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

    lock = threading.Lock()
    seen = set()
    output_rows = []

    def write_row(row):
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
                return False
            seen.add(key)
            output_rows.append(row)
        return True

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

    def list_team_matches(tournament):
        try:
            return _discover_team_match_items(client_for(), cfg, tournament)
        except Exception as exc:  # noqa: BLE001 - a bad tournament can't kill the run
            tele.record_error(
                redact_secrets(
                    f"Discover team matches {tournament.get('tournament_url', '')} failed: {exc}"
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
            pairs = list(_ranking_rows(sel, cfg))
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
        if cfg.ranking_dob or cfg.biography_dob:
            ctx = {**ctx, "dob_map": dob_map}
        if player_enrichment:
            ctx = {**ctx, "player_enrichment": player_enrichment}
        try:
            rows = _parse_player_matches(client_for(), cfg, ctx, player_url)
            for row in rows:
                if (
                    cfg.discovery_lookback_days > 0
                    and not tournament_url
                    and not _date_in_window(row.get("date", ""), start_date, end_date)
                ):
                    continue
                # Each match is reachable from both players' pages, so dedupe on
                # a content key without collapsing genuine rematches (same
                # players/score in a different draw, round or date).
                if not write_row(row):
                    continue
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

    def crawl_team_match(item):
        match_url, ctx = item
        if claude_keys:
            ctx = {**ctx, "claude_keys": claude_keys}
        if cfg.ranking_dob or cfg.biography_dob:
            ctx = {**ctx, "dob_map": dob_map}
        if player_enrichment:
            ctx = {**ctx, "player_enrichment": player_enrichment}
        try:
            rows = _parse_team_match_page(client_for(), cfg, ctx, match_url)
            for row in rows:
                if (
                    not direct_team_match_url
                    and not tournament_url
                    and not _date_in_window(row.get("date", ""), start_date, end_date)
                ):
                    continue
                if not write_row(row):
                    continue
                log(
                    "INFO",
                    f"   \U0001f3c6 {row.get('draw_team_type', '')}: "
                    f"{row.get('winner_1_name') or '?'} def. "
                    f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                    f"@ {row.get('tournament_name') or cfg.label}",
                )
        except Exception as exc:  # noqa: BLE001 - a bad page can't kill the run
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
        if direct_team_match_url:
            work = [(tournament_url, {"tournament_url": tournament_url})]
            Run.objects.filter(pk=run_obj.pk).update(
                progress_total=len(work), progress_done=0
            )
            log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping team-match \u2500\u2500\u2500\u2500")
            crawl_team_match(work[0])
        elif tournaments:
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
                team_work = []
                if cfg.discover_team_matches:
                    log(
                        "INFO",
                        "──── phase 2a · mapping team-matches ────",
                    )
                    nested_team = executor.map(list_team_matches, tournaments)
                    team_work = _dedupe_team_match_items(
                        item for sub in nested_team for item in sub
                    )
                    log("INFO", f"🗺️ {len(team_work)} team-match(es) to scrape")

                nested = executor.map(list_one, tournaments)
                work = [item for sub in nested for item in sub]
                Run.objects.filter(pk=run_obj.pk).update(
                    progress_total=len(work) + len(team_work), progress_done=0
                )
                log("INFO", f"\U0001f5fa\ufe0f {len(work)} entrant(s) to scrape")

                # ---- phase 3 · scrape each entrant's matches concurrently ----
                if work:
                    log(
                        "INFO",
                        "\u2500\u2500\u2500\u2500 phase 3 \u00b7 scraping matches \u2500\u2500\u2500\u2500",
                    )
                    list(executor.map(crawl_one, work))
                if team_work:
                    log(
                        "INFO",
                        "──── phase 3b · scraping team-matches ────",
                    )
                    list(executor.map(crawl_team_match, team_work))
    finally:
        for cli in clients:
            cli.close()

    if cfg.claude_gender_fallback and output_rows:
        changed = _normalize_claude_fallback_genders(output_rows)
        if changed:
            log(
                "INFO",
                f"Gender fallback normalized {changed} field(s) by draw group",
            )

    row_count = len(output_rows)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    for row in output_rows:
        writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
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
