"""MaxPreps (US high-school tennis) results scraper.

Ports the production ``maxpreps`` spider onto MatchMiner's shared HTTP client
(:mod:`accounts.live_scrapers._http`) + telemetry. MaxPreps exposes an
**XML feed** (no HTML, no JSON), so this scraper GETs the affiliate results
feed and parses the returned XML rather than scraping pages.

Input is a **date range** (``date_from`` / ``date_to``). The source iterates
*every* calendar day in the inclusive window and, for each day, requests both
the ``Singles`` and ``Doubles`` feeds:

    GET https://www.maxpreps.com/feeds/affiliates/ut/results.ashx
        ?ssid={ssid}&type={singles|doubles}&updated={m/d/Y}
        &apikey={api_key}&state=UTR

Each ``<Match>`` element in the XML is one played match and becomes one CSV row.
Every interesting value (names, genders, schools, scores, dates) is a flat XML
**attribute** on the ``<Match>`` element.

**Deterministic / AI-free port.** The source contains *no* AI/LLM calls — gender
comes straight from the feed's ``DrawGender`` / ``Winner1Gender`` … attributes
(no name-guessing, no college-name normalization). Player/college fields are
emitted exactly as the feed provides them; the only transforms are deterministic
formatting (``Last, First`` names, date reformatting, gender spelling). Nothing
AI-flavoured had to be removed.

The ``apikey`` is a non-secret constant baked into the source; it is exposed as
:data:`MAXPREPS_API_KEY` and may be overridden via ``settings.MAXPREPS_API_KEY``.
The feed ``ssid`` is read from the run's params (``ssid``) with a settings /
module-constant fallback so the scraper still runs out of the box.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import io
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

# Affiliate results feed (XML). State is fixed to "UTR" exactly like the source.
BASE = "https://www.maxpreps.com/feeds/affiliates/ut/results.ashx"
STATE = "UTR"
# Match-type feeds requested for every day (sent lower-cased as the ``type``
# query param, e.g. "singles" / "doubles").
MATCH_TYPES = ("Singles", "Doubles")

# Non-secret API key hard-coded in the source feed URL. Overridable via
# ``settings.MAXPREPS_API_KEY`` (Replit secret / local .env) without code edits.
MAXPREPS_API_KEY = "50B280F4-5F20-4191-8AEC-726AA3AD800C"
# The feed's ``ssid`` (the affiliate stream id from the source's example URL).
# Used only when the run / settings don't supply one.
DEFAULT_SSID = "e59ca771-23ee-484f-8ccf-d2b8c3ef4878"

# Items CSV columns — the shared MatchMiner items schema (same as Brazil/Czech/
# Ioncourt), so downloaded files stay uniform across scrapers.
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
# Deterministic helpers (AI-free ports of the source's formatters)
# ---------------------------------------------------------------------------
def _api_key(params=None):
    """The MaxPreps feed API key.

    Prefers a run-supplied ``api_key`` param (start form / webhook), then a
    ``settings.MAXPREPS_API_KEY`` override, then the module default.
    """
    params = params or {}
    return (
        (params.get("api_key") or "").strip()
        or (getattr(settings, "MAXPREPS_API_KEY", "") or "").strip()
        or MAXPREPS_API_KEY
    )


def _match_types(params):
    """The match-type feeds to scrape (Singles / Doubles / both).

    Honours an optional ``rank_type`` param (``singles`` / ``doubles`` / ``both``);
    anything else collects both feeds, preserving the historical default.
    """
    rt = (params.get("rank_type") or "").strip().lower()
    if rt == "singles":
        return ("Singles",)
    if rt == "doubles":
        return ("Doubles",)
    return MATCH_TYPES


def _feed_ssid(params):
    """Resolve the feed ``ssid``: run params -> settings -> module default."""
    return (
        (params.get("ssid") or "").strip()
        or (getattr(settings, "MAXPREPS_SSID", "") or "").strip()
        or DEFAULT_SSID
    )


def _date_window(start_d, end_d):
    """Yield every ``date`` in the inclusive ``[start_d, end_d]`` window."""
    day = start_d
    while day <= end_d:
        yield day
        day += timedelta(days=1)


def _parse_date(date_str):
    """Reformat a feed date to ``YYYY-MM-DD`` (faithful to the source).

    Handles the feed's ``M/D/YYYY`` form plus ISO datetime/date; returns the
    original string unchanged when nothing matches, and ``""`` when empty.
    """
    date_str = (date_str or "").strip()
    if not date_str:
        return ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str


def _format_name(attribs, prefix):
    """``"Last, First"`` from ``{prefix}LastName`` / ``{prefix}FirstName``.

    Returns ``""`` when both name parts are absent.
    """
    last = attribs.get(f"{prefix}LastName", "")
    first = attribs.get(f"{prefix}FirstName", "")
    return f"{last}, {first}" if last or first else ""


def _spell_gender(raw):
    """Spell a draw gender out as ``"Male"``/``"Female"`` (feed value, no guess)."""
    g = (raw or "").strip()
    u = g.upper()
    if u in ("M", "MALE", "MEN", "BOY", "BOYS"):
        return "Male"
    if u in ("F", "FEMALE", "W", "WOMEN", "GIRL", "GIRLS"):
        return "Female"
    return g


def _norm_gender(raw):
    """Normalise a player gender to ``"M"``/``"F"`` (feed value, no guess)."""
    g = (raw or "").strip()
    u = g.upper()
    if u in ("M", "MALE", "MEN", "BOY", "BOYS"):
        return "M"
    if u in ("F", "FEMALE", "W", "WOMEN", "GIRL", "GIRLS"):
        return "F"
    return g


# ---------------------------------------------------------------------------
# Feed URL + XML parsing
# ---------------------------------------------------------------------------
def _index_url(ssid, api_key, job_type, updated):
    """Build the affiliate-feed URL, mirroring the source's raw string format.

    ``updated`` is an ``m/d/Y`` string; the slashes are kept raw (as the source
    does) rather than percent-encoded.
    """
    return (
        f"{BASE}?ssid={ssid}&type={job_type.lower()}"
        f"&updated={updated}&apikey={api_key}&state={STATE}"
    )


def _parse_matches(content):
    """Parse the feed body into a list of ``<Match>`` XML elements."""
    if not content:
        return []
    try:
        # Strip a UTF-8 BOM if present, then parse the XML (faithful to source).
        xml_text = content.decode("utf-8-sig")
        root = ET.fromstring(xml_text)
        return root.findall("Match")
    except Exception:  # noqa: BLE001 - a malformed feed body yields no matches
        return []


def _row_from_match(match):
    """Map one ``<Match>`` element's attributes onto a full CSV row dict."""
    a = match.attrib

    draw_team_type = a.get("DrawTeamType", "")
    is_doubles = draw_team_type.strip().lower() == "doubles"

    match_date = _parse_date(a.get("Date", ""))
    raw_score = a.get("Score", "")
    score = f"{raw_score};" if raw_score else ""

    w1_school = a.get("Winner1SchoolName", "")
    l1_school = a.get("Loser1SchoolName", "")
    tournament_name = (
        a.get("TournamentName", "")
        or f"Dual Match: {w1_school} vs {l1_school}"
    )

    t_start = a.get("TournamentStartDate", "")
    t_end = a.get("TournamentEndDate", "")

    return {
        # ── identifiers / draw ──────────────────────────────────────────
        "match_id": a.get("MatchID", ""),
        "ball_type": "Yellow",
        "id_type": "Maxpreps",
        "draw_bracket_value": a.get("DrawBracketValue", ""),
        "draw_name": a.get("DrawName", ""),
        "draw_team_type": draw_team_type,
        "draw_gender": _spell_gender(a.get("DrawGender", "")),
        "draw_bracket_type": a.get("DrawBracketType", ""),
        "draw_type": a.get("DrawType", ""),
        "round": "",
        # ── match ───────────────────────────────────────────────────────
        "date": match_date,
        "score": score,
        "outcome": "Completed",
        # ── winner 1 ────────────────────────────────────────────────────
        "winner_1_name": _format_name(a, "Winner1"),
        "winner_1_gender": _norm_gender(a.get("Winner1Gender", "")),
        "winner_1_dob": a.get("Winner1DOB", ""),
        "winner_1_third_party_id": a.get("Winner1ThirdPartyID", ""),
        "winner_1_city": a.get("Winner1SchoolCity", ""),
        "winner_1_state": a.get("Winner1SchoolState", ""),
        "winner_1_country": a.get("Winner1Country", "") or "USA",
        "winner_1_college": "",
        # ── winner 2 (doubles only) ─────────────────────────────────────
        "winner_2_name": _format_name(a, "Winner2") if is_doubles else "",
        "winner_2_gender": _norm_gender(a.get("Winner2Gender", "")) if is_doubles else "",
        "winner_2_dob": a.get("Winner2DOB", "") if is_doubles else "",
        "winner_2_third_party_id": a.get("Winner2ThirdPartyID", "") if is_doubles else "",
        "winner_2_city": a.get("Winner2SchoolCity", "") if is_doubles else "",
        "winner_2_state": a.get("Winner2SchoolState", "") if is_doubles else "",
        "winner_2_country": (a.get("Winner2Country", "") or "USA") if is_doubles else "",
        "winner_2_college": "",
        # ── loser 1 ─────────────────────────────────────────────────────
        "loser_1_name": _format_name(a, "Loser1"),
        "loser_1_gender": _norm_gender(a.get("Loser1Gender", "")),
        "loser_1_dob": a.get("Loser1DOB", ""),
        "loser_1_third_party_id": a.get("Loser1ThirdPartyID", ""),
        "loser_1_city": a.get("Loser1SchoolCity", ""),
        "loser_1_state": a.get("Loser1SchoolState", ""),
        "loser_1_country": a.get("Loser1Country", "") or "USA",
        "loser_1_college": "",
        # ── loser 2 (doubles only) ──────────────────────────────────────
        "loser_2_name": _format_name(a, "Loser2") if is_doubles else "",
        "loser_2_gender": _norm_gender(a.get("Loser2Gender", "")) if is_doubles else "",
        "loser_2_dob": a.get("Loser2DOB", "") if is_doubles else "",
        "loser_2_third_party_id": a.get("Loser2ThirdPartyID", "") if is_doubles else "",
        "loser_2_city": a.get("Loser2SchoolCity", "") if is_doubles else "",
        "loser_2_state": a.get("Loser2SchoolState", "") if is_doubles else "",
        "loser_2_country": (a.get("Loser2Country", "") or "USA") if is_doubles else "",
        "loser_2_college": "",
        # ── tournament ──────────────────────────────────────────────────
        "tournament_name": tournament_name,
        "tournament_city": a.get("TournamentCity", ""),
        "tournament_state": a.get("TournamentState", ""),
        "tournament_country_code": a.get("TournamentCountryCode", "") or "USA",
        "tournament_host": a.get("TournamentHost", ""),
        "tournament_location_type": a.get("LocationType", ""),
        "tournament_surface": a.get("Surface", ""),
        "tournament_event_category": a.get("EventCategory", ""),
        "tournament_event_grade": a.get("EventGrade", ""),
        "tournament_import_source": "Maxpreps",
        "tournament_sanction_body": "Maxpreps",
        "tournament_event_type": a.get("EventType", "") or "Dual Match",
        "tournament_url": a.get("TournamentURL", ""),
        "tournament_country": a.get("TournamentCountry", "") or "USA",
        "tournament_start_date": _parse_date(t_start) if t_start else match_date,
        "tournament_end_date": _parse_date(t_end) if t_end else match_date,
    }


