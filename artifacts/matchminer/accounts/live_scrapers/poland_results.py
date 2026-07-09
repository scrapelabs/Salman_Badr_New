"""Poland Results (PZT / portal.pzt.pl) scraper.

Ports the production ``poland_results`` spider onto MatchMiner's shared HTTP
client (:mod:`accounts.live_scrapers._http`) + telemetry. The source is a
two-stage HTML pipeline over the Polish Tennis Association portal
(``portal.pzt.pl``), an ASP.NET site:

1. **Discovery** — for each category summary page
   (``TournamentsResults.aspx``, one link per age-group/gender) read the result
   table (``//table[@class="tabRB"]``); a tournament whose listed date range
   falls **entirely** inside the run window is kept (deduped by URL).
2. **Details** — each tournament URL is rewritten to its
   ``TournamentOrderOfPlay.aspx`` view, whose calendar exposes one link per
   playing day. Each per-date sub-page's match table
   (``//table[@class="listBlue"]``) is walked and parsed into one CSV row per
   played match, with the score normalised to the winner's perspective
   ("6-3, 6-2;").

Input is a **date range** (``date_from`` / ``date_to``) *or* a single
``tournament_url`` (validated against the ``portal.pzt.pl`` allowlist at the
view layer); a URL skips discovery and scrapes that one tournament.

This is a **deterministic** port — the source detects gender from Polish
category words (``_gender_from_draw``) and resolves dates via a static
``POLISH_MONTHS`` map and regex/XPath, so nothing AI-flavoured had to be
removed (``helper.py``'s LLM functions are never called by this spider). The
production spider also persisted rows to its own Django model; that side-channel
is dropped — MatchMiner persists rows to the DB / downloadable CSV.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from urllib.parse import urljoin

from django.db.models import F
from django.utils import timezone

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

BASE = "https://portal.pzt.pl"

# Category summary pages, kept verbatim from the source spider — one entry per
# age-group/gender plus the combined seniors/amateurs view.
SUMMARIES = [
    {"name": "SKRZATY", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=12&Male=M"},
    {"name": "SKRZATKI", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=12&Male=K"},
    {"name": "MŁODZICY", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=14&Male=M"},
    {"name": "MŁODZICZKI", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=14&Male=K"},
    {"name": "KADECI", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=16&Male=M"},
    {"name": "KADETKI", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=16&Male=K"},
    {"name": "JUNIORZY", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=18&Male=M"},
    {"name": "JUNIORKI", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=18&Male=K"},
    {"name": "MĘŻCZYŹNI", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=19&Male=M"},
    {"name": "KOBIETY", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=19&&Male=K"},
    {"name": "SENIORZY I AMATORZY", "link": "https://portal.pzt.pl/TournamentsResults.aspx?CategoryID=AIS"},
]

# Items CSV columns — the shared MatchMiner items schema (same as Czech), so
# downloaded files stay uniform across scrapers.
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


# Static Polish month names (full / inflected / abbreviated) -> month number.
# Kept verbatim from the source — used for the title-line date fallback.
POLISH_MONTHS = {
    "styczeń": 1, "stycznia": 1, "sty": 1,
    "luty": 2, "lutego": 2, "lut": 2,
    "marzec": 3, "marca": 3, "mar": 3,
    "kwiecień": 4, "kwietnia": 4, "kwi": 4,
    "maj": 5, "maja": 5,
    "czerwiec": 6, "czerwca": 6, "cze": 6,
    "lipiec": 7, "lipca": 7, "lip": 7,
    "sierpień": 8, "sierpnia": 8, "sie": 8,
    "wrzesień": 9, "września": 9, "wrz": 9,
    "październik": 10, "października": 10, "paź": 10,
    "listopad": 11, "listopada": 11, "lis": 11,
    "grudzień": 12, "grudnia": 12, "gru": 12,
}


# ---------------------------------------------------------------------------
# Generic selector / text helpers (mirror the source's fctcore.parse_field +
# clean helpers — parse_field normalises whitespace via XPath normalize-space).
# ---------------------------------------------------------------------------
def _field(sel, query):
    """First ``normalize-space(query)`` match, or ``""`` (== fctcore.parse_field)."""
    try:
        return sel.xpath(f"normalize-space({query})").get() or ""
    except Exception:  # noqa: BLE001 - malformed query can't kill discovery
        return ""


def clean(text):
    """Collapse whitespace and strip."""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def format_name(raw_name):
    """'Kurzawa Filip' -> 'Kurzawa, Filip' (PZT shows Surname Firstname)."""
    parts = clean(raw_name).split(" ")
    if len(parts) < 2:
        return clean(raw_name)
    return parts[0] + ", " + " ".join(parts[1:])


def to_us_date(y, m, d):
    """M/D/YYYY without leading zeros, like the samples (5/23/2026)."""
    return f"{int(m)}/{int(d)}/{int(y)}"


# ---------------------------------------------------------------------------
# Date-window parsing (ported faithfully from the source TournamentParser).
# ---------------------------------------------------------------------------
def parse_single_date(text):
    """Parse one date in ISO ('2024-04-21') or European ('21. 4. 2024') format."""
    text = (text or "").strip()

    # ISO format: YYYY-MM-DD
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass

    # European format: 'D. M. YYYY' (with flexible spacing/dots)
    m = re.match(r"^(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})$", text)
    if m:
        day, month, year = map(int, m.groups())
        return date(year, month, day)

    raise ValueError(f"Unrecognized date format: {text!r}")


def parse_range(range_str):
    """Parse a date range string into ``(start, end)``.

    Supports:
      - ISO:      '2024-04-21 - 2024-04-22'
      - European: '28. 11.-2. 12. 2025'  (year applies to both parts)
    """
    range_str = (range_str or "").strip()

    # ISO range: split on the dash between the two dates
    if re.match(r"^\d{4}-\d{2}-\d{2}\s*-\s*\d{4}-\d{2}-\d{2}$", range_str):
        start_str, end_str = re.split(r"\s*-\s*(?=\d{4}-)", range_str, maxsplit=1)
        return parse_single_date(start_str), parse_single_date(end_str)

    # European range: '28. 11.-2. 12. 2025' or '28. 11. 2025 - 2. 12. 2025'
    parts = re.split(r"\s*[-–]\s*", range_str)
    if len(parts) == 2:
        start_part, end_part = parts[0].strip(), parts[1].strip()
        # Year is usually only on the end part: '28. 11.' + '2. 12. 2025'
        year_match = re.search(r"(\d{4})\s*$", end_part)
        if year_match and not re.search(r"\d{4}", start_part):
            start_part = start_part.rstrip(". ") + ". " + year_match.group(1)
        return parse_single_date(start_part), parse_single_date(end_part)

    raise ValueError(f"Unrecognized range format: {range_str!r}")


def is_fully_inside(range_str, window_start, window_end):
    """True only if the tournament range falls ENTIRELY within the window.

    ``window_start`` / ``window_end`` are ``date`` objects (inclusive bounds).
    Mirrors the source's stricter ``is_fully_inside`` behaviour.
    """
    start, end = parse_range(range_str)
    return start >= window_start and end <= window_end


# ---------------------------------------------------------------------------
# Match / score / gender helpers (ported from the source details Parser).
# ---------------------------------------------------------------------------
def _winner_side(sets):
    """sets = [(left, right), ...] -> 'left' or 'right' by sets won."""
    left = sum(1 for a, b in sets if a > b)
    right = sum(1 for a, b in sets if b > a)
    return "left" if left > right else "right"


def _format_score(sets, winner_side):
    """Score from the winner's perspective, semicolon at the end: '6-3, 6-2;'"""
    parts = []
    for a, b in sets:
        if winner_side == "left":
            parts.append(f"{a}-{b}")
        else:
            parts.append(f"{b}-{a}")
    return ", ".join(parts) + ";"


