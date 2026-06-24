"""Simulated run generation for the scraper lab.

No live network scraping happens here: MatchMiner ships with sample data, so a
"run" is generated deterministically-ish from its inputs. `create_run` is used
by both the real-time lab form and the `seed_demo_runs` management command so
the two produce identical-looking output.
"""

import csv
import io
import random
from datetime import timedelta

from django.utils import timezone

from .models import Run

ALL_TOURNAMENTS = "All tournaments"

_MEN = [
    "Djokovic, N.", "Alcaraz, C.", "Sinner, J.", "Medvedev, D.", "Zverev, A.",
    "Rune, H.", "Rublev, A.", "Fritz, T.", "Tsitsipas, S.", "Hurkacz, H.",
    "De Minaur, A.", "Ruud, C.", "Dimitrov, G.", "Paul, T.", "Shelton, B.",
]
_WOMEN = [
    "Swiatek, I.", "Sabalenka, A.", "Gauff, C.", "Rybakina, E.", "Pegula, J.",
    "Vondrousova, M.", "Jabeur, O.", "Sakkari, M.", "Garcia, C.", "Kasatkina, D.",
    "Collins, D.", "Kostyuk, M.", "Navarro, E.", "Andreeva, M.", "Keys, M.",
]
_COUNTRIES = ["SRB", "ESP", "ITA", "USA", "GER", "DEN", "POL", "FRA", "GRE", "AUS"]
_ROUNDS = ["R128", "R64", "R32", "R16", "QF", "SF", "F"]
_SET = ["6-4", "7-6(5)", "6-3", "7-5", "6-2", "3-6", "6-7(4)", "4-6", "6-1"]


def _csv_kind(scraper):
    if "rankings" in scraper.slug:
        return "rankings"
    if scraper.slug == "atp-live":
        return "live"
    return "draw"


def _players(scraper):
    if scraper.slug in {"wta-rankings"} or "Women" in (scraper.tour or ""):
        return _WOMEN
    return _MEN


def _sanitize(value):
    """Guard against CSV/spreadsheet formula injection."""
    text = str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def _row_target(kind, single, status, rng):
    if status == Run.Status.FAILED:
        return 0
    if kind == "rankings":
        base = rng.randint(100, 300) if single else rng.randint(220, 520)
    elif kind == "live":
        base = rng.randint(4, 20) if single else rng.randint(8, 44)
    else:
        base = rng.randint(16, 90) if single else rng.randint(70, 260)
    if status == Run.Status.PARTIAL:
        base = int(base * rng.uniform(0.35, 0.7))
    return base


def build_csv(scraper, rows, rng):
    kind = _csv_kind(scraper)
    pool = _players(scraper)
    buf = io.StringIO()
    # csv.writer handles quoting of comma-containing fields (e.g. "Djokovic, N.");
    # _sanitize still guards against spreadsheet formula injection.
    writer = csv.writer(buf, lineterminator="\n")

    def emit(cells):
        writer.writerow([_sanitize(c) for c in cells])

    if kind == "rankings":
        writer.writerow(
            ["rank", "player", "country", "points", "movement", "tournaments_played"]
        )
        for i in range(rows):
            player = rng.choice(pool)
            move = rng.choice(["up 2", "up 1", "steady", "down 1", "down 3", "new"])
            emit([
                i + 1,
                player,
                rng.choice(_COUNTRIES),
                rng.randint(180, 11500),
                move,
                rng.randint(3, 24),
            ])
    elif kind == "live":
        writer.writerow(
            ["match_id", "tournament", "player_1", "player_2", "set_score", "server", "status"]
        )
        for i in range(rows):
            emit([
                f"M{rng.randint(1000, 9999)}",
                scraper.name,
                rng.choice(pool),
                rng.choice(pool),
                f"{rng.choice(_SET)} {rng.choice(_SET)}",
                rng.choice(["P1", "P2"]),
                rng.choice(["in_progress", "in_progress", "completed"]),
            ])
    else:
        writer.writerow(
            ["round", "match_id", "player_1", "player_2", "score", "winner", "duration"]
        )
        for i in range(rows):
            p1 = rng.choice(pool)
            p2 = rng.choice(pool)
            winner = rng.choice([p1, p2])
            score = " ".join(rng.choice(_SET) for _ in range(rng.randint(2, 3)))
            emit([
                rng.choice(_ROUNDS),
                f"MM{rng.randint(10000, 99999)}",
                p1,
                p2,
                score,
                winner,
                f"{rng.randint(0, 3)}h {rng.randint(1, 59):02d}m",
            ])
    return buf.getvalue()


