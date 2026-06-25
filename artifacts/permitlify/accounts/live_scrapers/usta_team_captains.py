"""USTA Team Captains (TennisLink Leagues) scraper.

Ports the production ``usta_team_captains`` spider onto MatchMiner's shared HTTP
client (:mod:`accounts.live_scrapers._http`) + telemetry. USTA TennisLink is a
classic **ASP.NET WebForms** site: every drill-down is a ``__doPostBack`` that
re-POSTs the page back to itself carrying the ``__VIEWSTATE`` / CSRF hidden
fields, so this scraper harvests those hidden fields with parsel and replays the
postbacks via :meth:`ScraperClient.post`.

Flow (over ``tennislink.usta.com`` + ``account.usta.com``):

1. **Login.** ``GET .../Dashboard/Main/Login.aspx?returnURL=.../Leagues`` follows
   the OIDC redirect to ``account.usta.com/u/login`` and reads the ``state`` from
   the final URL (with a hidden-field fallback). Then ``POST
   account.usta.com/u/login`` with ``username`` + ``password`` + ``state``;
   success is confirmed by the redirect chain landing on
   ``.../Leagues/Common/Home.aspx`` (with an explicit Home.aspx fetch as a
   backstop).
2. **NTRP discovery.** ``GET .../StatsAndStandings.aspx?SearchType=3`` yields the
   ASP.NET hidden fields (``__VIEWSTATE`` / ``__VIEWSTATEGENERATOR`` / CSRF /
   ``HistoryPointParam`` / ``ddlCYear``) plus the available NTRP levels and
   genders. Every NTRP x Gender pairing becomes one search task.
3. **Per combo (concurrently):** POST the team search, then for each team walk
   the ``__doPostBack`` chain team -> Player Roster -> captain profile, pulling
   the team metadata + the captain's name and the captain's City/State/NTRP.

**Threading.** ``curl_cffi`` sessions are not thread-safe, so the discovery
session's authenticated cookies are captured once and re-injected into each
worker thread's own :class:`ScraperClient` (mirroring the source, which shared
one logged-in session's cookies across all worker threads).

**Deterministic / AI-free port.** The original ran every captain name through an
Anthropic call (``helper.format_name_gender_claude``) that returned
``{"formatted": "Last, Firstname", "gender": "M/F"}`` purely to tidy the name and
guess a gender. The output schema has **no gender column**, so the gender is
dropped entirely, and the AI name-formatting is replaced by a small
deterministic helper (:func:`_format_name`): a name that already contains a comma
is kept as-is, otherwise the whitespace-split name is rendered ``"Last, First"``
with the final token treated as the surname. The per-captain DB de-dup the source
used around that AI call is dropped too (no persistence here).

Credentials come from ``settings.USTA_USERNAME`` / ``settings.USTA_PASSWORD``
(env vars). They are **never** hard-coded or logged; when unset the run fails
honestly. The championship year is ``run_obj.params["year"]`` (falling back to
``date_from.year`` then the current year) and is passed through wherever the
source used ``rank_year``.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from parsel import Selector

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

# --- endpoints (public, not secrets) ---------------------------------------
LOGIN_PAGE_URL = (
    "https://tennislink.usta.com/Dashboard/Main/Login.aspx"
    "?returnURL=https://tennislink.usta.com/Leagues"
)
ACCOUNT_LOGIN_URL = "https://account.usta.com/u/login"
HOME_URL = "https://tennislink.usta.com/Leagues/Common/Home.aspx"
SEARCH_URL = "https://tennislink.usta.com/Leagues/Main/StatsAndStandings.aspx?SearchType=3"

# OIDC + WebForms redirect chains are deeper than the client's default of 5.
MAX_REDIRECTS = 12

# Items CSV columns — the source's bespoke "finalize" fields (already Title Case),
# NOT the 61-column match schema. Order is significant and HEADER == COLUMNS.
COLUMNS = [
    "Ntrp Text",
    "Gender Text",
    "Team Name",
    "Season Start",
    "No. Players",
    "Usta Section",
    "Usta District",
    "Local League League Type",
    "Team Ntrp Gender",
    "Flight Sub-Flight Name",
    "Facility Name",
    "Facility Address",
    "Captain Name",
    "Captain City State",
    "Captain Ntrp",
]
HEADER = list(COLUMNS)

# Headers for the WebForms postbacks (X-MicrosoftAjax delta requests).
_AJAX_HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-MicrosoftAjax": "Delta=true",
    "X-Requested-With": "XMLHttpRequest",
}


# ---------------------------------------------------------------------------
# Small deterministic helpers (ports of fctcore.parse_field / parse_target /
# Utils.get_payload_params, plus the AI-free name formatter)
# ---------------------------------------------------------------------------
def _parse_field(query, node):
    """Return ``normalize-space(query)`` against ``node`` (a parsel selector), or ''."""
    try:
        return node.xpath(f"normalize-space({query})").get() or ""
    except Exception:  # noqa: BLE001 - a bad xpath/node is non-fatal
        return ""


def _parse_target(link):
    """Extract the ``__doPostBack('TARGET', ...)`` event target from an href."""
    try:
        m = re.search(r"__doPostBack\('([^']+)'", link or "")
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001 - malformed href is non-fatal
        return ""


def _format_name(raw):
    """Deterministic, AI-free replacement for the captain name formatter.

    The source asked Claude to produce ``"Last, Firstname"``. Here: a name that
    already has a comma is kept as-is; otherwise the whitespace-split name is
    rendered ``"Last, First"`` with the final token treated as the surname. A
    single-token (or empty) name is returned unchanged.
    """
    name = (raw or "").strip()
    if not name or "," in name:
        return name
    parts = name.split()
    if len(parts) < 2:
        return name
    last = parts[-1]
    first = " ".join(parts[:-1])
    return f"{last}, {first}"


def _get_payload_params(text):
    """Harvest the ASP.NET hidden fields needed to replay a postback.

    Works on both full HTML pages (hidden ``<input>`` fields) and AJAX delta
    responses (where ``__VIEWSTATE`` arrives in the pipe-delimited body), mirroring
    the source ``Utils.get_payload_params``.
    """
    params = {
        "__VIEWSTATE": "",
        "__VIEWSTATEGENERATOR": "",
        "CSRFToken": "",
        "HistoryPointParam": "",
        "ddlCYear": "",
        "hdnCyear": "",
    }
    try:
        sel = Selector(text=text or "")
        params["__VIEWSTATE"] = _parse_field('//input[@id="__VIEWSTATE"]/@value', sel)
        params["__VIEWSTATEGENERATOR"] = _parse_field(
            '//input[@id="__VIEWSTATEGENERATOR"]/@value', sel
        )
        params["CSRFToken"] = _parse_field('//input[@id="hdnCSRFToken"]/@value', sel)
        params["HistoryPointParam"] = _parse_field(
            '//input[@id="ctl00_mainContent_hfHistoryPointParam"]/@value', sel
        )
        params["ddlCYear"] = _parse_field(
            '//select[@name="ctl00$mainContent$ddlCYear"]/option[@selected="selected"]/@value',
            sel,
        )
        params["hdnCyear"] = _parse_field(
            '//input[@id="ctl00_mainContent_hdnCyear"]/@value', sel
        )
        if not params["__VIEWSTATE"]:
            m_vs = re.search(r"__VIEWSTATE\|([^|]+)", text or "")
            m_gen = re.search(r"__VIEWSTATEGENERATOR\|([^|]+)", text or "")
            if m_vs:
                params["__VIEWSTATE"] = m_vs.group(1)
            if m_gen:
                params["__VIEWSTATEGENERATOR"] = m_gen.group(1)
    except Exception:  # noqa: BLE001 - hidden-field harvest is best-effort
        pass
    return params


def _make_row(ntrp_text, gender_text, team_name, season_start, no_players,
              usta_section, usta_district, local_league_league_type,
              team_ntrp_gender, flight_sub_flight_name, facility_name,
              facility_address, captain_name, captain_city_state, captain_ntrp):
    """Combine the scraped fields into a CSV row dict keyed by :data:`COLUMNS`."""
    return {
        "Ntrp Text": ntrp_text,
        "Gender Text": gender_text,
        "Team Name": team_name,
        "Season Start": season_start,
        "No. Players": no_players,
        "Usta Section": usta_section,
        "Usta District": usta_district,
        "Local League League Type": local_league_league_type,
        "Team Ntrp Gender": team_ntrp_gender,
        "Flight Sub-Flight Name": flight_sub_flight_name,
        "Facility Name": facility_name,
        "Facility Address": facility_address,
        "Captain Name": captain_name,
        "Captain City State": captain_city_state,
        "Captain Ntrp": captain_ntrp,
    }


def _chunk(lst, n):
    """Round-robin ``lst`` into at most ``n`` non-empty chunks (mirrors source split)."""
    if not lst:
        return []
    n = max(1, min(n, len(lst)))
    out = [[] for _ in range(n)]
    for i, item in enumerate(lst):
        out[i % n].append(item)
    return [c for c in out if c]


def _inject_cookies(client, cookies):
    """Re-inject the authenticated session cookies into a worker's client.

    ``curl_cffi`` sessions are not thread-safe, so each worker has its own client;
    this seeds it with the discovery session's login cookies (scoped to
    ``.usta.com`` so they reach TennisLink). Cookies are never logged.
    """
    for name, value in (cookies or {}).items():
        try:
            client.session.cookies.set(name, value, domain=".usta.com")
        except Exception:  # noqa: BLE001 - fall back to a domain-less cookie
            try:
                client.session.cookies.set(name, value)
            except Exception:  # noqa: BLE001 - a single bad cookie is non-fatal
                pass


# ---------------------------------------------------------------------------
# Auth + discovery
# ---------------------------------------------------------------------------
def _extract_state(resp):
    """Read the OIDC ``state`` from the login page's final URL (or a hidden field)."""
    state = ""
    try:
        final_url = getattr(resp, "url", "") or ""
        query = parse_qs(urlparse(final_url).query)
        if query.get("state"):
            state = query["state"][0]
    except Exception:  # noqa: BLE001 - odd URL is non-fatal
        state = ""
    if not state and resp is not None:
        try:
            sel = Selector(text=resp.text or "")
            state = sel.xpath('normalize-space(//input[@name="state"]/@value)').get() or ""
        except Exception:  # noqa: BLE001 - body may be undecodable
            state = ""
    return state


