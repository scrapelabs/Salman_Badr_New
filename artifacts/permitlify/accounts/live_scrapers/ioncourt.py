"""Ioncourt (college dual-match) scraper.

Ports the production ``ioncourt`` spider onto MatchMiner's shared HTTP client
(:mod:`accounts.live_scrapers._http`) + telemetry. Ioncourt is a **JSON API**
(no HTML), so this scraper POSTs JSON and reads JSON rather than parsing pages.
Input is a **date range** (``date_from`` / ``date_to``); ties are filtered to
that window client-side (the API returns them newest-first).

Flow:

1. ``POST /api/auth/login`` with ``{country_code, phone, password}`` and read the
   ``refresh-token`` **response header** (the bearer for the search endpoint).
2. Paginate ``POST /api/search/ties`` (``organisationAbbreviation=ITA``,
   ``type=college``, ``matchStatus=COMPLETED``), collecting tie ids whose
   ``startDate`` falls in the window. Because results are date-descending, paging
   stops after enough consecutive out-of-window ties (mirrors the source guard).
3. Per tie (concurrently): ``POST /api/tie/{id}/info`` for the dual-match teams,
   genders and team score, then ``POST /api/match/{id}/tie-matches`` for each
   individual match, emitted as one CSV row.

**Deterministic / AI-free port.** The original fed every college name through an
OpenAI "official name" lookup; that is dropped — the cleaned scraped name (the
team label minus its ``(M)/(W)`` suffix) is used as-is. Two source quirks are
corrected so the port actually returns data: matches are keyed by their real
``_id`` (the source read a non-existent ``matchId`` key, which collapsed every
match to a single deduped row), and the player→college join uses the info side's
``person._id`` (which equals the match player's ``participant._id``).

Credentials come from ``settings.IONCOURT_PHONE`` / ``settings.IONCOURT_PASSWORD``
(env vars). They are **never** hard-coded or logged; when unset the run fails
honestly (like a Stadion scraper without its residential proxy).

``run(run_obj, log)`` returns ``(items_csv, requests_csv, errors_csv, row_count,
status)``.
"""

import csv
import io
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from django.conf import settings
from django.db.models import F

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

LOGIN_URL = "https://api.ioncourt.com/api/auth/login"
TIES_URL = "https://api.ioncourt.com/api/search/ties"
INFO_URL = "https://api.ioncourt.com/api/tie/{tie_id}/info"
MATCHES_URL = "https://api.ioncourt.com/api/match/{tie_id}/tie-matches"
TIE_PAGE_URL = "https://ioncourt.com/ties/{tie_id}"

COUNTRY_CODE = "+1"
PAGE_SIZE = 30
# A fixed, non-secret browser device id from the source; the detail endpoints
# only require it (no bearer token).
DEVICE_ID = "cdfb6c04-6ee4-4cf2-9666-52630499812e"
# Ties come back newest-first; stop paging once this many out-of-window ties have
# been seen (mirrors the source's cumulative counter guard).
OUT_OF_WINDOW_LIMIT = 60

_JSON_ACCEPT = {"Accept": "application/json, text/plain, */*", "Content-Type": "application/json"}
_DETAIL_HEADERS = dict(_JSON_ACCEPT, **{"device-id": DEVICE_ID})

# Items CSV columns — the shared MatchMiner items schema (same as Brazil), so
# downloaded files stay uniform across scrapers. Ioncourt's dual-match extras
# (team score, raw team labels) survive via the per-player *_college fields and
# the embedded ``tournament_name`` ("Dual Match: A vs B - Men").
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


# --- deterministic helpers (AI-free ports of the source's formatters) ------
def _clean_college(raw):
    """College name with the ``(M)/(W)`` suffix (and anything after ``(``) removed.

    The source fed this string to an OpenAI "official name" lookup; AI-free we
    keep the cleaned scraped label.
    """
    return re.sub(r"\(.*", "", raw or "").strip()