def build_log(scraper, tournament, date_from, date_to, status, rows, started_at, rng):
    logger = f"matchminer.spiders.{scraper.slug}"
    cur = started_at
    lines = []

    def add(level, msg, jump_ms=None):
        nonlocal cur
        cur = cur + timedelta(milliseconds=jump_ms if jump_ms is not None else rng.randint(8, 240))
        stamp = cur.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{stamp}] {level:<5} {logger}  {msg}")

    window = "full history"
    if date_from and date_to:
        window = f"{date_from} \u2192 {date_to}"
    scope = tournament if tournament != ALL_TOURNAMENTS else "all tournaments"

    add("INFO", f"Starting run \u2014 scope={scope}, window={window}")
    add("INFO", f"curl_cffi impersonate=chrome120; proxy pool=res-rotating ({rng.randint(18, 64)} live)")
    age = rng.randint(2, 58)
    add("INFO", f"Reusing cached VendorSession cookies (age {age}m) \u2014 skipping login")
    add("INFO", f"GET https://{scraper.domain}/draws?scope={scope.replace(' ', '+')} -> 200")

    pages = max(6, min(180, rows // rng.randint(2, 4) + rng.randint(4, 18)))
    fetched = 0
    for p in range(1, pages + 1):
        got = rng.randint(1, 6)
        fetched += got
        code = 200
        if status != Run.Status.SUCCESS and rng.random() < 0.06:
            code = rng.choice([429, 503, 502])
            add("WARN", f"page {p:>3}/{pages}: HTTP {code} \u2014 backing off {rng.randint(1, 8)}s, retrying")
        add("INFO", f"page {p:>3}/{pages}: parsed {got} rows ({fetched} total) in {rng.randint(40, 900)}ms")

    if status == Run.Status.PARTIAL:
        add("WARN", f"{rng.randint(2, 9)} rows dropped \u2014 unexpected markup in detail.py selectors")
        add("WARN", "continuing with partial dataset; flag set partial=true")

    if status == Run.Status.FAILED:
        add("ERROR", "Fatal: vendor returned HTTP 403 after 5 consecutive retries")
        add("ERROR", "Traceback (most recent call last):")
        add("ERROR", f"  File \"spiders/{scraper.slug}.py\", line 142, in parse_draw")
        add("ERROR", "    raise ScrapeError(resp.status_code, resp.url)")
        add("ERROR", "matchminer.exceptions.ScrapeError: 403 \u2014 access denied")
        add("INFO", "Run aborted \u2014 status=failed, 0 rows written")
        return "\n".join(lines) + "\n"

    add("INFO", f"Normalising + de-duplicating {rows} rows")
    add("INFO", f"Wrote {rows} rows to CSV")
    add("INFO", f"Run finished \u2014 status={status}")
    return "\n".join(lines) + "\n"


def create_run(
    scraper,
    *,
    tournament=None,
    date_from=None,
    date_to=None,
    user=None,
    started_at=None,
    status=None,
    allow_failure=True,
    rng=None,
):
    rng = rng or random.Random()
    tournament = tournament or ALL_TOURNAMENTS
    started_at = started_at or timezone.now()
    single = tournament != ALL_TOURNAMENTS

    if status is None:
        roll = rng.random()
        if allow_failure and roll < 0.10:
            status = Run.Status.FAILED
        elif roll < 0.24:
            status = Run.Status.PARTIAL
        else:
            status = Run.Status.SUCCESS

    kind = _csv_kind(scraper)
    rows = _row_target(kind, single, status, rng)
    duration_ms = rng.randint(700, 1500) if status == Run.Status.FAILED else rng.randint(1400, 14000)
    csv_data = "" if status == Run.Status.FAILED else build_csv(scraper, rows, rng)
    log_text = build_log(scraper, tournament, date_from, date_to, status, rows, started_at, rng)

    return Run.objects.create(
        scraper=scraper,
        launched_by=user,
        tournament=tournament,
        date_from=date_from,
        date_to=date_to,
        status=status,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=duration_ms),
        duration_ms=duration_ms,
        row_count=rows,
        output_size_bytes=len(csv_data.encode("utf-8")),
        log_text=log_text,
        csv_data=csv_data,
    )