def _login(client, username, password, log):
    """Authenticate against USTA TennisLink. Returns ``True`` on success."""
    resp = client.get(LOGIN_PAGE_URL)
    if resp is None or not (200 <= resp.status_code < 300):
        return False
    state = _extract_state(resp)
    if not state:
        log("WARN", "\u26a0\ufe0f Could not read the OIDC 'state' from the login page")
        return False

    data = {
        "state": state,
        "ulp-login": "email",
        "username": username,
        "password": password,
    }
    resp2 = client.post(
        ACCOUNT_LOGIN_URL,
        params={"state": state},
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp2 is not None and 200 <= resp2.status_code < 300:
        if "Home.aspx" in (getattr(resp2, "url", "") or ""):
            return True
    # Backstop: explicitly confirm the session reaches the Leagues home page.
    home = client.get(HOME_URL)
    if home is not None and 200 <= home.status_code < 300:
        if "Home.aspx" in (getattr(home, "url", "") or ""):
            return True
    return False


def _parse_ntrp(client, log):
    """Return ``(ntrp_list, payload_params)`` from the StatsAndStandings page."""
    resp = client.get(SEARCH_URL)
    if resp is None or not (200 <= resp.status_code < 300):
        return [], {}
    text = resp.text or ""
    sel = client.selector(resp)
    payload_params = _get_payload_params(text)

    ntrp_list = []
    ntrp_q = (
        '//select[@id="ctl00_mainContent_ddlNTRPLevel"]'
        '/option[not(contains(text(), "-- select --"))]'
    )
    gender_q = (
        '//select[@id="ctl00_mainContent_ddlGender"]'
        '/option[not(contains(text(), "-- select --"))]'
    )
    for n_opt in sel.xpath(ntrp_q):
        ntrp_text = _parse_field("./text()", n_opt)
        ntrp_value = _parse_field("./@value", n_opt)
        for g_opt in sel.xpath(gender_q):
            ntrp_list.append({
                "ntrp_text": ntrp_text,
                "ntrp_value": ntrp_value,
                "gender_text": _parse_field("./text()", g_opt),
                "gender_value": _parse_field("./@value", g_opt),
            })
    return ntrp_list, payload_params


# ---------------------------------------------------------------------------
# Per-combo drill-down (team search -> team -> roster -> captain)
# ---------------------------------------------------------------------------
def _parse_captain_page(client, captain_target, csrf_token, text):
    """Open the captain's profile postback; return (name, city_state, ntrp)."""
    captain_name = ""
    captain_city_state = ""
    captain_ntrp = ""
    payload_params = _get_payload_params(text)
    data = {
        "ctl00$ScriptManager1": "ctl00$mainContent$UpdatePanel1|" + captain_target,
        "__LASTFOCUS": "",
        "hdnCSRFToken": csrf_token,
        "hdnCSRFLogId": "",
        "hdnCSRFCookieRefreshed": "false",
        "q_player_record": "on",
        "ctl00$SocialMediaPanel$isExistSocialMedia": "True",
        "ctl00$mainContent$hdnSearchType": "PlayerRoster",
        "ctl00$mainContent$hfScoreEntryPopupShowHide": "",
        "ctl00$mainContent$hfHistoryPointParam": payload_params.get("HistoryPointParam", ""),
        "__EVENTTARGET": captain_target,
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": payload_params.get("__VIEWSTATE", ""),
        "__VIEWSTATEGENERATOR": payload_params.get("__VIEWSTATEGENERATOR", ""),
        "__ASYNCPOST": "true",
    }
    resp = client.post(SEARCH_URL, data=data, headers=_AJAX_HEADERS)
    if resp is not None and 200 <= resp.status_code < 300:
        sel = client.selector(resp)
        anchor = '//table[@id="ctl00_mainContent_tblIndividualAnchor"]//tr[2]'
        captain_name = _parse_field(f"{anchor}/td[1]", sel)
        captain_city_state = _parse_field(f"{anchor}/td[2]", sel)
        captain_ntrp = _parse_field(f"{anchor}/td[3]", sel)
    return captain_name, captain_city_state, captain_ntrp


def _parse_roster_info(client, ntrp_text, gender_text, team_name, csrf_token,
                       text, sel, emit, log):
    """Pull the team metadata + captain from a Player Roster page; emit rows."""
    def field(label, row, col):
        return _parse_field(
            f'//td[contains(@class, "subhead") and contains(text(), "{label}")]'
            f"//ancestor::table//tr[{row}]/td[{col}]",
            sel,
        )

    season_start = field("Season Start", 2, 3)
    no_players = field("No. Players", 2, 4)
    usta_section = field("USTA Section", 2, 1)
    usta_district = field("USTA District", 2, 2)
    local_league_league_type = field("Local League / League Type", 2, 3)
    team_ntrp_gender = field("Team NTRP/Gender", 2, 4)
    flight_sub_flight_name = field("Flight/Sub-Flight Name", 2, 5)
    facility_name = field("Facility Name", 2, 1)
    facility_address = field("Facility Address", 2, 2)

    if not season_start:
        return

    captain_q = (
        '//td[contains(@class, "subhead") and contains(text(), "Captain Name")]'
        '//ancestor::table//tr[not(td[contains(@class, "subhead")])]'
        '//td//a[contains(@id, "CaptainForPlayerRosterForPublic")]'
    )

    def build(captain_name="", captain_city_state="", captain_ntrp=""):
        return _make_row(
            ntrp_text, gender_text, team_name, season_start, no_players,
            usta_section, usta_district, local_league_league_type,
            team_ntrp_gender, flight_sub_flight_name, facility_name,
            facility_address, captain_name, captain_city_state, captain_ntrp,
        )

    captain_found = False
    for a in sel.xpath(captain_q):
        captain_found = True
        captain_name = _format_name(_parse_field("./text()", a))
        captain_target = _parse_target(_parse_field("./@href", a))
        captain_city_state = ""
        captain_ntrp = ""
        if captain_target:
            _, captain_city_state, captain_ntrp = _parse_captain_page(
                client, captain_target, csrf_token, text
            )
        emit(build(captain_name, captain_city_state, captain_ntrp))

    if not captain_found:
        emit(build())


def _parse_player_roster(client, ntrp_text, gender_text, team_name, csrf_token,
                         text, sel, emit, log):
    """Follow the "Player Roster" postback for a team, then parse its roster info."""
    payload_params = _get_payload_params(text)
    roster_link = _parse_field(
        '//a[contains(@id, "ctl00_mainContent_lnkPlayerRosterForTeams")]/@href', sel
    )
    roster_target = _parse_target(roster_link)
    if not roster_target:
        return

    data = {
        "ctl00$ScriptManager1": "ctl00$mainContent$UpdatePanel1|" + roster_target,
        "__LASTFOCUS": "",
        "hdnCSRFToken": csrf_token,
        "hdnCSRFLogId": "",
        "hdnCSRFCookieRefreshed": "false",
        "q_player_record": "on",
        "ctl00$SocialMediaPanel$isExistSocialMedia": "True",
        "ctl00$mainContent$hdnMatchWinCriteria": "1",
        "ctl00$mainContent$hdnCyear": payload_params.get("hdnCyear", ""),
        "ctl00$mainContent$hdnIsGamesLostFirst": "False",
        "ctl00$mainContent$hdnTeamWinsSwapIndivWinsIndex": "0",
        "ctl00$mainContent$hdnTeamSummaryHTMLContent": "",
        "ctl00$mainContent$hdnTeamSummaryTeamMatchesHTMContent": "",
        "ctl00$mainContent$hdnSearchType": "DefaultType",
        "ctl00$mainContent$hfScoreEntryPopupShowHide": "",
        "ctl00$mainContent$hfHistoryPointParam": payload_params.get("HistoryPointParam", ""),
        "__EVENTTARGET": roster_target,
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": payload_params.get("__VIEWSTATE", ""),
        "__VIEWSTATEGENERATOR": payload_params.get("__VIEWSTATEGENERATOR", ""),
        "__ASYNCPOST": "true",
    }
    resp = client.post(SEARCH_URL, data=data, headers=_AJAX_HEADERS)
    if resp is None or not (200 <= resp.status_code < 300):
        return
    roster_text = resp.text or ""
    roster_sel = client.selector(resp)
    _parse_roster_info(
        client, ntrp_text, gender_text, team_name, csrf_token,
        roster_text, roster_sel, emit, log,
    )


def _parse_teams(client, ntrp_text, gender_text, csrf_token, text, sel, emit, log):
    """Iterate the team-results repeater and open each team's roster."""
    payload_params = _get_payload_params(text)
    for a in sel.xpath('//a[contains(@id, "mainContent_rptYearForTeamResults")]'):
        team_name = _parse_field("./text()", a)
        team_target = _parse_target(_parse_field("./@href", a))
        if not team_target:
            continue

        data = {
            "ctl00$ScriptManager1": "ctl00$mainContent$UpdatePanel1|" + team_target,
            "__LASTFOCUS": "",
            "hdnCSRFToken": csrf_token,
            "hdnCSRFLogId": "",
            "hdnCSRFCookieRefreshed": "false",
            "q_player_record": "on",
            "ctl00$SocialMediaPanel$isExistSocialMedia": "False",
            "ss_search_member_name_criteria_year": "",
            "ctl00$mainContent$ss_jsonSearForMemberFilterCriteria": "",
            "ctl00$mainContent$ss_jsonSearForMemberResultCriteria": "",
            "ctl00$mainContent$ss_jsonSearForMemberFirstLoadYear": "",
            "ss_search_member_name": "",
            "ctl00$mainContent$hdnSearchType": "",
            "ctl00$mainContent$hfScoreEntryPopupShowHide": "",
            "ctl00$mainContent$hfHistoryPointParam": payload_params.get("HistoryPointParam", ""),
            "__EVENTTARGET": team_target,
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": payload_params.get("__VIEWSTATE", ""),
            "__VIEWSTATEGENERATOR": payload_params.get("__VIEWSTATEGENERATOR", ""),
            "__ASYNCPOST": "true",
        }
        resp = client.post(SEARCH_URL, data=data, headers=_AJAX_HEADERS)
        if resp is None or not (200 <= resp.status_code < 300):
            continue
        team_text = resp.text or ""
        team_sel = client.selector(resp)
        _parse_player_roster(
            client, ntrp_text, gender_text, team_name, csrf_token,
            team_text, team_sel, emit, log,
        )


def _parse_search(client, ntrp_data, payload_params, year, emit, log):
    """POST the "Find Teams" search for one NTRP/Gender combo, then walk teams."""
    ntrp_text = ntrp_data.get("ntrp_text", "")
    ntrp_value = ntrp_data.get("ntrp_value", "")
    gender_text = ntrp_data.get("gender_text", "")
    gender_value = ntrp_data.get("gender_value", "")
    csrf_token = payload_params.get("CSRFToken", "")

    log("INFO", f"\U0001f50e NTRP {ntrp_text or '?'} \u00b7 {gender_text or '?'}")

    data = {
        "ctl00$ScriptManager1": "ctl00$mainContent$UpdatePanel1|ctl00$mainContent$btnSearchTeamByName",
        "hdnCSRFToken": csrf_token,
        "hdnCSRFLogId": "",
        "hdnCSRFCookieRefreshed": "false",
        "q_player_record": "on",
        "ctl00$SocialMediaPanel$isExistSocialMedia": "False",
        "ctl00$mainContent$ddlCYear": payload_params.get("ddlCYear", ""),
        "ctl00$mainContent$ddlDivision": "0",
        "ctl00$mainContent$ddlNTRPlevelChampionlevel": "0",
        "ctl00$mainContent$ddlGenderChampion": "0",
        "ctl00$mainContent$ddlClevel": "",
        "ctl00$mainContent$txtMatchNum": "",
        "ctl00$mainContent$ddlChampYear": year,
        "ctl00$mainContent$ddlDivisionForTeams": "0",
        "ctl00$mainContent$ddlSection": "",
        "ctl00$mainContent$ddlNTRPLevel": ntrp_value,
        "ctl00$mainContent$ddlGender": gender_value,
        "ctl00$mainContent$txtTeamName": "",
        "ctl00$mainContent$txtTeamNum": "",
        "ctl00$mainContent$txtUSTANum": "",
        "ctl00$mainContent$txtFirstName": "",
        "ctl00$mainContent$txtLastName": "",
        "ctl00$mainContent$hdnSearchType": "",
        "ctl00$mainContent$hfScoreEntryPopupShowHide": "",
        "ctl00$mainContent$hfHistoryPointParam": payload_params.get("HistoryPointParam", ""),
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__LASTFOCUS": "",
        "__VIEWSTATE": payload_params.get("__VIEWSTATE", ""),
        "__VIEWSTATEGENERATOR": payload_params.get("__VIEWSTATEGENERATOR", ""),
        "__ASYNCPOST": "true",
        "ctl00$mainContent$btnSearchTeamByName": "Find Teams",
    }
    resp = client.post(SEARCH_URL, data=data, headers=_AJAX_HEADERS)
    if resp is None or not (200 <= resp.status_code < 300):
        return
    text = resp.text or ""
    sel = client.selector(resp)
    _parse_teams(client, ntrp_text, gender_text, csrf_token, text, sel, emit, log)


def run(run_obj, log):
    """Execute the USTA Team Captains scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count

    params = run_obj.params or {}
    year = params.get("year")
    if year is None or str(year).strip() == "":
        year = run_obj.date_from.year if run_obj.date_from else timezone.localdate().year
    year = str(year)

    log("INFO", f"\U0001f3be USTA Team Captains starting \u2014 championship year {year}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    username = (getattr(settings, "USTA_USERNAME", "") or "").strip()
    password = getattr(settings, "USTA_PASSWORD", "") or ""
    if not (username and password):
        msg = "Set USTA_USERNAME and USTA_PASSWORD to run the USTA Team Captains scraper."
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    proxies = build_proxies(scraper, log)

    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 login + discovering NTRP/gender combos \u2500\u2500\u2500\u2500")
    with ScraperClient(
        log=log, tele=tele, proxies=proxies, max_redirects=MAX_REDIRECTS
    ) as discovery:
        if not _login(discovery, username, password, log):
            msg = (
                "USTA login failed \u2014 could not reach the Leagues Home.aspx "
                "(check USTA_USERNAME / USTA_PASSWORD)."
            )
            log("ERROR", f"\U0001f6d1 {msg}")
            tele.record_error(msg)
            return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED
        log("INFO", "\U0001f511 Authenticated \u2014 TennisLink session established")

        try:
            shared_cookies = discovery.session.cookies.get_dict()
        except Exception:  # noqa: BLE001 - fall back to a plain mapping
            try:
                shared_cookies = dict(discovery.session.cookies)
            except Exception:  # noqa: BLE001 - cookies still usable per-session
                shared_cookies = {}

        ntrp_list, payload_params = _parse_ntrp(discovery, log)

    if not ntrp_list:
        msg = (
            "No NTRP/Gender combinations found on StatsAndStandings \u2014 nothing "
            "to scrape (session may have expired or the page layout changed)."
        )
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    total = len(ntrp_list)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} NTRP/Gender combination(s) to search")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def emit(row):
        key = tuple(row.get(c, "") for c in COLUMNS)
        with lock:
            if key in seen:
                return
            seen.add(key)
            writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
            counter["rows"] += 1
        log(
            "INFO",
            f"   \U0001f3c5 {row.get('Team Name') or '?'} \u2014 captain "
            f"{row.get('Captain Name') or '?'} "
            f"[{row.get('Ntrp Text', '')} {row.get('Gender Text', '')}]",
        )

    def process_chunk(chunk):
        client = ScraperClient(
            log=log, tele=tele, proxies=proxies, max_redirects=MAX_REDIRECTS
        )
        _inject_cookies(client, shared_cookies)
        try:
            for ntrp_data in chunk:
                try:
                    _parse_search(client, ntrp_data, payload_params, year, emit, log)
                except Exception as exc:  # noqa: BLE001 - one combo can't kill the run
                    tele.record_error(
                        redact_secrets(
                            f"NTRP {ntrp_data.get('ntrp_text', '')} / "
                            f"{ntrp_data.get('gender_text', '')} failed: {exc}"
                        ),
                        exc=exc,
                    )
                    log(
                        "WARN",
                        redact_secrets(
                            f"\u26a0\ufe0f combo failed: {exc.__class__.__name__}: {exc}"
                        ),
                    )
                finally:
                    Run.objects.filter(pk=run_obj.pk).update(
                        progress_done=F("progress_done") + 1
                    )
        finally:
            client.close()

    chunks = _chunk(ntrp_list, workers)
    if chunks:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 searching teams + captains \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=len(chunks)) as executor:
            list(executor.map(process_chunk, chunks))

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
