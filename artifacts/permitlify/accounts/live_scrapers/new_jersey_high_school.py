"""New Jersey high-school tennis (njschoolsports.com) scraper.

Ports the production ``new_jersey_high_school`` spider onto MatchMiner's shared
HTTP client (:mod:`accounts.live_scrapers._http`) + telemetry. The source is a
single JSON feed — for each day in the run window it GETs the UTR results feed
once per gender (boys + girls) and emits one CSV row per individual match.

Flow:

1. Read the run's date window (``date_from`` / ``date_to``); expand it to one
   ``date`` per day (the source chunks the range one day at a time).
2. For each day, GET ``/Feeds/UTRResults/`` with ``key`` / ``gender`` /
   ``gamedate`` params, iterating **both** genders (``boys`` then ``girls``)
   exactly as the source's URL template does. ``gamedate`` is ``m/d/Y``.
3. Walk ``games.games[].matches[]``; a match with a ``winner2``/``loser2`` is a
   doubles match. Gender comes from the game's ``sportGender`` (``Boys`` -> M /
   Male, ``Girls`` -> F / Female); player names are ``"lastName, firstName"``.

This is a **deterministic** port: the source contains no AI/LLM/name-guessing or
college-normalization, so nothing AI-flavoured had to be removed — every field
is mapped straight off the feed. The college columns simply stay blank as in the
source.

The feed API key is a module constant (:data:`NJ_HS_API_KEY`), overridable via
``settings.NJ_HS_API_KEY``; it is not a secret in the source but is kept
configurable. The scraper's proxy is honoured via :func:`build_proxies` and is
never logged.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import F

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

# UTR results JSON feed. ``key`` / ``gender`` / ``gamedate`` are passed as query
# params (the source hard-codes them into the URL template).
FEED_URL = "https://www.njschoolsports.com/Feeds/UTRResults/"
# Iterated for every day in the window, exactly like the source's URL template.
GENDERS = ("boys", "girls")
# Non-secret feed key from the source; overridable via settings for safety.
NJ_HS_API_KEY = "4f59cee1-3db0-4128-84ba-bd7995dadd95"

# Items CSV columns — the shared MatchMiner items schema (copied verbatim from
# czech_scraper), so downloaded files stay uniform across scrapers.
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
# Deterministic field helpers (verbatim ports of the source's formatters)
# ---------------------------------------------------------------------------
def _gender_short(sport_gender):
    """``Boys`` -> ``M``, ``Girls`` -> ``F``, anything else -> ``""``."""
    if sport_gender == "Boys":
        return "M"
    if sport_gender == "Girls":
        return "F"
    return ""


def _gender_long(sport_gender):
    """``Boys`` -> ``Male``, ``Girls`` -> ``Female``, anything else -> ``""``."""
    if sport_gender == "Boys":
        return "Male"
    if sport_gender == "Girls":
        return "Female"
    return ""


def _format_name(player):
    """``"lastName, firstName"`` for a player dict, or ``""`` when empty."""
    if not player:
        return ""
    last = player.get("lastName", "") or ""
    first = player.get("firstName", "") or ""
    return f"{last}, {first}" if (last or first) else ""


def _parse_date(date_str):
    """Reformat the feed's ``gameDate`` to ``%Y-%m-%d``; raw on parse failure."""
    if not date_str:
        return ""
    try:
        return datetime.fromisoformat(date_str).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 - a non-ISO date is kept verbatim
        return date_str