def _gender_from_draw(draw_name):
    d = draw_name.lower()
    if "chłopcy" in d or "mężczyźni" in d or "chlopcy" in d or "mezczyzni" in d:
        return "Male", "M"
    if "dziewczęta" in d or "kobiety" in d or "dziewczeta" in d:
        return "Female", "F"
    return "", ""


# ---------------------------------------------------------------------------
# Page-level info (same for every match on an order-of-play sub-page)
# ---------------------------------------------------------------------------
def _parse_page_info(page):
    """Parse tournament-level fields shared by every match on the page."""
    info = {}

    info["tournament_name"] = clean(
        page.xpath('//div[@class="tournAppName_B"]/text()').get()
    )

    # "Od: 2026.06.06" / "Do: 2026.06.08"
    dates_txt = clean(" ".join(
        page.xpath('//div[@class="tournAppTopCent_B"]//text()').getall()
    ))
    m = re.search(r"Od:\s*(\d{4})\.(\d{2})\.(\d{2})", dates_txt)
    info["tournament_start_date"] = to_us_date(m.group(1), m.group(2), m.group(3)) if m else ""
    m = re.search(r"Do:\s*(\d{4})\.(\d{2})\.(\d{2})", dates_txt)
    info["tournament_end_date"] = to_us_date(m.group(1), m.group(2), m.group(3)) if m else ""

    # Match date: prefer Date=YYYY-MM-DD in the form action / active calendar link
    action = page.xpath('//form[@id="aspnetForm"]/@action').get() or ""
    m = re.search(r"Date=(\d{4})-(\d{2})-(\d{2})", action)
    if not m:
        # fallback: "Kolejność rozgrywanych meczów w dniu 07 czerwiec 2026"
        title = clean(page.xpath('//div[@class="titleOrderOfPlay"]/text()').get())
        m2 = re.search(r"(\d{1,2})\s+(\wfffd*\w+)\s+(\d{4})", title)
        if m2 and m2.group(2).lower() in POLISH_MONTHS:
            info["date"] = to_us_date(m2.group(3), POLISH_MONTHS[m2.group(2).lower()], m2.group(1))
        else:
            info["date"] = ""
    else:
        info["date"] = to_us_date(m.group(1), m.group(2), m.group(3))

    # City: "Miejsce turnieju: 99-210 Uniejów, ul. Sportowa ..."
    place = clean(" ".join(page.xpath(
        '//div[@class="tournAppPlaceOfGame_B"]//div[@class="tournAppPlaceOfGameR_B"]//text()'
    ).getall()))
    m = re.search(r"\d{2}-\d{3}\s+([^,]+)", place)
    info["tournament_city"] = clean(m.group(1)) if m else place.split(",")[0]

    tid = re.search(r"TournamentID=([0-9A-Fa-f-]+)", action)
    info["tournament_url"] = (
        f"http://portal.pzt.pl/TournamentOrderOfPlay.aspx?TournamentID={tid.group(1)}" if tid else ""
    )
    return info


