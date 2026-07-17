"""SportRadar Tennis daily summaries scraper.

Input is a date range. Scheduled runs default to yesterday through today via the
registry's ``default_range_days=1``. For each date the scraper calls:

    GET /tennis/{access}/v3/{language}/schedules/{date}/summaries.json

with the API key in the ``x-api-key`` header, filters to the requested Tennis
category IDs, and emits one standard MatchMiner match row per completed score.
DOBs come from embedded doubles players when present, otherwise from the
SportRadar competitor-profile endpoint and are cached per run.
"""

import csv
import io
from datetime import datetime, timedelta
from urllib.parse import quote

from django.conf import settings
from django.db.models import F

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

BASE_URL = "https://api.sportradar.com/tennis"
PAGE_LIMIT = 200
ALLOWED_HOSTS = ("api.sportradar.com",)

ALLOWED_CATEGORY_IDS = frozenset(
    {
        "sr:category:3",     # ATP
        "sr:category:74",    # Billie Jean King Cup
        "sr:category:72",    # Challenger
        "sr:category:76",    # Davis Cup
        "sr:category:79",    # Exhibition
        "sr:category:181",   # Hopman Cup
        "sr:category:785",   # ITF Men
        "sr:category:1474",  # Juniors
        "sr:category:2109",  # Other
        "sr:category:2414",  # United Cup
        "sr:category:6",     # WTA
        "sr:category:871",   # WTA 125K
    }
)

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


def _access_level():
    value = (getattr(settings, "SPORTRADAR_ACCESS_LEVEL", "") or "trial").strip()
    return value if value in {"trial", "production"} else "trial"


def _language_code():
    value = (getattr(settings, "SPORTRADAR_LANGUAGE_CODE", "") or "en").strip()
    return value if value.isalpha() and 2 <= len(value) <= 3 else "en"


def _api_key(run_obj):
    return (
        (getattr(run_obj.scraper, "secret_value", "") or "").strip()
        or (getattr(settings, "SPORTRADAR_API_KEY", "") or "").strip()
    )


def _daily_url(day):
    return (
        f"{BASE_URL}/{_access_level()}/v3/{_language_code()}"
        f"/schedules/{day.isoformat()}/summaries.json"
    )


def _profile_url(competitor_id):
    cid = quote(competitor_id or "", safe=":")
    return (
        f"{BASE_URL}/{_access_level()}/v3/{_language_code()}"
        f"/competitors/{cid}/profile.json"
    )


def _competition_url(competition_id):
    cid = quote(competition_id or "", safe=":")
    return (
        f"{BASE_URL}/{_access_level()}/v3/{_language_code()}"
        f"/competitions/{cid}/info.json"
    )


def _iter_dates(start_d, end_d):
    day = start_d
    while day <= end_d:
        yield day
        day += timedelta(days=1)