def _iter_dates(start_d, end_d):
    """Yield every ``date`` from ``start_d`` to ``end_d`` inclusive."""
    current = start_d
    while current <= end_d:
        yield current
        current += timedelta(days=1)


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------
def _build_row(game, match):
    """Map one feed ``match`` (within its ``game``) onto a full CSV row dict."""
    game_report_id = game.get("gameReportId", "")
    event_result_id = match.get("eventResultId", "")
    sport_gender = game.get("sportGender", "")
    game_date = _parse_date(game.get("gameDate", ""))

    winner1 = match.get("winner1") or {}
    winner2 = match.get("winner2") or {}
    loser1 = match.get("loser1") or {}
    loser2 = match.get("loser2") or {}

    # A doubles match has a second winner/loser object.
    is_doubles = bool(match.get("winner2") or match.get("loser2"))

    gender_short = _gender_short(sport_gender)
    w1_school = winner1.get("schoolName", "")
    l1_school = loser1.get("schoolName", "")

    return {
        "match_id": f"{game_report_id}-{event_result_id}",
        "ball_type": "Yellow",
        "id_type": "NNJHS",
        "draw_bracket_value": "",
        "draw_name": match.get("eventName", ""),
        "draw_team_type": "Doubles" if is_doubles else "Singles",
        "tournament_name": f"Dual Match: {w1_school} vs {l1_school}",
        "date": game_date,
        "round": "",
        "score": match.get("score", ""),
        # Winner 1
        "winner_1_name": _format_name(winner1),
        "winner_1_gender": gender_short,
        "winner_1_dob": "",
        "winner_1_third_party_id": str(winner1.get("playerId", "")),
        "winner_1_city": winner1.get("schoolCity", ""),
        "winner_1_state": winner1.get("schoolState", ""),
        "winner_1_country": "USA",
        # Winner 2 (doubles only)
        "winner_2_name": _format_name(winner2) if is_doubles else "",
        "winner_2_gender": gender_short if is_doubles else "",
        "winner_2_dob": "",
        "winner_2_third_party_id": str(winner2.get("playerId", "")) if is_doubles else "",
        "winner_2_city": winner2.get("schoolCity", "") if is_doubles else "",
        "winner_2_state": winner2.get("schoolState", "") if is_doubles else "",
        "winner_2_country": "USA" if is_doubles else "",
        # Loser 1
        "loser_1_name": _format_name(loser1),
        "loser_1_gender": gender_short,
        "loser_1_dob": "",
        "loser_1_third_party_id": str(loser1.get("playerId", "")),
        "loser_1_city": loser1.get("schoolCity", ""),
        "loser_1_state": loser1.get("schoolState", ""),
        "loser_1_country": "USA",
        # Loser 2 (doubles only)
        "loser_2_name": _format_name(loser2) if is_doubles else "",
        "loser_2_gender": gender_short if is_doubles else "",
        "loser_2_dob": "",
        "loser_2_third_party_id": str(loser2.get("playerId", "")) if is_doubles else "",
        "loser_2_city": loser2.get("schoolCity", "") if is_doubles else "",
        "loser_2_state": loser2.get("schoolState", "") if is_doubles else "",
        "loser_2_country": "USA" if is_doubles else "",
        # Outcome / draw / tournament metadata
        "outcome": "Completed",
        # Items schema spells the draw gender out; player genders stay M/F.
        "draw_gender": _gender_long(sport_gender),
        "draw_bracket_type": "",
        "draw_type": "",
        "tournament_city": "",
        "tournament_state": "",
        "tournament_country_code": "USA",
        "tournament_host": "",
        "tournament_location_type": "",
        "tournament_surface": "",
        "tournament_event_category": "",
        "tournament_event_grade": "",
        "tournament_import_source": "NJ School Sports",
        "tournament_sanction_body": "NJ School Sports",
        "winner_2_college": "",
        "loser_2_college": "",
        "tournament_event_type": "Dual Match",
        "winner_1_college": "",
        "loser_1_college": "",
        "tournament_url": "",
        "tournament_country": "USA",
        "tournament_start_date": game_date,
        "tournament_end_date": game_date,
    }


def _parse_feed(results):
    """Walk a feed payload and return a list of CSV row dicts."""
    rows = []
    games = (results or {}).get("games", {}).get("games", []) or []
    for game in games:
        for match in game.get("matches", []) or []:
            rows.append(_build_row(game, match))
    return rows


def _scrape_day(client, api_key, job_date, log):
    """Fetch + parse both genders for one day; return a list of row dicts."""
    rows = []
    gamedate = job_date.strftime("%m/%d/%Y")
    for gender in GENDERS:
        params = {"key": api_key, "gender": gender, "gamedate": gamedate}
        results = client.get_json(FEED_URL, params=params)
        if not results:
            continue
        rows.extend(_parse_feed(results))
    return rows


def run(run_obj, log):
    """Execute the NJ high-school tennis scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    start_d = run_obj.date_from
    end_d = run_obj.date_to
    api_key = getattr(settings, "NJ_HS_API_KEY", NJ_HS_API_KEY)

    log(
        "INFO",
        f"\U0001f3be NJ high-school tennis starting \u2014 window "
        f"{start_d} \u2192 {end_d}",
    )
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    if not (start_d and end_d):
        msg = "NJ high-school tennis needs a date range (date_from / date_to)."
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    if start_d > end_d:
        start_d, end_d = end_d, start_d

    proxies = build_proxies(scraper, log)
    job_dates = list(_iter_dates(start_d, end_d))

    total = len(job_dates)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} day(s) to scrape (boys + girls each)")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def process(job_date):
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            rows = _scrape_day(client, api_key, job_date, log)
            for row in rows:
                # Dedup on the match id + players + score (mirrors the source's
                # existence check), so a match never lands twice.
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
                    f"   \U0001f3c6 {row.get('draw_team_type', '')}: "
                    f"{row.get('winner_1_name') or '?'} def. "
                    f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                    f"@ {row.get('tournament_name') or 'NJ high-school tennis'}",
                )
        except Exception as exc:  # noqa: BLE001 - one bad day can't kill the run
            tele.record_error(
                redact_secrets(f"Day {job_date} failed: {exc}"), exc=exc
            )
            log(
                "WARN",
                redact_secrets(f"\u26a0\ufe0f day failed: {exc.__class__.__name__}: {exc}"),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
            client.close()

    if job_dates:
        log("INFO", "\u2500\u2500\u2500\u2500 scraping days \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, job_dates))

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
