"""Shared scraper for ITF / Stadion team-competition data.

The Billie Jean King Cup (Fed Cup) and sibling team competitions (e.g. the
Davis Cup) are all served by the same public ITF / Stadion data API
(``api.itf-production.sports-data.stadion.io``), with the same JSON shape and
the same 60-column ITF-style item schema. This module ports that production
spider using ``curl_cffi`` with Chrome impersonation (matching the production
``use_cffi=True`` client) and optional proxy routing — no ``rich`` or
``pandas`` — and is parameterised by a small :class:`StadionConfig`, so each
competition is a thin wrapper. Currently only ``billiejeankingcup.py`` is
wired; others can be re-added later as more thin wrappers.

Note: the upstream ITF / Stadion API sits behind CloudFront, which blocks
cloud / datacenter IPs (including Replit's) with a 403 ("Request blocked").
A residential proxy must be assigned to the scraper (Lab → Settings tab) for
the scrape to reach the origin — exactly as the production framework routes
through ``settings.PROXIES`` with ``use_proxy=True``.

Every HTTP call and every failure is recorded through a :class:`Telemetry`
instance so each run can export ``requests`` / ``errors`` CSVs next to the
items CSV, matching the formats produced by the original framework.

``run(config, run_obj, log)`` returns
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

import csv
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from curl_cffi import requests as cffi_requests
from django.utils import timezone

from accounts.models import Run

from .telemetry import Telemetry, redact_secrets, sanitize_cell

API = "https://api.itf-production.sports-data.stadion.io"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36 Edg/139.0.0.0"
)
# curl_cffi browser fingerprint to impersonate (production uses use_cffi=True).
IMPERSONATE = "chrome"

# Ties are fetched concurrently, matching the production spider's behaviour, so
# a full season's worth of ties is processed without an artificial cap.
MAX_WORKERS = 8

# Long ?include= expression copied from the production spider.
_MATCH_INCLUDE = (
    "sides.sidePlayer.player.person.country.images,"
    "sides.sidePlayer.player.person.images,sides.sideSets.set,matchStatus,"
    "scoringType,court,round.draw.event.venue.country,"
    "round.draw.event.surface,round.draw.event.discipline,"
    "round.draw.event.eventCategory.images,"
    "round.draw.event.venue.country.images,round.draw.discipline,"
    "tie.teams.country,tie.tieStatus,tie.teams.country.images,"
    "tie.round.draw.event,tie.round.draw.event.surface,"
    "tie.round.draw.event.venue.country.images"
)

COLUMNS = [
    "tie_id", "match_id", "date",
    "draw_bracket_type", "draw_bracket_value", "draw_gender", "draw_name",
    "draw_size", "draw_team_type", "draw_type", "id_type",
    "loser_1_city", "loser_1_college", "loser_1_country", "loser_1_dob",
    "loser_1_gender", "loser_1_name", "loser_1_state", "loser_1_third_party_id",
    "loser_2_city", "loser_2_college", "loser_2_country", "loser_2_dob",
    "loser_2_gender", "loser_2_name", "loser_2_state", "loser_2_third_party_id",
    "outcome", "score",
    "tournament_city", "tournament_country", "tournament_country_code",
    "tournament_end_date", "tournament_event_category", "tournament_event_grade",
    "tournament_event_type", "tournament_host", "tournament_import_source",
    "tournament_location_type", "tournament_name", "tournament_sanction_body",
    "tournament_start_date", "tournament_state", "tournament_surface",
    "tournament_url",
    "winner_1_city", "winner_1_college", "winner_1_country", "winner_1_dob",
    "winner_1_gender", "winner_1_name", "winner_1_state",
    "winner_1_third_party_id",
    "winner_2_city", "winner_2_college", "winner_2_country", "winner_2_dob",
    "winner_2_gender", "winner_2_name", "winner_2_state",
    "winner_2_third_party_id",
]

# Title-cased header, matching the framework's items CSV (e.g. "Tie Id").
HEADER = [c.replace("_", " ").title() for c in COLUMNS]


@dataclass(frozen=True)
class StadionConfig:
    """Per-competition differences over the shared Stadion logic."""

    label: str           # human label for logs
    draw_code: str       # "bjkc" (BJK Cup) / "dc" (Davis Cup)
    id_type: str         # "Fedcup" / "DavisCup"
    gender_full: str     # "Female" / "Male"
    gender_short: str    # "F" / "M"
    url_builder: Callable[[str, str], str]  # (tie_id, match_id) -> tournament_url


def _build_proxies(scraper, log):
    """Return a curl_cffi ``proxies`` dict honouring the scraper's selected proxy.

    A proxy with a non-empty address routes traffic through it; otherwise the
    scraper connects directly (``None``). The address (which may carry
    credentials) is never logged — only the pool's name and type.
    """
    proxy = getattr(scraper, "proxy", None)
    if proxy and proxy.is_active and (proxy.address or "").strip():
        addr = proxy.address.strip()
        if "://" not in addr:
            addr = "http://" + addr
        log(
            "INFO",
            f"\U0001f50c HTTP client: curl_cffi (impersonate {IMPERSONATE}) via "
            f"{proxy.get_kind_display()} proxy '{proxy.name}'",
        )
        return {"http": addr, "https": addr}
    if proxy and proxy.is_active:
        log(
            "WARN",
            f"\u26a0\ufe0f Proxy '{proxy.name}' ({proxy.get_kind_display()}) "
            "selected but has no address \u2014 using direct connection",
        )
    else:
        log(
            "INFO",
            f"\U0001f50c HTTP client: curl_cffi (impersonate {IMPERSONATE}, "
            "direct \u2014 no proxy)",
        )
    return None


def _get_json(url, log, tele, proxies, tries=3, timeout=25):
    """GET ``url`` as JSON via curl_cffi, recording each attempt into ``tele``."""
    last_exc = None
    for attempt in range(1, tries + 1):
        start = time.time()
        try:
            # A fresh session per call (curl_cffi sessions are not safe to share
            # across the worker threads). trust_env=False makes "direct" (no
            # proxy) authoritative — it must not silently pick up HTTP(S)_PROXY
            # env vars; per-scraper routing is the only source of truth.
            session = cffi_requests.Session(trust_env=False)
            try:
                resp = session.get(
                    url,
                    headers={"User-Agent": UA, "Accept": "application/json"},
                    impersonate=IMPERSONATE,
                    proxies=proxies,
                    timeout=timeout,
                )
                status = resp.status_code
                body = resp.content
            finally:
                session.close()
            tele.record_request(
                url=url, method="GET", status=status, size=len(body),
                duration_ms=(time.time() - start) * 1000,
            )
            if 200 <= status < 300:
                return resp.json()
            log(
                "WARN",
                f"\u26a0\ufe0f GET {url} \u2192 HTTP {status} (retry {attempt}/{tries})",
            )
            last_exc = RuntimeError(f"HTTP {status}")
        except Exception as exc:  # noqa: BLE001 - log, record and retry
            tele.record_request(
                url=url, method="GET", status=None, size=0,
                duration_ms=(time.time() - start) * 1000,
            )
            log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f GET {url} \u2192 {exc.__class__.__name__}: "
                    f"{exc} (retry {attempt}/{tries})"
                ),
            )
            last_exc = exc
        time.sleep(min(2 * attempt, 6))
    if last_exc is not None:
        tele.record_error(f"Request failed for {url}: {last_exc}", exc=last_exc)
    else:
        tele.record_error(
            f"Request failed for {url}: no successful response after {tries} tries"
        )
    return None


def _years(run):
    if run.date_from and run.date_to:
        return list(range(run.date_from.year, run.date_to.year + 1))
    if run.date_from:
        return [run.date_from.year]
    return [timezone.localdate().year]


def _parse_dates(date_str):
    """Return the start date (YYYY-MM-DD) from an ITF formatted date string."""
    try:
        date_str = (date_str or "").strip()
        if not date_str:
            return ""
        if " - " in date_str:
            parts = date_str.split(" - ")
            start_parts = parts[0].strip().split()
            if len(start_parts) == 2:  # cross-month "31 January - 28 February 2025"
                year = parts[1].strip().split()[-1]
                return datetime.strptime(
                    f"{parts[0].strip()} {year}", "%d %B %Y"
                ).strftime("%Y-%m-%d")
            month_year = parts[1].split(" ", 1)[1]  # "16 - 21 June 2025"
            return datetime.strptime(
                f"{parts[0].strip()} {month_year}", "%d %B %Y"
            ).strftime("%Y-%m-%d")
        if len(date_str.split()) == 4:  # "31 January February 2025"
            day, month1, _month2, year = date_str.split()
            return datetime.strptime(f"{day} {month1} {year}", "%d %B %Y").strftime(
                "%Y-%m-%d"
            )
        return datetime.strptime(date_str, "%d %B %Y").strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def _conv_date(value):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%m/%d/%Y")
        except (ValueError, TypeError):
            continue
    return ""


def _extract_player(side_player):
    person = (side_player or {}).get("player", {}).get("person", {}) or {}
    country = person.get("country") or {}
    return {
        "first_name": (person.get("firstName") or "").title(),
        "last_name": (person.get("lastName") or "").title(),
        "country": country.get("name", ""),
        "third_party_id": (side_player or {}).get("playerId", ""),
    }


def _winner_loser(match_data):
    try:
        sides = match_data["data"]["sides"]
        winner_side_id = match_data["data"]["winnerSideId"]
        winner_side = loser_side = None
        for side in sides:
            if side["id"] == winner_side_id:
                winner_side = side
            else:
                loser_side = side
        return winner_side, loser_side
    except Exception:  # noqa: BLE001
        return None, None


def _match_score(match):
    try:
        winner_id = match.get("winnerSideId")
        if not winner_id:
            return ""
        sets = {}
        for side in match.get("sides", []):
            sid = side.get("id")
            for ss in side.get("sideSets") or []:
                sn = ss.get("setNumber")
                score = ss.get("setScore")
                if sn is None or score is None:
                    continue
                tb = ss.get("setTieBreakScore")
                sets.setdefault(int(sn), {})[sid] = (
                    int(score), int(tb) if tb is not None else None
                )
        if not sets:
            return ""
        pieces = []
        for sn in sorted(sets):
            scores = sets[sn]
            if winner_id not in scores or len(scores) < 2:
                continue
            my_score, my_tb = scores[winner_id]
            opp_id = next(s for s in scores if s != winner_id)
            opp_score, opp_tb = scores[opp_id]
            part = f"{my_score}-{opp_score}"
            tb_played = (my_tb or 0) > 0 or (opp_tb or 0) > 0
            if tb_played and (
                (my_score == 7 and opp_score == 6)
                or (my_score == 6 and opp_score == 7)
            ):
                loser_tb = opp_tb if my_score > opp_score else my_tb
                if loser_tb is not None and loser_tb > 0:
                    part += f"({loser_tb})"
            pieces.append(part)
        return ", ".join(pieces) + ";"
    except Exception:  # noqa: BLE001
        return ""


def _build_row(config, tie_id, tie_date, match_id, score, log, tele, proxies):
    link = f"{API}/match/{match_id}?include={_MATCH_INCLUDE}"
    results = _get_json(link, log, tele, proxies)
    if not results:
        return None
    data = results.get("data", {}) or {}

    row = {c: "" for c in COLUMNS}
    row.update({
        "tie_id": tie_id,
        "match_id": match_id,
        "date": tie_date,
        "draw_bracket_type": "Age",
        "draw_bracket_value": "Open",
        "draw_gender": config.gender_full,
        "draw_name": f"Match {data.get('orderInRound', '')}" if data.get("orderInRound", "") else "",
        "id_type": config.id_type,
        "score": score,
        "tournament_event_category": "Pro Circuit",
        "tournament_event_type": "Team Match",
        "tournament_location_type": "Outdoor",
        "tournament_sanction_body": "ITF",
        "tournament_url": config.url_builder(tie_id, match_id),
        "outcome": "",
    })

    try:
        row["draw_type"] = data.get("tie", {}).get("round", {}).get("name", "")
    except Exception:  # noqa: BLE001
        pass

    event = data.get("round", {}).get("draw", {}).get("event", {}) or {}
    venue = event.get("venue", {}) or {}
    country = venue.get("country", {}) or {}
    try:
        row["tournament_city"] = (venue.get("city", "") or "").split(",")[0].strip()
    except Exception:  # noqa: BLE001
        pass
    row["tournament_country"] = country.get("name", "")
    row["tournament_country_code"] = country.get("ISOcode", "")
    row["tournament_host"] = venue.get("_name", "")
    row["tournament_start_date"] = _conv_date(event.get("startDate", ""))
    row["tournament_end_date"] = _conv_date(event.get("endDate", ""))
    try:
        teams = data.get("tie", {}).get("teams", []) or []
        names = [
            t["country"]["name"]
            for t in sorted(teams, key=lambda x: x.get("teamOrder", 0))
        ]
        base = data.get("tie", {}).get("round", {}).get("draw", {}).get("event", {}).get("name", "")
        if base:
            row["tournament_name"] = base + " - " + " vs ".join(map(str, names))
    except Exception:  # noqa: BLE001
        pass

    winners, losers = _winner_loser(results)
    if not (winners and losers):
        return None

    win_info = [_extract_player(sp) for sp in winners.get("sidePlayer", [])]
    lose_info = [_extract_player(sp) for sp in losers.get("sidePlayer", [])]
    row["draw_team_type"] = "Doubles" if len(win_info) > 1 else "Singles"
    g = config.gender_short

    def name(info):
        return f"{info['last_name']}, {info['first_name']}".strip(", ")

    if win_info:
        row.update({
            "winner_1_name": name(win_info[0]),
            "winner_1_country": win_info[0]["country"],
            "winner_1_gender": g,
            "winner_1_third_party_id": win_info[0]["third_party_id"],
        })
    if len(win_info) > 1:
        row.update({
            "winner_2_name": name(win_info[1]),
            "winner_2_country": win_info[1]["country"],
            "winner_2_gender": g,
            "winner_2_third_party_id": win_info[1]["third_party_id"],
        })
    if lose_info:
        row.update({
            "loser_1_name": name(lose_info[0]),
            "loser_1_country": lose_info[0]["country"],
            "loser_1_gender": g,
            "loser_1_third_party_id": lose_info[0]["third_party_id"],
        })
    if len(lose_info) > 1:
        row.update({
            "loser_2_name": name(lose_info[1]),
            "loser_2_country": lose_info[1]["country"],
            "loser_2_gender": g,
            "loser_2_third_party_id": lose_info[1]["third_party_id"],
        })
    return row


def run(config, run_obj, log):
    """Execute the scrape for ``config``.

    Returns ``(items_csv, requests_csv, errors_csv, row_count, status)``.
    """
    tele = Telemetry()
    years = _years(run_obj)
    log("INFO", f"\U0001f3be {config.label} scraper starting \u2014 ranking years={years}")
    proxies = _build_proxies(run_obj.scraper, log)

    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering ties \u2500\u2500\u2500\u2500")
    ties = []
    seen = set()
    for year in years:
        index_link = f"{API}/custom/wcotDrawsModeled/{config.draw_code}/{year}"
        log("INFO", f"\U0001f310 GET {index_link}")
        data = _get_json(index_link, log, tele, proxies)
        if not data:
            log("WARN", f"\u26a0\ufe0f No draw data returned for {year}")
            continue
        before = len(ties)
        for entry in data.get("data", []):
            for event in entry.get("events", []):
                for draw in event.get("draws", []):
                    contents = draw.get("content")
                    bucket = []
                    if isinstance(contents, list):
                        for content in contents:
                            bucket.extend(content.get("ties", []))
                    elif isinstance(contents, dict):
                        bucket.extend(contents.get("recent", []))
                    for tie in bucket:
                        tid = tie.get("id", "")
                        if tid and tid not in seen:
                            seen.add(tid)
                            ties.append(
                                (tid, _parse_dates(tie.get("formattedDate", "")))
                            )
        log("INFO", f"\U0001f50e {year}: {len(ties) - before} ties discovered")

    total = len(ties)
    log(
        "INFO",
        f"\U0001f4cb {total} tie(s) discovered total "
        f"\u2014 processing all with {MAX_WORKERS} workers",
    )
    log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping ties \u2500\u2500\u2500\u2500")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)

    lock = threading.Lock()
    counter = {"rows": 0, "done": 0}

    def process_tie(item):
        tie_id, tie_date = item
        try:
            tc_link = f"{API}/custom/tieCentre/{tie_id}"
            tie_centre = _get_json(tc_link, log, tele, proxies)
            with lock:
                counter["done"] += 1
                done = counter["done"]
            if not tie_centre:
                log(
                    "INFO",
                    f"\u2796 [tie {done}/{total}] {tie_id} "
                    f"({tie_date or 'n/a'}) \u2014 no data",
                )
                return
            matches = (
                tie_centre.get("data", {}).get("tie", {}).get("matches", [])
                or []
            )
            log(
                "INFO",
                f"\U0001f3af [tie {done}/{total}] {tie_id} "
                f"({tie_date or 'n/a'}) \u2014 {len(matches)} match(es)",
            )
            for match in matches:
                match_id = match.get("id", "")
                if not match_id:
                    continue
                score = _match_score(match)
                row = _build_row(
                    config, tie_id, tie_date, match_id, score, log, tele, proxies
                )
                if not row:
                    continue
                cells = [sanitize_cell(row.get(c, "")) for c in COLUMNS]
                with lock:
                    writer.writerow(cells)
                    counter["rows"] += 1
                log(
                    "INFO",
                    f"   \U0001f3c6 {row.get('draw_team_type', '')}: "
                    f"{row.get('winner_1_name') or '?'} def. "
                    f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                    f"@ {row.get('tournament_name') or config.label}",
                )
        except Exception as exc:  # noqa: BLE001 - a bad tie must not kill the run
            tele.record_error(f"Tie {tie_id} failed: {exc}", exc=exc)
            log(
                "WARN",
                redact_secrets(
                    f"\u26a0\ufe0f [tie] {tie_id} failed: "
                    f"{exc.__class__.__name__}: {exc}"
                ),
            )

    if ties:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            list(executor.map(process_tie, ties))

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