def _to_mdy(value):
    text = (value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text.replace("Z", "+0000"), fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return text


def _gender_short(value):
    text = (value or "").strip().lower()
    if text in {"m", "men", "man", "male"}:
        return "M"
    if text in {"f", "w", "women", "woman", "female"}:
        return "F"
    return ""


def _gender_long(value):
    code = _gender_short(value)
    if code == "M":
        return "Male"
    if code == "F":
        return "Female"
    return ""


def _country_value(*values):
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text and text.lower() != "neutral":
            return text
    return ""


def _draw_suffix(parent_name, child_name):
    parent = (parent_name or "").strip()
    child = (child_name or "").strip()
    if not parent or not child.startswith(parent):
        return ""
    return child[len(parent):].lstrip(" \t,-:").strip()


def _team_type(competition_type):
    text = (competition_type or "").strip().lower()
    return "Doubles" if "double" in text else "Singles"


def _first_group_name(context):
    groups = context.get("groups") or []
    if not groups:
        return ""
    group = groups[0] or {}
    return group.get("name") or group.get("group_name") or ""


def _competitor_ids(competitor):
    ids = {competitor.get("id", "")}
    for player in competitor.get("players") or []:
        ids.add((player or {}).get("id", ""))
    ids.discard("")
    return ids


def _side_by_qualifier(competitors, qualifier):
    for comp in competitors:
        if (comp.get("qualifier") or "").lower() == qualifier:
            return comp
    return None


def _winner_loser_competitors(competitors, status):
    winner_id = status.get("winner_id") or ""
    for comp in competitors:
        if winner_id and winner_id in _competitor_ids(comp):
            winner = comp
            loser = next((c for c in competitors if c is not comp), None)
            return winner, loser

    home = _side_by_qualifier(competitors, "home") or (competitors[0] if competitors else None)
    away = _side_by_qualifier(competitors, "away") or (competitors[1] if len(competitors) > 1 else None)
    try:
        home_score = int(status.get("home_score"))
        away_score = int(status.get("away_score"))
    except (TypeError, ValueError):
        return None, None
    if home and away and home_score != away_score:
        return (home, away) if home_score > away_score else (away, home)
    return None, None


def _qualifier(competitor):
    return ((competitor or {}).get("qualifier") or "").lower()


def _score(status, winner_qualifier):
    parts = []
    periods = sorted(
        status.get("period_scores") or [],
        key=lambda p: p.get("number") or 0,
    )
    for period in periods:
        home = period.get("home_score")
        away = period.get("away_score")
        if home is None or away is None:
            continue
        loser_tiebreak = None
        if home > away:
            loser_tiebreak = period.get("away_tiebreak_score")
        elif away > home:
            loser_tiebreak = period.get("home_tiebreak_score")
        first, second = (away, home) if winner_qualifier == "away" else (home, away)
        suffix = f"({loser_tiebreak})" if loser_tiebreak is not None else ""
        parts.append(f"{first}-{second}{suffix}")
    if not parts:
        home = status.get("home_score")
        away = status.get("away_score")
        if home is not None and away is not None:
            first, second = (away, home) if winner_qualifier == "away" else (home, away)
            parts.append(f"{first}-{second}")
    return ", ".join(parts) + ";" if parts else ""


def _outcome(status):
    reason = (status.get("winning_reason") or "").strip().lower()
    match_status = (status.get("match_status") or status.get("status") or "").strip().lower()
    text = reason or match_status
    if "retire" in text:
        return "Retired"
    if "walkover" in text:
        return "Walkover"
    if "default" in text:
        return "Defaulted"
    if match_status in {"ended", "closed", "not_started", "live"}:
        return "Completed" if match_status in {"ended", "closed"} else match_status.replace("_", " ").title()
    return match_status.replace("_", " ").title() if match_status else "Completed"


def _side_people(competitor, team_type):
    players = [p for p in (competitor.get("players") or []) if isinstance(p, dict)]
    if players:
        return players
    name = competitor.get("name", "") or ""
    if team_type == "Doubles" and " / " in name:
        return [
            {
                "name": part.strip(),
                "country": competitor.get("country", ""),
                "country_code": competitor.get("country_code", ""),
                "gender": competitor.get("gender", ""),
                "id": "",
            }
            for part in name.split(" / ")[:2]
            if part.strip()
        ]
    return [competitor]


def _fetch_json(client, api_key, url, *, params=None):
    resp = client.get(
        url,
        headers={"Accept": "application/json", "x-api-key": api_key},
        params=params,
        retry_statuses={408, 425, 429, 500, 502, 503, 504},
    )
    if resp is None:
        return None
    if not (200 <= resp.status_code < 300):
        client.tele.record_error(f"SportRadar HTTP {resp.status_code} for {url}")
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        client.tele.record_error(f"SportRadar response was not valid JSON for {url}", exc=exc)
        return None


def _competition_metadata(client, api_key, competition, cache):
    parent_id = (competition.get("parent_id") or "").strip()
    if not parent_id:
        return None

    if parent_id not in cache:
        data = _fetch_json(client, api_key, _competition_url(parent_id))
        parent = data.get("competition") if isinstance(data, dict) else None
        if not isinstance(parent, dict) or not (parent.get("name") or "").strip():
            if data is not None:
                client.tele.record_error(
                    f"SportRadar Competition Info payload was malformed for {parent_id}"
                )
            parent = None
        cache[parent_id] = parent

    parent = cache[parent_id]
    if parent is None:
        return None

    children = parent.get("children") or []
    if not isinstance(children, list):
        children = []
    child = None
    for entry in children:
        candidate = entry.get("competition") if isinstance(entry, dict) else None
        if isinstance(candidate, dict) and candidate.get("id") == competition.get("id"):
            child = candidate
            break
    return {"parent": parent, "child": child or {}}


def _fetch_daily_summaries(client, api_key, day):
    summaries = []
    start = 0
    while True:
        data = _fetch_json(
            client,
            api_key,
            _daily_url(day),
            params={"start": str(start), "limit": str(PAGE_LIMIT)},
        )
        if data is None:
            return None
        batch = data.get("summaries") or []
        if not isinstance(batch, list):
            client.tele.record_error(f"SportRadar summaries payload was not a list for {day}")
            return None
        summaries.extend(batch)
        if len(batch) < PAGE_LIMIT:
            return summaries
        start += PAGE_LIMIT


def _profile_dob(client, api_key, competitor_id, dob_cache):
    if not competitor_id:
        return ""
    if competitor_id in dob_cache:
        return dob_cache[competitor_id]
    data = _fetch_json(client, api_key, _profile_url(competitor_id))
    dob = ""
    if data:
        dob = _to_mdy((data.get("info") or {}).get("date_of_birth", ""))
    dob_cache[competitor_id] = dob
    return dob


def _person_fields(person, fallback_competitor, fallback_gender, client, api_key, dob_cache):
    cid = person.get("id") or ""
    dob = _to_mdy(person.get("date_of_birth", ""))
    if not dob and client is not None and api_key:
        dob = _profile_dob(client, api_key, cid, dob_cache)
    gender = _gender_short(person.get("gender", "")) or fallback_gender
    return {
        "name": person.get("name", "") or "",
        "gender": gender,
        "dob": dob,
        "id": cid,
        "country": _country_value(
            person.get("country_code"),
            person.get("country"),
            fallback_competitor.get("country_code"),
            fallback_competitor.get("country"),
        ),
    }


def _row_from_summary(
    summary,
    *,
    client=None,
    api_key="",
    dob_cache=None,
    competition_metadata=None,
):
    dob_cache = dob_cache if dob_cache is not None else {}
    sport_event = summary.get("sport_event") or {}
    status = summary.get("sport_event_status") or {}
    context = sport_event.get("sport_event_context") or {}
    category = context.get("category") or {}
    if category.get("id") not in ALLOWED_CATEGORY_IDS:
        return None

    competition = context.get("competition") or {}
    metadata = competition_metadata if isinstance(competition_metadata, dict) else {}
    parent_competition = metadata.get("parent") or {}
    child_competition = metadata.get("child") or {}
    competition_type = child_competition.get("type") or competition.get("type") or ""
    competition_gender = child_competition.get("gender") or competition.get("gender") or ""
    team_type = _team_type(competition_type)
    fallback_gender = _gender_short(competition_gender)

    competitors = [c for c in (sport_event.get("competitors") or []) if isinstance(c, dict)]
    if len(competitors) < 2:
        return None
    winner_comp, loser_comp = _winner_loser_competitors(competitors, status)
    if not (winner_comp and loser_comp):
        return None

    winner_people = _side_people(winner_comp, team_type)
    loser_people = _side_people(loser_comp, team_type)
    if not (winner_people and loser_people):
        return None

    w1 = _person_fields(winner_people[0], winner_comp, fallback_gender, client, api_key, dob_cache)
    w2 = (
        _person_fields(winner_people[1], winner_comp, fallback_gender, client, api_key, dob_cache)
        if team_type == "Doubles" and len(winner_people) > 1
        else None
    )
    l1 = _person_fields(loser_people[0], loser_comp, fallback_gender, client, api_key, dob_cache)
    l2 = (
        _person_fields(loser_people[1], loser_comp, fallback_gender, client, api_key, dob_cache)
        if team_type == "Doubles" and len(loser_people) > 1
        else None
    )

    venue = sport_event.get("venue") or {}
    season = context.get("season") or {}
    round_info = context.get("round") or {}
    fallback_draw_name = _first_group_name(context) or competition.get("name", "")
    parent_name = (parent_competition.get("name") or "").strip()
    child_name = child_competition.get("name") or competition.get("name") or ""
    draw_name = _draw_suffix(parent_name, child_name) or fallback_draw_name
    tournament_name = parent_name or competition.get("name", "")

    return {
        "match_id": "",
        "ball_type": "Yellow",
        "id_type": "SportRadar",
        "draw_bracket_value": "",
        "draw_name": draw_name,
        "draw_team_type": team_type,
        "tournament_name": tournament_name,
        "date": _to_mdy(sport_event.get("start_time", "")),
        "round": round_info.get("name", ""),
        "score": _score(status, _qualifier(winner_comp)),
        "winner_1_name": w1["name"],
        "winner_1_gender": w1["gender"],
        "winner_1_dob": w1["dob"],
        "winner_1_third_party_id": w1["id"],
        "winner_1_city": "",
        "winner_1_state": "",
        "winner_1_country": w1["country"],
        "winner_2_name": w2["name"] if w2 else "",
        "winner_2_gender": w2["gender"] if w2 else "",
        "winner_2_dob": w2["dob"] if w2 else "",
        "winner_2_third_party_id": w2["id"] if w2 else "",
        "winner_2_city": "",
        "winner_2_state": "",
        "winner_2_country": w2["country"] if w2 else "",
        "loser_1_name": l1["name"],
        "loser_1_gender": l1["gender"],
        "loser_1_dob": l1["dob"],
        "loser_1_third_party_id": l1["id"],
        "loser_1_city": "",
        "loser_1_state": "",
        "loser_1_country": l1["country"],
        "loser_2_name": l2["name"] if l2 else "",
        "loser_2_gender": l2["gender"] if l2 else "",
        "loser_2_dob": l2["dob"] if l2 else "",
        "loser_2_third_party_id": l2["id"] if l2 else "",
        "loser_2_city": "",
        "loser_2_state": "",
        "loser_2_country": l2["country"] if l2 else "",
        "outcome": _outcome(status),
        "draw_gender": _gender_long(competition_gender),
        "draw_bracket_type": "",
        "draw_type": "",
        "tournament_city": venue.get("city_name", ""),
        "tournament_state": "",
        "tournament_country_code": venue.get("country_code", ""),
        "tournament_host": "",
        "tournament_location_type": "",
        "tournament_surface": "",
        "tournament_event_category": "",
        "tournament_event_grade": "",
        "tournament_import_source": "SportRadar",
        "tournament_sanction_body": "SportRadar",
        "winner_2_college": "",
        "loser_2_college": "",
        "tournament_event_type": "Tournament",
        "winner_1_college": "",
        "loser_1_college": "",
        "tournament_url": "",
        "tournament_country": venue.get("country_name", ""),
        "tournament_start_date": _to_mdy(season.get("start_date", "")),
        "tournament_end_date": _to_mdy(season.get("end_date", "")),
    }


def run(run_obj, log):
    tele = Telemetry()
    scraper = run_obj.scraper
    params = run_obj.params or {}
    start_d = run_obj.date_from
    end_d = run_obj.date_to

    log("INFO", f"SportRadar Tennis starting - window {start_d} -> {end_d}")

    if not (start_d and end_d):
        msg = "SportRadar needs a date range (date_from / date_to)."
        tele.record_error(msg)
        log("ERROR", msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED
    if start_d > end_d:
        start_d, end_d = end_d, start_d

    api_key = _api_key(run_obj)
    if not api_key:
        msg = (
            "SportRadar API key required. Add it on this scraper's Settings tab "
            "or set SPORTRADAR_API_KEY."
        )
        tele.record_error(msg)
        log("ERROR", msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    dates = list(_iter_dates(start_d, end_d))
    Run.objects.filter(pk=run_obj.pk).update(progress_total=len(dates), progress_done=0)
    log("INFO", f"{len(dates)} daily summary request(s) queued")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    seen = set()
    dob_cache = {}
    competition_cache = {}
    row_count = 0
    failed_days = 0
    proxies = build_proxies(scraper, log)

    try:
        with ScraperClient(
            log=log,
            tele=tele,
            proxies=proxies,
            allowed_hosts=ALLOWED_HOSTS,
            headers={"Accept": "application/json"},
        ) as client:
            for day in dates:
                summaries = _fetch_daily_summaries(client, api_key, day)
                if summaries is None:
                    failed_days += 1
                    Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
                    continue
                log("INFO", f"{day}: {len(summaries)} summaries received")
                for summary in summaries:
                    context = summary.get("sport_event", {}).get("sport_event_context") or {}
                    if (context.get("category") or {}).get("id") not in ALLOWED_CATEGORY_IDS:
                        continue
                    competition_metadata = _competition_metadata(
                        client,
                        api_key,
                        context.get("competition") or {},
                        competition_cache,
                    )
                    row = _row_from_summary(
                        summary,
                        client=client,
                        api_key=api_key,
                        dob_cache=dob_cache,
                        competition_metadata=competition_metadata,
                    )
                    if not row:
                        continue
                    key = (
                        summary.get("sport_event", {}).get("id", ""),
                        row.get("winner_1_third_party_id", ""),
                        row.get("loser_1_third_party_id", ""),
                        row.get("score", ""),
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
                    row_count += 1
                Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
    except Exception as exc:  # noqa: BLE001
        tele.record_error(redact_secrets(f"SportRadar run failed: {exc}"), exc=exc)
        log("ERROR", redact_secrets(f"SportRadar run failed: {exc.__class__.__name__}: {exc}"))
        return "", tele.requests_csv(), tele.errors_csv(), row_count, Run.Status.FAILED

    if failed_days == len(dates):
        status = Run.Status.FAILED
    elif failed_days:
        status = Run.Status.PARTIAL
    else:
        status = Run.Status.SUCCESS
    log("INFO", f"SportRadar finished - status={status}, rows={row_count}")
    return buf.getvalue(), tele.requests_csv(), tele.errors_csv(), row_count, status