def _scrape_feed(client, ssid, api_key, job_type, job_date):
    """Fetch + parse one (match-type, date) feed; return a list of row dicts."""
    updated = job_date.strftime("%m/%d/%Y")
    url = _index_url(ssid, api_key, job_type, updated)
    resp = client.get(url)
    if resp is None or not (200 <= resp.status_code < 300):
        return []
    matches = _parse_matches(resp.content)
    return [_row_from_match(m) for m in matches]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run(run_obj, log):
    """Execute the MaxPreps scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    params = run_obj.params or {}
    start_d = run_obj.date_from
    end_d = run_obj.date_to

    log(
        "INFO",
        f"\U0001f3be MaxPreps (US high-school tennis) starting \u2014 window "
        f"{start_d} \u2192 {end_d}",
    )
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    if not (start_d and end_d):
        msg = "MaxPreps needs a date range (date_from / date_to) to scrape the feed."
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    if start_d > end_d:
        start_d, end_d = end_d, start_d

    ssid = _feed_ssid(params)
    api_key = _api_key(params)
    match_types = _match_types(params)
    proxies = build_proxies(scraper, log)

    # One task per (match-type, day); the source requests the selected feeds for
    # every day in the window (both Singles and Doubles unless rank_type narrows it).
    tasks = [
        (job_type, job_date)
        for job_type in match_types
        for job_date in _date_window(start_d, end_d)
    ]
    total = len(tasks)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} feed request(s) queued ({len(match_types)} type(s) \u00d7 days)")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def process(task):
        job_type, job_date = task
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            rows = _scrape_feed(client, ssid, api_key, job_type, job_date)
            for row in rows:
                # Mirror the source's dedup key (names + score + match id).
                key = (
                    row.get("match_id", ""),
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
                    f"   \U0001f3c6 {row.get('draw_team_type', '') or job_type}: "
                    f"{row.get('winner_1_name') or '?'} def. "
                    f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                    f"@ {row.get('tournament_name') or 'MaxPreps'}",
                )
        except Exception as exc:  # noqa: BLE001 - one bad feed can't kill the run
            tele.record_error(
                redact_secrets(f"Feed {job_type} {job_date} failed: {exc}"),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f feed failed: {exc.__class__.__name__}: {exc}"
                ),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
            client.close()

    if tasks:
        log("INFO", "\u2500\u2500\u2500\u2500 fetching feeds \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, tasks))

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
