"""ATP player-ranking scraper (www.atptour.com).

Two-stage, like the source:

1. **Discover** — walk the men's singles **and** doubles ranking tables (16
   rank-range pages each) for the snapshot week, scraping each ranked player's
   id + rank + points from the rankings HTML.
2. **Enrich** — fetch each player's ``/hero/`` JSON (name, nationality,
   birthdate) concurrently and emit one row per player.

atptour sits behind Cloudflare. The shared :class:`ScraperClient` impersonates
Chrome and detects anti-bot interstitials, but a hard JS challenge can't be
solved here — so without a residential proxy that clears Cloudflare the run
**fails honestly** (empty discovery → 0 rows → FAILED), exactly like the
Stadion scrapers do without a proxy. No AI: the source's only "AI" was a
hard-coded ``gender = 'M'``, which we keep.

Returns the standard runner 5-tuple
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from django.db.models import F

from accounts.models import Run

from . import _rankings
from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets

# The 16 rank-range slices the ATP rankings page is fetched in (mirrors source).
RANK_RANGES = [
    (0, 100), (101, 200), (201, 300), (301, 400), (401, 500), (501, 600),
    (601, 700), (701, 800), (801, 900), (901, 1000), (1001, 1100),
    (1101, 1200), (1201, 1300), (1301, 1400), (1401, 1500), (1501, 5000),
]
ROW_XPATH = (
    '//table[contains(@class, "mega-table") and '
    'contains(@class, "desktop-table")]//tr[@class="lower-row"]'
)
LINK_XPATH = (
    './/td[contains(@class, "player")]//ul[@class="player-stats"]'
    '//li[contains(@class, "name")]//a[contains(@href, "/players/")]/@href'
)
HERO_URL = "https://www.atptour.com/en/-/www/players/hero/{player_id}"
_PLAYER_ID_RE = re.compile(r"/players/[^/]+/([^/]+)/")


def _is_current_week(snap):
    """True when ``snap`` falls in the current Mon–Sun week (per the source)."""
    today = datetime.today().date()
    start = today - timedelta(days=today.weekday())
    return start <= snap <= start + timedelta(days=6)


def _rankings_url(rank_type, date_week, lo, hi):
    """Build the rankings-table URL for one rank-range slice.

    ``date_week`` is the literal ``Current+Week`` token or a ``YYYY-MM-DD`` date;
    it is interpolated into the URL string (not passed as a param) so the ``+``
    in ``Current+Week`` survives exactly as the site expects.
    """
    return (
        f"https://www.atptour.com/en/rankings/{rank_type.lower()}"
        f"?dateWeek={date_week}&rankRange={lo}-{hi}"
    )


def _discover(client, rank_type, date_week, date_iso, log):
    """Scrape every ranked player's id/rank/points for one ranking table.

    Aborts the table early if the very first (top-100) range yields nothing —
    the ATP top 100 always exists, so an empty first page means Cloudflare
    blocked us (or the date is invalid); hammering the other 15 ranges would
    just waste the run.
    """
    players = []
    seen = set()
    for idx, (lo, hi) in enumerate(RANK_RANGES):
        url = _rankings_url(rank_type, date_week, lo, hi)
        sel = client.get_selector(url, timeout=20)
        title = ""
        if sel is not None:
            title = (sel.xpath("string(//title)").get() or "").strip().lower()
        rows = sel.xpath(ROW_XPATH) if (sel is not None and "just a moment" not in title) else []
        if not rows:
            if idx == 0:
                log(
                    "WARN",
                    f"\u26a0\ufe0f {rank_type}: no rows in the top-100 range "
                    "(blocked or empty) \u2014 skipping the rest of this table",
                )
                break
            continue
        for d1 in rows:
            href = d1.xpath(LINK_XPATH).get() or ""
            m = _PLAYER_ID_RE.search(href)
            if not m:
                continue
            player_id = m.group(1)
            if player_id in seen:
                continue
            seen.add(player_id)
            rank = (d1.xpath('string(.//td[contains(@class, "rank")])').get() or "").strip()
            rank = re.sub(r"[^\d+]", "", rank)
            points = (d1.xpath('string(.//td[contains(@class, "points")])').get() or "").strip()
            players.append({
                "player_id": player_id,
                "rank_type": rank_type,
                "range_date": date_iso,
                "points": points,
                "rank": rank,
            })
        log("INFO", f"   \U0001f50e {rank_type} {lo}-{hi}: {len(rows)} row(s)")
    return players


def _enrich_one(client, player):
    """Fetch a player's hero JSON and return a finished row dict, or ``None``."""
    player_id = player["player_id"]
    hero = client.get_json(HERO_URL.format(player_id=player_id), timeout=30)
    if not hero:
        return None
    last_name = hero.get("LastName", "") or ""
    first_name = hero.get("FirstName", "") or ""
    return {
        "birthdate": _rankings.to_mdy(hero.get("BirthDate", ""), "%Y-%m-%dT%H:%M:%S"),
        "gender": "M",
        "player_id": player_id,
        "name": f"{last_name}, {first_name}",
        "nationality": hero.get("NatlId", "") or "",
        "points": player.get("points", ""),
        "rank": player.get("rank", ""),
        "rankdate": player.get("range_date", ""),
        "ranktype": player.get("rank_type", ""),
    }


def run(run_obj, log):
    """Execute the ATP rankings scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    snap = _rankings.snapshot_date(run_obj)
    date_iso = snap.isoformat()
    date_week = "Current+Week" if _is_current_week(snap) else date_iso
    log("INFO", f"\U0001f3be ATP rankings starting \u2014 snapshot {date_iso}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering ranked players \u2500\u2500\u2500\u2500")
    players = []
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        for rank_type in _rankings.RANK_TYPES:
            players.extend(_discover(discovery, rank_type, date_week, date_iso, log))

    total = len(players)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} player(s) to enrich")

    csv_out = _rankings.RankingsCsv()

    def process(chunk):
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            for player in chunk:
                try:
                    row = _enrich_one(client, player)
                    if row:
                        csv_out.add(row)
                        log(
                            "INFO",
                            f"   \U0001f3c6 {row['ranktype']} #{row['rank'] or '?'}: "
                            f"{row['name'] or '?'} ({row['nationality'] or '?'})",
                        )
                except Exception as exc:  # noqa: BLE001 - one bad player can't kill the run
                    tele.record_error(
                        redact_secrets(
                            f"Player {player.get('player_id', '')} failed: {exc}"
                        ),
                        exc=exc,
                    )
                finally:
                    Run.objects.filter(pk=run_obj.pk).update(
                        progress_done=F("progress_done") + 1
                    )
        finally:
            client.close()

    if players:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 enriching players \u2500\u2500\u2500\u2500")
        n = max(1, min(workers, len(players)))
        chunks = [players[i::n] for i in range(n)]
        with ThreadPoolExecutor(max_workers=n) as executor:
            list(executor.map(process, chunks))

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