def _team_name_gender(text):
    """Split ``"Flagler College (M)"`` into ``("Flagler College", "M")``.

    ``W`` is normalised to ``F`` to match the items schema's gender codes.
    """
    match = re.match(r"(.+?) \((M|W|F)\)", text or "")
    if not match:
        return "", ""
    gender = match.group(2)
    if gender == "W":
        gender = "F"
    return _clean_college(match.group(1)), gender


def _team_score(raw):
    """Format the tie's team score larger-first, e.g. ``"4 - 3"`` -> ``"4-3;"``."""
    a, b = (int(x) for x in str(raw).split("-"))
    return f"{max(a, b)}-{min(a, b)};"


def _match_score(score_str):
    """Format a match score: drop parenthesised set/game noise, keep tiebreaks.

    Parentheses are removed only when their content contains a dash (set games
    like ``(6-3)``); tiebreak parentheses such as ``(5)`` are kept. A trailing
    ``;`` matches the production framework's score format.
    """
    def replacer(match):
        content = match.group(1)
        return "" if "-" in content else f"({content})"

    return re.sub(r"\(([^)]+)\)", replacer, score_str or "").strip() + ";"


def _fmt_date(raw, out_format):
    """Reformat an ISO ``...Z`` timestamp; ``""`` on any parse failure."""
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").strftime(out_format)
    except Exception:  # noqa: BLE001 - missing/garbled date is non-fatal
        return ""


def _participant_name(participant):
    """``"Last, First"`` (title-cased), or ``""`` when no name is present."""
    last = (participant or {}).get("last_name", "") or ""
    first = (participant or {}).get("first_name", "") or ""
    if not (last or first):
        return ""
    return f"{last}, {first}".title()


# --- API calls -------------------------------------------------------------
def _login(client, phone, password):
    """Authenticate and return the ``refresh-token`` header, or ``""``."""
    resp = client.post(
        LOGIN_URL,
        headers=_JSON_ACCEPT,
        json={"credentials": {"country_code": COUNTRY_CODE, "phone": phone, "password": password}},
    )
    if resp is not None and 200 <= resp.status_code < 300:
        return resp.headers.get("refresh-token") or ""
    return ""


def _fetch_ties_page(client, token, page):
    """Fetch one page of the college-ties search, or ``None``."""
    headers = dict(_JSON_ACCEPT, **{"Refresh-Token": token, "Authorization": token})
    body = {
        "filter": {
            "from": None,
            "to": None,
            "organisationAbbreviation": "ITA",
            "type": "college",
            "subtype": None,
            "gender": None,
            "matchStatus": "COMPLETED",
            "page": page,
        }
    }
    resp = client.post(TIES_URL, headers=headers, json=body)
    if resp is not None and 200 <= resp.status_code < 300:
        try:
            return resp.json()
        except Exception:  # noqa: BLE001 - body wasn't JSON
            return None
    return None


def _discover_ties(client, token, start_d, end_d, log):
    """Return tie ids whose ``startDate`` falls within ``[start_d, end_d]``."""
    first = _fetch_ties_page(client, token, 0)
    if not first:
        return []
    pagination = first.get("data", {}).get("pagination", {}) or {}
    total = pagination.get("totalRecords", 0) or 0
    pages = math.ceil(total / PAGE_SIZE) if total else 1
    log("INFO", f"\U0001f50e {total} completed college tie(s) available across {pages} page(s)")

    tie_ids = []
    seen = set()
    state = {"out_of_window": 0}

    def harvest(results):
        """Collect in-window tie ids; return True once the page guard trips."""
        for tie in results.get("data", {}).get("ties", []):
            tie_id = tie.get("_id", "")
            if not tie_id:
                continue
            tie_date_str = _fmt_date(tie.get("startDate", ""), "%Y-%m-%d")
            if not tie_date_str:
                continue
            tie_date = datetime.strptime(tie_date_str, "%Y-%m-%d").date()
            if start_d <= tie_date <= end_d:
                if tie_id not in seen:
                    seen.add(tie_id)
                    tie_ids.append(tie_id)
            else:
                state["out_of_window"] += 1
                if state["out_of_window"] > OUT_OF_WINDOW_LIMIT:
                    return True
        return False

    stop = harvest(first)
    page = 1
    while not stop and page < pages:
        results = _fetch_ties_page(client, token, page)
        if results:
            stop = harvest(results)
        page += 1
    return tie_ids


