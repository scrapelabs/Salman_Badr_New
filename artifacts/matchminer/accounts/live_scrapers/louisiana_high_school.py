"""Louisiana high-school tennis (LHSAA) scraper.

The LHSAA source is a flat cumulative JSON feed. A run sends the requested range
start as ``updatedafter``, then locally keeps matches whose played ``Date`` falls
inside the inclusive requested range.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import io
from datetime import datetime

from django.conf import settings
from django.db.models import F

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

FEED_URL = "https://lhsaaonline.org/feeds/lHSAATennisData.ashx"
DEFAULT_LHSAA_API_KEY = "7761eaf8-774a-40c3-950f-c2698a362e35"
GENDERS = ("boys", "girls")

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

_HEADER_ACRONYMS = {"id": "ID", "dob": "DOB", "url": "URL"}
HEADER = [
    " ".join(_HEADER_ACRONYMS.get(part, part.capitalize()) for part in col.split("_"))
    for col in COLUMNS
]


def _date_param(day):
    # LHSAA uses the U.S. month/day/year format for query and response dates.
    return f"{day.month}/{day.day}/{day.year}"


def _parse_date(value):
    raw = (value or "").strip()
    if not raw:
        return ""
    for fmt in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def _parse_last_updated(value):
    raw = (value or "").strip()
    if not raw:
        return None
    for fmt in (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%y %I:%M:%S %p",
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _gender_long(value):
    raw = (value or "").strip()
    normalized = raw.lower()
    if normalized in ("m", "male", "boy", "boys"):
        return "Male"
    if normalized in ("f", "female", "girl", "girls"):
        return "Female"
    return raw


def _gender_short(value):
    raw = (value or "").strip()
    normalized = raw.lower()
    if normalized in ("m", "male", "boy", "boys"):
        return "M"
    if normalized in ("f", "female", "girl", "girls"):
        return "F"
    return raw


def _normalize_score(value):
    raw = str(value or "").strip().rstrip(";").strip()
    if not raw:
        return ""
    return f"{', '.join(part.strip() for part in raw.split(','))};"


def _genders_for(value):
    gender = (value or "both").strip().lower()
    if gender == "boys":
        return ("boys",)
    if gender == "girls":
        return ("girls",)
    return GENDERS


def _is_doubles(record):
    return any(
        (record.get(field) or "").strip()
        for field in (
            "Winner2Name",
            "Winner2PlayerID",
            "Loser2Name",
            "Loser2PlayerID",
        )
    )


def _team_type(record):
    value = (record.get("DrawTeamType") or "").strip().title()
    if value in ("Singles", "Doubles"):
        return value
    return "Doubles" if _is_doubles(record) else "Singles"


def _row_from_record(record):
    is_doubles = _team_type(record) == "Doubles"
    draw_name = (record.get("DrawName") or "").strip()
    match_date = _parse_date(record.get("Date"))
    tournament_start = _parse_date(record.get("TournamentStartDate")) or match_date
    tournament_end = _parse_date(record.get("TournamentEndDate")) or match_date

    return {
        "match_id": str(record.get("GameUniqueID", "") or ""),
        "ball_type": "Yellow",
        "id_type": "Louisiana HS",
        "draw_bracket_value": "",
        "draw_name": draw_name,
        "draw_team_type": _team_type(record),
        "tournament_name": record.get("TournamentName", ""),
        "date": match_date,
        "round": "",
        "score": _normalize_score(record.get("Score")),
        "winner_1_name": record.get("Winner1Name", ""),
        "winner_1_gender": _gender_short(record.get("Winner1Gender")),
        "winner_1_dob": record.get("Winner1DOB", ""),
        "winner_1_third_party_id": str(record.get("Winner1PlayerID", "") or ""),
        "winner_1_city": record.get("Winner1City", ""),
        "winner_1_state": record.get("Winner1State", ""),
        "winner_1_country": record.get("Winner1Country", ""),
        "winner_2_name": record.get("Winner2Name", "") if is_doubles else "",
        "winner_2_gender": _gender_short(record.get("Winner2Gender")) if is_doubles else "",
        "winner_2_dob": record.get("Winner2DOB", "") if is_doubles else "",
        "winner_2_third_party_id": str(record.get("Winner2PlayerID", "") or "") if is_doubles else "",
        "winner_2_city": record.get("Winner2City", "") if is_doubles else "",
        "winner_2_state": record.get("Winner2State", "") if is_doubles else "",
        "winner_2_country": record.get("Winner2Country", "") if is_doubles else "",
        "loser_1_name": record.get("Loser1Name", ""),
        "loser_1_gender": _gender_short(record.get("Loser1Gender")),
        "loser_1_dob": record.get("Loser1DOB", ""),
        "loser_1_third_party_id": str(record.get("Loser1PlayerID", "") or ""),
        "loser_1_city": record.get("Loser1City", ""),
        "loser_1_state": record.get("Loser1State", ""),
        "loser_1_country": record.get("Loser1Country", ""),
        "loser_2_name": record.get("Loser2Name", "") if is_doubles else "",
        "loser_2_gender": _gender_short(record.get("Loser2Gender")) if is_doubles else "",
        "loser_2_dob": record.get("Loser2DOB", "") if is_doubles else "",
        "loser_2_third_party_id": str(record.get("Loser2PlayerID", "") or "") if is_doubles else "",
        "loser_2_city": record.get("Loser2City", "") if is_doubles else "",
        "loser_2_state": record.get("Loser2State", "") if is_doubles else "",
        "loser_2_country": record.get("Loser2Country", "") if is_doubles else "",
        "outcome": "Completed",
        "draw_gender": _gender_long(draw_name or record.get("Winner1Gender")),
        "draw_bracket_type": "",
        "draw_type": "",
        "tournament_city": record.get("TournamentCity", ""),
        "tournament_state": "LA",
        "tournament_country_code": "USA",
        "tournament_host": "",
        "tournament_location_type": "",
        "tournament_surface": "",
        "tournament_event_category": "",
        "tournament_event_grade": "",
        "tournament_import_source": "LHSAA",
        "tournament_sanction_body": "LHSAA",
        "winner_2_college": "",
        "loser_2_college": "",
        "tournament_event_type": record.get("EventType", ""),
        "winner_1_college": "",
        "loser_1_college": "",
        "tournament_url": FEED_URL,
        "tournament_country": "USA",
        "tournament_start_date": tournament_start,
        "tournament_end_date": tournament_end,
    }


def _parse_feed(records, *, updated_from, updated_to):
    rows = []
    for record in records or []:
        played_date = _parse_last_updated(record.get("Date"))
        if played_date is None or not (updated_from <= played_date <= updated_to):
            continue
        rows.append(_row_from_record(record))
    return rows


def _api_key(scraper):
    return (
        (getattr(scraper, "secret_value", "") or "").strip()
        or (getattr(settings, "LHSAA_API_KEY", "") or "").strip()
        or DEFAULT_LHSAA_API_KEY
    )


def _fetch_gender(client, api_key, gender, updated_from, updated_to):
    payload = client.get_json(
        FEED_URL,
        params={
            "apikey": api_key,
            "gender": gender,
            "updatedafter": _date_param(updated_from),
        },
    )
    return _parse_feed(payload if isinstance(payload, list) else [], updated_from=updated_from, updated_to=updated_to)


def run(run_obj, log):
    tele = Telemetry()
    scraper = run_obj.scraper
    start_d = run_obj.date_from
    end_d = run_obj.date_to
    params = run_obj.params or {}
    genders = _genders_for(params.get("gender"))

    log("INFO", f"🎾 LHSAA tennis starting — played dates {start_d} → {end_d}")
    log("INFO", f"👥 Gender(s): {', '.join(genders)}")

    if not (start_d and end_d):
        msg = "LHSAA tennis needs a date range (date_from / date_to)."
        log("ERROR", f"🛑 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    if start_d > end_d:
        start_d, end_d = end_d, start_d

    api_key = _api_key(scraper)
    proxies = build_proxies(scraper, log)
    Run.objects.filter(pk=run_obj.pk).update(
        progress_total=len(genders),
        progress_done=0,
    )
    log("INFO", f"📅 Cumulative feed from {_date_param(start_d)}")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    seen = set()
    row_count = 0

    with ScraperClient(log=log, tele=tele, proxies=proxies) as client:
        for gender in genders:
            try:
                rows = _fetch_gender(client, api_key, gender, start_d, end_d)
                for row in rows:
                    key = row.get("match_id") or (
                        row.get("date", ""),
                        row.get("winner_1_name", ""),
                        row.get("loser_1_name", ""),
                        row.get("score", ""),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
                    row_count += 1
                    log(
                        "INFO",
                        f"   🏆 {row.get('draw_team_type', '')}: "
                        f"{row.get('winner_1_name') or '?'} def. "
                        f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                        f"@ {row.get('tournament_name') or 'LHSAA tennis'}",
                    )
            except Exception as exc:  # noqa: BLE001 - one gender feed should not kill the run
                context = f"{gender} feed failed: {exc}"
                tele.record_error(redact_secrets(context), exc=exc)
                log(
                    "WARN",
                    redact_secrets(
                        f"⚠️ feed failed: {exc.__class__.__name__}: {exc}"
                    ),
                )
            finally:
                Run.objects.filter(pk=run_obj.pk).update(
                    progress_done=F("progress_done") + 1
                )

    log("INFO", "──── summary ────")
    log("INFO", f"💾 Writing {row_count} row(s) to CSV")
    log("INFO", f"📊 Telemetry: {tele.request_count} request(s), {tele.error_count} error(s)")
    clean_empty = row_count == 0 and tele.error_count == 0
    status = Run.Status.SUCCESS if row_count or clean_empty else Run.Status.FAILED
    icon = "🏁" if status == Run.Status.SUCCESS else "🛑"
    log("INFO", f"{icon} Run finished — status={status}, rows={row_count}")
    items_csv = buf.getvalue() if row_count or clean_empty else ""
    return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
