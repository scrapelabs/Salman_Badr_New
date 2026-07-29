"""ATP player-ranking scraper (www.atptour.com).

Two-stage, like the source:

1. **Discover** — walk the men's singles **and** doubles ranking tables (16
   rank-range pages each) for the snapshot week, scraping each ranked player's
   id + rank + points from the rankings HTML.
2. **Enrich** — fetch each player's ``/hero/`` JSON (name, nationality,
   birthdate) concurrently and emit one row per player.

atptour sits behind Cloudflare, so both phases use independent Patchright
Chromium sessions through the scraper's assigned proxy. One browser discovers
rankings; each enrichment worker owns one browser for its whole chunk and uses
in-page ``fetch()`` for protected hero JSON. There is no curl fallback.

Returns the standard runner 5-tuple
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import F

from accounts.models import Run

from . import _rankings
from ._browser import BrowserClient, allow_async_unsafe, browser_proxy
from .telemetry import Telemetry, redact_secrets

# The 16 rank-range slices the ATP rankings page is fetched in (mirrors source).
RANK_RANGES = [
    (0, 100), (101, 200), (201, 300), (301, 400), (401, 500), (501, 600),
    (601, 700), (701, 800), (801, 900), (901, 1000), (1001, 1100),
    (1101, 1200), (1201, 1300), (1301, 1400), (1401, 1500), (1501, 5000),
]
# Select every player row in the desktop rankings table by the presence of a
# player-profile link, NOT by an exact ``tr`` class. ATP renders the top 10 in
# ``<tr class="">`` (empty class) and ranks 11+ in ``<tr class="lower-row">``;
# the old ``tr[@class="lower-row"]`` selector silently dropped the entire top
# 10. Filtering on the player link captures both, while still excluding the
# header row and the empty spacer rows (neither has a /players/ link).
ROW_XPATH = (
    '//table[contains(@class, "mega-table") and '
    'contains(@class, "desktop-table")]'
    '//tr[.//td[contains(@class, "player")]//a[contains(@href, "/players/")]]'
)
LINK_XPATH = (
    './/td[contains(@class, "player")]//ul[@class="player-stats"]'
    '//li[contains(@class, "name")]//a[contains(@href, "/players/")]/@href'
)
HERO_URL = "https://www.atptour.com/en/-/www/players/hero/{player_id}"
_HOST = "www.atptour.com"
ATP_REQUEST_TRIES = 10
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


def _make_browser(scraper, log, tele, *, announce):
    """Create one ATP-owned Patchright client with an ephemeral profile."""
    return BrowserClient(
        log=log,
        tele=tele,
        proxy=scraper.proxy,
        allowed_hosts=(_HOST,),
        headless=getattr(settings, "SCRAPER_BROWSER_HEADLESS", True),
        channel=getattr(settings, "SCRAPER_BROWSER_CHANNEL", "") or None,
        user_data_dir=None,
        rotate_proxy_session=False,
        manage_async_unsafe=False,
        announce=announce,
        api_tries=ATP_REQUEST_TRIES,
    )


def _browser_pool_summary(scraper, workers):
    channel = (getattr(settings, "SCRAPER_BROWSER_CHANNEL", "") or "").strip()
    engine = f"Google {channel.title()}" if channel else "Chromium"
    mode = (
        "headless"
        if getattr(settings, "SCRAPER_BROWSER_HEADLESS", True)
        else "headed"
    )
    proxy = scraper.proxy
    if browser_proxy(proxy):
        kind = proxy.get_kind_display() if hasattr(proxy, "get_kind_display") else "?"
        connection = f"via {kind} proxy '{getattr(proxy, 'name', '?')}'"
    else:
        connection = "direct — no proxy"
    return (
        f"🌐 Enrichment pool: {workers} independent patchright {engine} "
        f"browser(s) ({mode}, ephemeral profiles) {connection}"
    )


def _navigate_with_retries(client, url, log):
    """Navigate with a fresh browser context for each bounded retry."""
    tries = max(1, int(client.api_tries))
    for attempt in range(1, tries + 1):
        if attempt > 1:
            try:
                client.relaunch()
            except Exception as exc:  # noqa: BLE001 - classified by the caller
                log(
                    "WARN",
                    redact_secrets(
                        f"⚠️ browser relaunch failed for {url}: "
                        f"{exc.__class__.__name__}: {exc} "
                        f"(attempt {attempt}/{tries})"
                    ),
                )
                continue
        selector = client.get_selector(url)
        if selector is not None:
            if attempt > 1:
                log(
                    "INFO",
                    f"   ✅ browser navigation recovered on attempt "
                    f"{attempt}/{tries}",
                )
            return selector
        if attempt < tries:
            log(
                "INFO",
                f"   🔁 browser navigation failed on attempt {attempt}/{tries} "
                "— relaunching",
            )
    return None


def _discover(client, rank_type, date_week, date_iso, log):
    """Scrape every ranked player's id/rank/points for one ranking table.

    Aborts the table early if the very first (top-100) range yields nothing —
    the ATP top 100 always exists, so an empty first page means Cloudflare
    blocked us (or the date is invalid); hammering the other 15 ranges would
    just waste the run.
    """
    players = []
    incomplete = False
    seen = set()
    for idx, (lo, hi) in enumerate(RANK_RANGES):
        url = _rankings_url(rank_type, date_week, lo, hi)
        sel = _navigate_with_retries(client, url, log)
        title = ""
        if sel is not None:
            title = (sel.xpath("string(//title)").get() or "").strip().lower()
        rows = (
            sel.xpath(ROW_XPATH)
            if (sel is not None and "just a moment" not in title)
            else []
        )
        if not rows:
            if sel is None:
                incomplete = True
            if idx == 0:
                incomplete = True
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
            rank = (
                d1.xpath('string(.//td[contains(@class, "rank")])').get()
                or ""
            ).strip()
            rank = re.sub(r"[^\d+]", "", rank)
            points = (
                d1.xpath('string(.//td[contains(@class, "points")])').get()
                or ""
            ).strip()
            players.append(
                {
                    "player_id": player_id,
                    "rank_type": rank_type,
                    "range_date": date_iso,
                    "points": points,
                    "rank": rank,
                }
            )
        log("INFO", f"   \U0001f50e {rank_type} {lo}-{hi}: {len(rows)} row(s)")
    return players, incomplete


def _enrich_one(client, player, bio_cache, cache_lock):
    """Build a finished row for one player, reusing a cached hero bio when present.

    A multi-week date range collects the same player on several Mondays; their
    profile (name / nationality / birthdate) is static, so the bio is cached and
    reused across weeks instead of refetched every time (the cache is not strict
    single-flight, so a rare concurrent miss may fetch twice — harmless). Only
    the rank / points / rankdate (which DO vary by week) come from ``player``.
    """
    player_id = player["player_id"]
    with cache_lock:
        bio = bio_cache.get(player_id)
    if bio is None:
        hero = client.get_json(
            HERO_URL.format(player_id=player_id),
            timeout=30,
            tries=ATP_REQUEST_TRIES,
        )
        if not hero:
            return None
        last_name = hero.get("LastName", "") or ""
        first_name = hero.get("FirstName", "") or ""
        bio = {
            "birthdate": _rankings.to_mdy(
                hero.get("BirthDate", ""), "%Y-%m-%dT%H:%M:%S"
            ),
            "name": f"{last_name}, {first_name}",
            "nationality": hero.get("NatlId", "") or "",
        }
        with cache_lock:
            bio_cache[player_id] = bio
    return {
        "birthdate": bio["birthdate"],
        "gender": "M",
        "player_id": player_id,
        "name": bio["name"],
        "nationality": bio["nationality"],
        "points": player.get("points", ""),
        "rank": player.get("rank", ""),
        "rankdate": player.get("range_date", ""),
        "ranktype": (player.get("rank_type", "") or "").capitalize(),
    }


def run(run_obj, log):
    """Execute the ATP rankings scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    # A date range collects every weekly ranking (Monday) inside it; a single
    # date collects just that snapshot. Either way, one Monday == one snapshot.
    snaps = _rankings.snapshot_dates(run_obj)
    rank_types = _rankings.resolve_rank_types(run_obj)
    span = (
        snaps[0].isoformat()
        if len(snaps) == 1
        else f"{snaps[0].isoformat()} \u2192 {snaps[-1].isoformat()} "
        f"({len(snaps)} weekly snapshot(s))"
    )
    log("INFO", f"\U0001f3be ATP rankings starting \u2014 {span}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    csv_out = _rankings.RankingsCsv(player_id_header="Id")
    # A player appears once per week in a multi-week range; their static bio is
    # cached and reused across weeks so a 3-week range isn't ~3x the hero requests.
    bio_cache = {}
    cache_lock = threading.Lock()

    def advance_progress(count=1):
        if count:
            Run.objects.filter(pk=run_obj.pk).update(
                progress_done=F("progress_done") + count
            )

    def process(chunk):
        completed = 0
        try:
            with _make_browser(scraper, log, tele, announce=False) as client:
                first = chunk[0]
                prime_url = _rankings_url(
                    first["rank_type"], first["range_date"], 0, 100
                )
                if _navigate_with_retries(client, prime_url, log) is None:
                    tele.record_error(
                        "ATP enrichment browser failed to prime the rankings origin"
                    )
                    log(
                        "WARN",
                        "⚠️ enrichment browser could not prime ATP — skipping "
                        f"{len(chunk)} assigned player(s)",
                    )
                    advance_progress(len(chunk))
                    return

                for player in chunk:
                    try:
                        row = _enrich_one(client, player, bio_cache, cache_lock)
                        if row:
                            csv_out.add(row)
                            log(
                                "INFO",
                                f"   \U0001f3c6 {row['rankdate']} "
                                f"{row['ranktype']} #{row['rank'] or '?'}: "
                                f"{row['name'] or '?'} "
                                f"({row['nationality'] or '?'})",
                            )
                        else:
                            tele.record_error(
                                "ATP hero profile failed after browser retries for "
                                f"player {player.get('player_id', '')}"
                            )
                    except Exception as exc:  # noqa: BLE001 - isolate one player
                        tele.record_error(
                            redact_secrets(
                                f"Player {player.get('player_id', '')} failed: {exc}"
                            ),
                            exc=exc,
                        )
                    finally:
                        advance_progress()
                        completed += 1
        except Exception as exc:  # noqa: BLE001 - isolate one worker browser
            tele.record_error(
                redact_secrets(f"ATP enrichment browser failed: {exc}"),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(
                    f"⚠️ enrichment browser failed: "
                    f"{exc.__class__.__name__}: {exc}"
                ),
            )
            advance_progress(len(chunk) - completed)

    players = []
    # BrowserClient's sync Playwright loop makes Django ORM calls async-unsafe.
    # This one process-wide scope covers discovery and every worker browser, so
    # no client races another by restoring the environment flag too early.
    with allow_async_unsafe():
        # ---- phase 1 · discovery --------------------------------------
        log(
            "INFO",
            "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering ranked players "
            "(patchright) \u2500\u2500\u2500\u2500",
        )
        try:
            with _make_browser(scraper, log, tele, announce=True) as discovery:
                for snap in snaps:
                    date_iso = snap.isoformat()
                    date_week = (
                        "Current+Week" if _is_current_week(snap) else date_iso
                    )
                    if len(snaps) > 1:
                        log("INFO", f"\U0001f4c5 ranking week {date_iso}")
                    for rank_type in rank_types:
                        found, incomplete = _discover(
                            discovery, rank_type, date_week, date_iso, log
                        )
                        players.extend(found)
                        if incomplete:
                            tele.record_error(
                                f"ATP {rank_type} rankings incomplete for {date_iso}"
                            )
        except Exception as exc:  # noqa: BLE001 - honest browser-unavailable failure
            tele.record_error(
                redact_secrets(f"ATP discovery browser failed: {exc}"),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(
                    f"⚠️ ATP discovery browser failed: "
                    f"{exc.__class__.__name__}: {exc}"
                ),
            )

        total = len(players)
        Run.objects.filter(pk=run_obj.pk).update(
            progress_total=total,
            progress_done=0,
        )
        log("INFO", f"\U0001f4cb {total} player-week row(s) to enrich")

        # ---- phase 2 · enrichment ------------------------------------
        if players:
            n = max(1, min(workers, len(players)))
            chunks = [players[i::n] for i in range(n)]
            log(
                "INFO",
                "\u2500\u2500\u2500\u2500 phase 2 \u00b7 enriching players "
                "(patchright) \u2500\u2500\u2500\u2500",
            )
            log("INFO", _browser_pool_summary(scraper, n))
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
    # ATP exhausts its expanded retry budget before omitting a row. Keep those
    # failures in errors.csv, but a run that produced ranking data is successful.
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