def _scrape_tie(client, tie_id):
    """Fetch a tie's info + matches and return a list of CSV row dicts."""
    info_resp = client.post(INFO_URL.format(tie_id=tie_id), headers=_DETAIL_HEADERS, json={"skipCache": False})
    if info_resp is None or not (200 <= info_resp.status_code < 300):
        return []
    try:
        info = info_resp.json()
    except Exception:  # noqa: BLE001 - body wasn't JSON
        return []
    data = info.get("data", {}) or {}

    date_str = _fmt_date(data.get("startDate", ""), "%m/%d/%Y")
    winning_side = data.get("winningSide") or 1

    winner_team = loser_team = ""
    winner_gender = loser_gender = ""
    college_map = {}
    for side in data.get("sides", []):
        team = side.get("team", {}) or {}
        raw_name = team.get("name", "")
        name, gender = _team_name_gender(raw_name)
        if side.get("sideNumber") == winning_side:
            winner_team, winner_gender = name, gender
        else:
            loser_team, loser_gender = name, gender
        # Match players reference the info side's ``person._id`` as their
        # ``participant._id`` — key the college map on it.
        for participant in team.get("participants", []):
            person_id = (participant.get("person") or {}).get("_id", "")
            if person_id:
                college_map[person_id] = raw_name

    try:
        team_score = _team_score(data.get("score", ""))
    except Exception:  # noqa: BLE001 - malformed team score is non-fatal
        team_score = ""

    matches_resp = client.post(
        MATCHES_URL.format(tie_id=tie_id), headers=_DETAIL_HEADERS, json={"skipCache": False}
    )
    if matches_resp is None or not (200 <= matches_resp.status_code < 300):
        return []
    try:
        matches = matches_resp.json()
    except Exception:  # noqa: BLE001 - body wasn't JSON
        return []

    rows = []
    for match in matches.get("data", []):
        row = _build_row(
            match, tie_id, date_str, winner_team, loser_team,
            winner_gender, loser_gender, team_score, college_map,
        )
        if row:
            rows.append(row)
    return rows


def _build_row(match, tie_id, date_str, winner_team, loser_team, winner_gender, loser_gender, team_score, college_map):
    """Turn one tie-match into a CSV row dict, or ``None`` if it has no id."""
    match_id = match.get("_id", "")
    if not match_id:
        return None
    winning_side = match.get("winningSide") or 1

    winner = [("", "", ""), ("", "", "")]
    loser = [("", "", ""), ("", "", "")]
    for side in match.get("sides", []):
        players = side.get("players", []) or []
        slot = []
        for player in players:
            participant = player.get("participant", {}) or {}
            tpid = participant.get("_id", "")
            slot.append((_participant_name(participant), tpid, _clean_college(college_map.get(tpid, ""))))
        while len(slot) < 2:
            slot.append(("", "", ""))
        if side.get("sideNumber") == winning_side:
            winner = slot[:2]
        else:
            loser = slot[:2]

    (w1_name, w1_id, w1_col), (w2_name, w2_id, w2_col) = winner
    (l1_name, l1_id, l1_col), (l2_name, l2_id, l2_col) = loser

    w1_gender = w2_gender = winner_gender
    if not w2_name:
        w2_gender = ""
    l1_gender = l2_gender = loser_gender
    if not l2_name:
        l2_gender = ""

    draw_gender = ""
    tournament_gender = ""
    if w1_gender == "M":
        draw_gender, tournament_gender = "Male", "Men"
    elif w1_gender == "F":
        draw_gender, tournament_gender = "Female", "Women"

    score = ""
    try:
        score = _match_score((match.get("score", {}) or {}).get(f"scoreStringSide{winning_side}", ""))
    except Exception:  # noqa: BLE001 - odd score shape is non-fatal
        score = ""

    draw_team_type = ""
    match_type = match.get("matchType", "")
    if match_type == "S":
        draw_team_type = "Singles"
    elif match_type == "D":
        draw_team_type = "Doubles"

    draw_name = ""
    if draw_team_type:
        draw_name = f"#{match.get('collectionPosition', '')} {draw_team_type}"

    tournament_name = f"Dual Match: {winner_team} vs {loser_team} - {tournament_gender}"

    outcome = (match.get("matchStatus", "") or "").title()
    if outcome == "Incomplete":
        outcome = "Tie"

    return {
        "match_id": match_id,
        "ball_type": "Yellow",
        "id_type": "Ioncourt",
        "draw_name": draw_name,
        "draw_team_type": draw_team_type,
        "draw_gender": draw_gender,
        "tournament_name": tournament_name,
        "date": date_str,
        "score": score,
        "outcome": outcome,
        "winner_1_name": w1_name, "winner_1_gender": w1_gender,
        "winner_1_third_party_id": w1_id, "winner_1_college": w1_col,
        "winner_2_name": w2_name, "winner_2_gender": w2_gender,
        "winner_2_third_party_id": w2_id, "winner_2_college": w2_col,
        "loser_1_name": l1_name, "loser_1_gender": l1_gender,
        "loser_1_third_party_id": l1_id, "loser_1_college": l1_col,
        "loser_2_name": l2_name, "loser_2_gender": l2_gender,
        "loser_2_third_party_id": l2_id, "loser_2_college": l2_col,
        "tournament_event_type": "Dual Match",
        "tournament_import_source": "College",
        "tournament_sanction_body": "College",
        "tournament_start_date": date_str,
        "tournament_end_date": date_str,
        "tournament_url": TIE_PAGE_URL.format(tie_id=tie_id),
    }


