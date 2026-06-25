"""WTA player-ranking scraper (api.wtatennis.com).

A pure JSON-API scraper: for a single snapshot date it walks the women's
singles **and** doubles ranking tables (paged 100 at a time) and emits one row
per ranked player. No HTML, no proxy strictly required (the API is open), and
no AI — the source's only "AI" was a hard-coded ``gender = 'F'``, which we keep.

Returns the standard runner 5-tuple
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

from accounts.models import Run

from . import _rankings
from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets

# api.wtatennis.com paginates ranking tables 100 rows at a time; this safety cap
# stops a misbehaving API (non-empty forever) from looping without end. The WTA
# table is a few thousand players, so 60 pages (6000) is comfortable headroom.
MAX_PAGES = 60
BASE = "https://api.wtatennis.com/tennis/players/ranked"


def _index_url(page, rank_type, date_iso):
    """Build the ranked-players API URL for one page of one ranking table."""
    return (
        f"{BASE}?page={page}&pageSize=100"
        f"&type=rank{rank_type.title()}&sort=asc"
        f"&metric={rank_type.upper()}&name=&at={date_iso}"
    )


def _row_from(result, rank_type):
    """Map one API record to a :data:`_rankings.COLUMNS` row dict, or ``None``."""
    player = result.get("player") or {}
    player_id = player.get("id") or ""
    if not player_id:
        return None
    last_name = player.get("lastName", "") or ""
    first_name = player.get("firstName", "") or ""
    return {
        "birthdate": _rankings.to_mdy(player.get("dateOfBirth", ""), "%Y-%m-%d"),
        "gender": "F",
        "player_id": player_id,
        "name": f"{last_name}, {first_name}",
        "nationality": player.get("countryCode", "") or "",
        "points": result.get("points", "") or "",
        "rank": result.get("ranking", "") or "",
        "rankdate": _rankings.to_mdy(result.get("rankedAt", ""), "%Y-%m-%dT%H:%M:%SZ"),
        "ranktype": rank_type,
    }


def run(run_obj, log):
    """Execute the WTA rankings scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    snap = _rankings.snapshot_date(run_obj)
    date_iso = snap.isoformat()
    log("INFO", f"\U0001f3be WTA rankings starting \u2014 snapshot {date_iso}")
    proxies = build_proxies(scraper, log)

    csv_out = _rankings.RankingsCsv()
    with ScraperClient(log=log, tele=tele, proxies=proxies) as client:
        for rank_type in _rankings.RANK_TYPES:
            log("INFO", f"\u2500\u2500\u2500\u2500 {rank_type} table \u2500\u2500\u2500\u2500")
            seen = set()
            kept_before = csv_out.row_count
            for page in range(MAX_PAGES):
                url = _index_url(page, rank_type, date_iso)
                results = client.get_json(url)
                if not results:
                    break  # empty page (or a hard failure) — end of this table
                for result in results:
                    row = _row_from(result, rank_type)
                    if not row:
                        continue
                    key = (row["player_id"], rank_type)
                    if key in seen:
                        continue
                    seen.add(key)
                    csv_out.add(row)
            log(
                "INFO",
                f"   \U0001f3c6 {csv_out.row_count - kept_before} {rank_type} "
                f"player(s) collected",
            )

    row_count = csv_out.row_count
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
    return (
        csv_out.value(),
        tele.requests_csv(),
        tele.errors_csv(),
        row_count,
        status,
    )