# ---------------------------------------------------------------------------
# One match row
# ---------------------------------------------------------------------------
def _parse_match(row, info):
    """Parse one ``//table[@class="listBlue"]`` row into a CSV row dict, or ``None``."""
    tds = row.xpath("./td")
    if len(tds) < 5:
        return None  # header / court row

    # Draw name, e.g. "Juniorzy - do 18 lat" + "Gra pojedyncza, Chłopcy"
    draw_name = clean(" ".join(tds[2].xpath(".//text()").getall()))
    if not draw_name:
        return None

    # Players: links with UserID inside the 4th td
    links = tds[3].xpath('.//a[contains(@href, "PlayerProfile.aspx")]')
    players = []
    for a in links:
        name = clean(" ".join(a.xpath(".//text()").getall()))
        href = a.xpath("./@href").get() or ""
        pid = ""
        m = re.search(r"UserID=([A-Z]{3}\d+)", href)
        if m:
            pid = m.group(1)
        players.append({"name": format_name(name), "id": pid})
    if len(players) < 2:
        return None

    is_doubles = len(players) == 4
    left_players = players[: 2 if is_doubles else 1]
    right_players = players[2:] if is_doubles else players[1:]

    # Score cells in the last td, e.g. ['3:6', '6:2', '10:6']
    raw_sets = [clean(t) for t in tds[4].xpath(".//td/text()").getall() if clean(t)]
    sets = []
    for s in raw_sets:
        m = re.match(r"^(\d+)\s*:\s*(\d+)", s)
        if m:
            sets.append((int(m.group(1)), int(m.group(2))))
    if not sets:
        # Not-played / scheduled match — skip honestly rather than emit a row.
        return None

    winner_side = _winner_side(sets)
    winners = left_players if winner_side == "left" else right_players
    losers = right_players if winner_side == "left" else left_players

    gender, player_gender = _gender_from_draw(draw_name)

    return {
        "match_id": "",
        "ball_type": "Yellow",
        "draw_bracket_value": "",
        "draw_name": draw_name,
        "draw_team_type": "Doubles" if is_doubles else "Singles",
        "tournament_name": info.get("tournament_name", ""),
        "date": info.get("date", ""),
        "round": "",
        "score": _format_score(sets, winner_side),
        "winner_1_name": winners[0]["name"],
        "winner_1_gender": player_gender,
        "winner_1_third_party_id": winners[0]["id"],
        "winner_1_city": "",
        "winner_1_country": "Poland",
        "winner_1_state": "",
        "winner_2_name": winners[1]["name"] if is_doubles else "",
        "winner_2_gender": player_gender if is_doubles else "",
        "winner_2_third_party_id": winners[1]["id"] if is_doubles else "",
        "winner_2_city": "",
        "winner_2_state": "",
        "loser_1_name": losers[0]["name"],
        "loser_1_gender": player_gender,
        "loser_1_third_party_id": losers[0]["id"],
        "loser_1_city": "",
        "loser_1_state": "",
        "loser_1_country": "Poland",
        "loser_2_name": losers[1]["name"] if is_doubles else "",
        "loser_2_gender": player_gender if is_doubles else "",
        "loser_2_third_party_id": losers[1]["id"] if is_doubles else "",
        "loser_2_city": "",
        "loser_2_state": "",
        "outcome": "Completed",
        "id_type": "Poland",
        "draw_gender": gender,
        "draw_bracket_type": "",
        "draw_type": "",
        "tournament_city": info.get("tournament_city", ""),
        "tournament_state": "",
        "tournament_country_code": "POL",
        "tournament_host": "",
        "tournament_location_type": "",
        "tournament_surface": "",
        "tournament_event_category": "",
        "tournament_event_grade": "",
        "tournament_import_source": "Poland",
        "tournament_sanction_body": "Tennis Poland",
        "winner_2_country": "Poland" if is_doubles else "",
        "winner_2_college": "",
        "loser_2_country": "Poland" if is_doubles else "",
        "loser_2_college": "",
        "tournament_event_type": "Tournament",
        "winner_1_college": "",
        "loser_1_college": "",
        "tournament_url": info.get("tournament_url", ""),
        "winner_1_dob": "",
        "winner_2_dob": "",
        "loser_1_dob": "",
        "loser_2_dob": "",
        "tournament_country": "Poland",
        "tournament_start_date": info.get("tournament_start_date", ""),
        "tournament_end_date": info.get("tournament_end_date", ""),
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _discover_tournaments(client, start_d, end_d, log):
    """Return ``[{tournament_url}]`` for tournaments fully inside the window."""
    tournaments = []
    seen = set()
    for summary in SUMMARIES:
        sel = client.get_selector(summary["link"])
        if sel is None:
            continue
        for d1 in sel.xpath('//table[@class="tabRB"]//tr[not(th)]'):
            pre_link = _field(d1, "./td[2]/a/@href")
            tournament_date = _field(d1, "./td[4]")
            if not (pre_link and tournament_date):
                continue
            tournament_url = urljoin(BASE + "/", pre_link)
            try:
                included = is_fully_inside(tournament_date, start_d, end_d)
            except ValueError:
                # A single unparseable listing date is skipped, not fatal.
                continue
            if not included or tournament_url in seen:
                continue
            seen.add(tournament_url)
            tournaments.append({"tournament_url": tournament_url})
            log(
                "INFO",
                f"   \U0001f5d3\ufe0f {summary['name']}: {tournament_date} \u2192 {tournament_url}",
            )
    log("INFO", f"\U0001f50e {len(tournaments)} tournament(s) in window")
    return tournaments


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------
def _scrape_tournament(client, tournament):
    """Fetch a tournament's order-of-play + per-date pages -> list of CSV rows."""
    tournament_url = tournament.get("tournament_url", "")
    # Rewrite the results/drawsheet view to the order-of-play view.
    tournament_url = tournament_url.replace("TournamentResults", "TournamentOrderOfPlay")
    tournament_url = tournament_url.replace("TournamentDrawsheet", "TournamentOrderOfPlay")

    sel = client.get_selector(tournament_url)
    if sel is None:
        return []

    rows = []
    for d1 in sel.xpath(
        '//div[@id="ctl00_cphMainContainer_updPanel"]'
        '//div[contains(@class, "tournamentcalendar")]/a'
    ):
        pre_link = _field(d1, "./@href")
        if not pre_link:
            continue
        date_url = urljoin(BASE + "/", pre_link)
        date_sel = client.get_selector(date_url)
        if date_sel is None:
            continue
        info = _parse_page_info(date_sel)
        for r in date_sel.xpath('//table[@class="listBlue"]//tr[td]'):
            record = _parse_match(r, info)
            if record:
                rows.append(record)
    return rows


def run(run_obj, log):
    """Execute the Poland (PZT) scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    params = run_obj.params or {}
    tournament_url = (params.get("tournament_url") or "").strip()

    if tournament_url:
        log("INFO", "\U0001f3be Poland (PZT) starting \u2014 single tournament URL")
        start_d = end_d = None
    else:
        start_d = run_obj.date_from or timezone.localdate()
        end_d = run_obj.date_to or timezone.localdate()
        log("INFO", f"\U0001f3be Poland (PZT) starting \u2014 {start_d} \u2192 {end_d}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering tournaments \u2500\u2500\u2500\u2500")
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        if tournament_url:
            tournaments = [{"tournament_url": tournament_url}]
        else:
            tournaments = _discover_tournaments(discovery, start_d, end_d, log)

    total = len(tournaments)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} tournament(s) to scrape")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def process(tournament):
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            rows = _scrape_tournament(client, tournament)
            for row in rows:
                key = row.get("match_id") or (
                    row.get("tournament_url", ""),
                    row.get("date", ""),
                    row.get("draw_name", ""),
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
                    f"@ {row.get('tournament_name') or 'Poland (PZT)'}",
                )
        except Exception as exc:  # noqa: BLE001 - one bad tournament can't kill the run
            tele.record_error(
                redact_secrets(
                    f"Tournament {tournament.get('tournament_url', '')} failed: {exc}"
                ),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(f"\u26a0\ufe0f tournament failed: {exc.__class__.__name__}: {exc}"),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
            client.close()

    if tournaments:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping tournaments \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, tournaments))
    else:
        tele.record_error(
            "No Poland (PZT) tournaments matched the run window — nothing to scrape."
            if not tournament_url
            else "Poland (PZT) tournament URL yielded no tournaments to scrape.",
            level="WARN",
        )

    row_count = counter["rows"]
    log("INFO", "\u2500\u2500\u2500\u2500 summary \u2500\u2500\u2500\u2500")
    log("INFO", f"\U0001f4be Writing {row_count} row(s) to CSV")
    log(
        "INFO",
        f"\U0001f4ca Telemetry: {tele.request_count} request(s), {tele.error_count} error(s)",
    )
    if not row_count:
        tele.record_error(
            "Poland (PZT) scrape produced 0 match rows — failing honestly "
            "(no fabricated data)."
        )
    status = Run.Status.SUCCESS if row_count else Run.Status.FAILED
    icon = "\U0001f3c1" if status == Run.Status.SUCCESS else "\U0001f6d1"
    log("INFO", f"{icon} Run finished \u2014 status={status}, rows={row_count}")
    items_csv = buf.getvalue() if row_count else ""
    return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