def run(run_obj, log):
    """Execute the Ioncourt scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    start_d = run_obj.date_from
    end_d = run_obj.date_to

    log(
        "INFO",
        f"\U0001f3be Ioncourt (college dual matches) starting \u2014 window "
        f"{start_d} \u2192 {end_d}",
    )
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    if not (start_d and end_d):
        msg = "Ioncourt needs a date range (date_from / date_to) to filter ties."
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    phone = (getattr(settings, "IONCOURT_PHONE", "") or "").strip()
    password = getattr(settings, "IONCOURT_PASSWORD", "") or ""
    if not (phone and password):
        msg = (
            "Ioncourt credentials missing \u2014 set IONCOURT_PHONE and "
            "IONCOURT_PASSWORD (Replit secrets, or the local .env) to enable "
            "this source."
        )
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    proxies = build_proxies(scraper, log)

    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 login + discovering ties \u2500\u2500\u2500\u2500")
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        token = _login(discovery, phone, password)
        if not token:
            msg = "Ioncourt login failed \u2014 no refresh-token returned (check credentials)."
            log("ERROR", f"\U0001f6d1 {msg}")
            tele.record_error(msg)
            return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED
        log("INFO", "\U0001f511 Authenticated \u2014 refresh-token acquired")
        tie_ids = _discover_ties(discovery, token, start_d, end_d, log)

    total = len(tie_ids)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} tie(s) in window")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def process(tie_id):
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            rows = _scrape_tie(client, tie_id)
            for row in rows:
                key = (tie_id, row.get("match_id", ""))
                with lock:
                    if key in seen:
                        continue
                    seen.add(key)
                    writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
                    counter["rows"] += 1
                log(
                    "INFO",
                    f"   \U0001f3be {row.get('draw_team_type', '')}: "
                    f"{row.get('winner_1_name') or '?'} def. "
                    f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                    f"\u2014 {row.get('tournament_name') or 'Ioncourt'}",
                )
        except Exception as exc:  # noqa: BLE001 - one bad tie can't kill the run
            tele.record_error(redact_secrets(f"Tie {tie_id} failed: {exc}"), exc=exc)
            log(
                "WARN",
                redact_secrets(f"\u26a0\ufe0f tie failed: {exc.__class__.__name__}: {exc}"),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
            client.close()

    if tie_ids:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping ties \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, tie_ids))

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
