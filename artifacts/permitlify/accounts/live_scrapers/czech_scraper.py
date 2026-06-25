"""Czech tennis (cesky-tenis.cz) scraper.

Ports the production ``czech_scraper`` spider onto MatchMiner's shared HTTP
client (:mod:`accounts.live_scrapers._http`) + telemetry. The source is a
three-stage HTML pipeline over ``cesky-tenis.cz``:

1. **Seasons** — for each age-group menu (``dospeli`` / ``dorost`` /
   ``starsi-zactvo`` / ``mladsi-zactvo``) read the season ``<select>`` and keep
   only the current + previous calendar year's seasons.
2. **Tournaments** — POST each season back to its listing to get the draw table;
   column 5 holds the men's draw link and column 6 the women's. Draws whose
   European-formatted date range falls **entirely** inside the run window are
   kept (deduped by tournament id + gender).
3. **Details** — per draw, fetch the registration list (``tab=seznam``) for
   tournament info + players (year of birth) and the results page for the
   singles/doubles brackets, then emit one CSV row per played match.

Input is a **date range** (``date_from`` / ``date_to``) *or* a single
``tournament_url`` (validated against the ``cesky-tenis.cz`` allowlist at the
view layer); a URL skips discovery and scrapes that one draw.

This is a **deterministic** port — the source already detects gender from Czech
category words (no AI), so nothing AI-flavoured had to be removed. Discovery is
made slightly more robust than the source: relative draw links are resolved to
absolute URLs before parsing so their ``sezona`` query param is preserved, and a
single unparseable listing date is skipped instead of aborting the whole season.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import io
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from django.db.models import F
from django.utils import timezone

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

BASE = "https://cesky-tenis.cz"
# Age-group menus that each expose their own season dropdown + draw listing.
MENU_SLUGS = ("dospeli", "dorost", "starsi-zactvo", "mladsi-zactvo")

# Items CSV columns — the shared MatchMiner items schema (same as Brazil), so
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


# ---------------------------------------------------------------------------
# Generic selector helper
# ---------------------------------------------------------------------------
def _xp(sel, query):
    """Return the first ``xpath`` match's text, stripped, or ``""``."""
    return (sel.xpath(query).get() or "").strip()


# ---------------------------------------------------------------------------
# Date / name helpers (verified against live cesky-tenis.cz markup)
# ---------------------------------------------------------------------------
def _parse_start_date(raw):
    """'8.-9. 5. 2026' or '07.05.2026 …'  →  m/d/yyyy."""
    raw = (raw or "").strip()
    m = re.match(r"(\d+)\.\s*(?:-\s*\d+\.)?\s*(\d+)\.\s*(\d{4})", raw)
    if m:
        return f"{int(m.group(2))}/{int(m.group(1))}/{m.group(3)}"
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if m:
        return f"{int(m.group(2))}/{int(m.group(1))}/{m.group(3)}"
    return raw


def _parse_end_date(raw):
    """'8.-9. 5. 2026'  →  end date as m/d/yyyy; single day → same as start."""
    raw = (raw or "").strip()
    m = re.match(r"\d+\.\s*-\s*(\d+)\.\s*(\d+)\.\s*(\d{4})", raw)
    if m:
        return f"{int(m.group(2))}/{int(m.group(1))}/{m.group(3)}"
    return _parse_start_date(raw)


def _normalise_name(raw):
    """'Lastname Firstname' (with stray spaces) → 'Lastname, Firstname'."""
    raw = re.sub(r"\s+", " ", (raw or "").strip())
    if not raw:
        return ""
    parts = raw.split(" ", 1)
    return f"{parts[0]}, {parts[1]}" if len(parts) == 2 else raw


def _info_blocks(sel):
    """Return ``{label: value}`` for every ``<p class="info">`` on the page."""
    result = {}
    for p in sel.css("p.info"):
        label = (p.css("span::text").get() or "").strip()
        if not label:
            continue
        texts = [t.strip() for t in p.css("*::text").getall()
                 if t.strip() and t.strip() != label]
        result[label] = " ".join(texts).strip()
    return result


def _td_classes(td):
    return (td.attrib.get("class") or "").split()


def _tournament_id_from_url(url):
    m = re.search(r"/turnaj/(\d+)", url or "")
    return m.group(1) if m else ""


# Czech (and English) category words that identify a draw's gender. Female is
# matched first and male patterns explicitly exclude female stems
# ("žák" but not "žákyně", "junior" but not "juniorky").
_FEMALE_WORDS = re.compile(
    r"\bžen\w*|žákyn\w*|dorostenk\w*|juniork\w*|dívk\w*"
    r"|\bwom[ae]n\b|\bgirls?\b|\bladies\b|\bfemale\b",
    re.IGNORECASE,
)
_MALE_WORDS = re.compile(
    r"\bmuž\w*|\bžác\w*|\bžák(?!yn)\w*|dorostenc\w*|junioř\w*|junior(?!k)\w*|chlapc\w*"
    r"|\bm[ae]n\b|\bboys?\b|\bmale\b",
    re.IGNORECASE,
)


def _detect_gender(sel, tournament_type="", tid=""):
    """Decide the draw gender ("M"/"F").

    Priority: (1) the ``tournament_type`` label from the listing; (2) Czech
    category words in the page header; (3) legacy id-prefix fallback (women's
    ids start with "2").
    """
    t = (tournament_type or "").strip()
    if t:
        u = t.upper()
        if u in ("F", "W", "Z", "Ž", "ZENY", "ŽENY", "WOMEN"):
            return "F"
        if u in ("M", "MUZI", "MUŽI", "MEN"):
            return "M"
        f, m = bool(_FEMALE_WORDS.search(t)), bool(_MALE_WORDS.search(t))
        if f and not m:
            return "F"
        if m and not f:
            return "M"

    header_texts = (
        sel.css("h1 ::text").getall()
        + sel.css("title::text").getall()
        + sel.css("div.box--blue h4::text").getall()
        + sel.css("p.info ::text").getall()
    )
    page = " ".join(x.strip() for x in header_texts if x and x.strip())
    f, m = bool(_FEMALE_WORDS.search(page)), bool(_MALE_WORDS.search(page))
    if f and not m:
        return "F"
    if m and not f:
        return "M"

    return "F" if str(tid).startswith("2") else "M"


def _build_urls(tournament_url):
    """Build ``(tid, results_url, seznam_url)`` from a draw URL, preserving its
    query params (e.g. ``sezona=L26``). The results page is the default tab; the
    seznam (registration) page adds ``tab=seznam``.
    """
    results = ""
    seznam = ""
    tid = _tournament_id_from_url(tournament_url)
    if tid:
        parts = urlparse(tournament_url)
        if not parts.scheme:
            parts = urlparse(f"{BASE}/turnaj/{tid}")
        query = {k: v for k, v in parse_qsl(parts.query) if k != "tab"}
        results = urlunparse(parts._replace(query=urlencode(query)))
        seznam = urlunparse(parts._replace(query=urlencode({**query, "tab": "seznam"})))
    return tid, results, seznam


def parse_european_range(range_str):
    """Parse a European-style date range into ``(start, end)`` ``date``s.

    Supports cross-month ("28. 11.-2. 12. 2025"), same-month shorthand
    ("18.-20. 4. 2026") and single-day ("18. 7. 2026") forms. A cross-year
    range ("28. 12.-2. 1. 2026") puts the start in the previous year. Raises
    ``ValueError`` if the string can't be parsed.
    """
    s = re.sub(r"\s+", " ", (range_str or "").strip())

    m = re.match(
        r"^(\d{1,2})\.\s*(\d{1,2})?\.?\s*-\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})$", s
    )
    if m:
        d1, m1_raw, d2, m2, yyyy = m.groups()
        year = int(yyyy)
        month2 = int(m2)
        month1 = int(m1_raw) if m1_raw else month2
        start = date(year, month1, int(d1))
        end = date(year, month2, int(d2))
        if start > end:
            start = date(year - 1, month1, int(d1))
        return start, end

    m = re.match(r"^(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})$", s)
    if m:
        d1, m1, yyyy = m.groups()
        single = date(int(yyyy), int(m1), int(d1))
        return single, single

    raise ValueError(f"Cannot parse European range: '{range_str}'")


def is_range_included(european_range_str, window_start, window_end):
    """True iff the European range falls entirely within the window (inclusive)."""
    start, end = parse_european_range(european_range_str)
    return start >= window_start and end <= window_end


# ---------------------------------------------------------------------------
# Tournament-level parsing
# ---------------------------------------------------------------------------
def parse_tournament(sel, tournament_id="", tournament_url="", tournament_type=""):
    """Parse all tournament-level fields from a seznam/results page."""
    info = _info_blocks(sel)

    tournament_name = (sel.css("h1::text").get() or "").strip()
    draw_name = (sel.css("div.box--blue h4::text").get() or "").strip()

    date_raw = info.get("Datum", "")
    tournament_start_date = _parse_start_date(date_raw)
    tournament_end_date = _parse_end_date(date_raw)
    match_date = tournament_start_date  # match date = event start

    organiser_raw = info.get("Pořadatel", "")
    org_parts = [x.strip() for x in organiser_raw.split(",") if x.strip()]
    tournament_host = org_parts[0] if org_parts else ""
    tournament_city = org_parts[-1] if len(org_parts) >= 2 else ""

    tid = str(tournament_id) if tournament_id else ""
    if not tid:
        code = info.get("Kódové číslo", "")
        cm = re.search(r"\d+", code)
        if cm:
            tid = cm.group(0)
    if not tid:
        first_link = sel.xpath('//a[contains(@href, "/turnaj/")]/@href').get("") or ""
        tid = _tournament_id_from_url(first_link)

    draw_gender = _detect_gender(sel, tournament_type, tid)

    return {
        "draw_name": draw_name,
        "draw_team_type": "Singles",
        "draw_gender": draw_gender,
        "draw_bracket_value": "",
        "draw_bracket_type": "",
        "draw_type": "",
        "ball_type": "Yellow",
        "tournament_name": tournament_name,
        "tournament_host": tournament_host,
        "tournament_city": tournament_city,
        "tournament_state": "",
        "tournament_country": "Czech",
        "tournament_country_code": "CZ",
        "tournament_location_type": "",
        "tournament_event_category": "",
        "tournament_event_grade": "",
        "tournament_import_source": "",
        "tournament_sanction_body": "",
        "tournament_event_type": "",
        "tournament_url": tournament_url,
        "tournament_start_date": tournament_start_date,
        "tournament_end_date": tournament_end_date,
        "date": match_date,
        "outcome": "Completed",
        "id_type": "Czech",
        "winner_1_country": "Czech",
        "loser_1_country": "Czech",
        "winner_2_country": "",
        "loser_2_country": "",
        "_tid": tid,
    }


# ---------------------------------------------------------------------------
# Player parsing (registration list "seznam" page)
# ---------------------------------------------------------------------------
def parse_players(sel, draw_gender):
    """Return ``{third_party_id: player_dict}`` for every registered player."""
    players = {}
    for row in sel.xpath("//table/tbody/tr"):
        tds = row.xpath("td")
        if len(tds) < 3:
            continue
        link = tds[1].xpath(".//a/@href").get("") or ""
        mid = re.search(r"/hrac/(\d+)", link)
        pid = mid.group(1) if mid else ""
        if not pid or pid in players:
            continue
        raw_name = (
            tds[1].xpath(".//a/@title").get("")
            or tds[1].xpath(".//a/text()").get("")
            or ""
        )
        name = _normalise_name(raw_name)
        yob = (tds[2].xpath("text()").get() or "").strip()
        players[pid] = {
            "name": name,
            "gender": draw_gender,
            "third_party_id": pid,
            "dob": f"1/1/{yob}" if re.fullmatch(r"\d{4}", yob) else "",
            "city": "",
            "state": "",
            "country": "Czech",
            "college": "",
        }
    return players


# ---------------------------------------------------------------------------
# Match-row / match-block parsing
# ---------------------------------------------------------------------------
def _row_side(row):
    """Extract player ids, names, set-scores and winner flag from one bracket row."""
    ids, names = [], []
    for a in row.css("span.name a"):
        href = a.attrib.get("href", "")
        mid = re.search(r"/hrac/(\d+)", href)
        raw = a.attrib.get("title") or " ".join(a.css("::text").getall())
        name = _normalise_name(raw)
        if mid:
            ids.append(mid.group(1))
            names.append(name)
        elif name:
            names.append(name)
    sets = []
    for td in row.css("td"):
        cls = _td_classes(td)
        if "result" in cls and "live-cell" not in cls:
            sets.append((td.css("::text").get() or "").strip())
    return {"ids": ids, "names": names, "sets": sets, "winner": bool(row.css("strong"))}


def _resolve_player(players, draw_gender, pid, fallback_name=""):
    p = players.get(pid)
    if p is None:
        return {
            "name": fallback_name, "gender": draw_gender,
            "third_party_id": pid, "dob": "",
            "city": "", "state": "", "country": "Czech", "college": "",
        }
    p = dict(p)
    if not p["name"]:
        p["name"] = fallback_name
    return p


def _build_score(win_sets, lose_sets):
    """Winner-perspective score, e.g. "6-1, 6-4;".

    Numeric columns are completed sets; a non-numeric cell (e.g. "scr.", "w.o")
    is a retirement/walkover marker, appended once after the completed sets.
    """
    pairs, status = [], ""
    for w, l in zip(win_sets, lose_sets):
        if w.isdigit() and l.isdigit():
            pairs.append(f"{w}-{l}")
        else:
            token = w if (w and not w.isdigit()) else (l if (l and not l.isdigit()) else "")
            if token:
                status = token
    score = ", ".join(pairs)
    if status:
        score = f"{score} {status}".strip()
    if score and not score.endswith(";"):
        score += ";"
    return score


def parse_match_block(match_sel, players, tournament, draw_team_type="Singles", position=0):
    """Parse one ``<div class="bracket__match">`` into ``(record, match_id)`` or ``None``."""
    rows = match_sel.css("tr.player-row")
    if len(rows) < 2:
        return None
    a = _row_side(rows[0])
    b = _row_side(rows[1])

    if not (a["ids"] or a["names"]) or not (b["ids"] or b["names"]):
        return None

    if b["winner"] and not a["winner"]:
        win, lose = b, a
    else:
        win, lose = a, b

    if not any(win["sets"]) and not any(lose["sets"]):
        return None

    score = _build_score(win["sets"], lose["sets"])

    team_type = "Doubles" if max(len(a["names"]), len(b["names"])) >= 2 else draw_team_type
    gender = tournament["draw_gender"]

    def side_player(side, idx):
        pid = side["ids"][idx] if idx < len(side["ids"]) else ""
        nm = side["names"][idx] if idx < len(side["names"]) else ""
        if not pid and not nm:
            return None
        return _resolve_player(players, gender, pid, nm)

    winner1 = side_player(win, 0)
    loser1 = side_player(lose, 0)
    winner2 = side_player(win, 1) if team_type == "Doubles" else None
    loser2 = side_player(lose, 1) if team_type == "Doubles" else None

    record = _build_match_record(tournament, winner1, loser1, score, team_type, winner2, loser2)

    # No per-match GUID in the markup, so build a stable dedup key from the
    # tournament id + bracket position + player ids.
    ids = [p["third_party_id"] for p in (winner1, winner2, loser1, loser2) if p]
    match_id = "-".join([tournament.get("_tid", ""), str(position)] + ids).strip("-")

    return record, match_id


def _build_match_record(tournament, winner, loser, score, draw_team_type, winner2, loser2):
    """Combine tournament fields + players into a full flat match record."""

    def player_fields(prefix, p):
        if p is None:
            return {
                f"{prefix}_name": "", f"{prefix}_gender": "",
                f"{prefix}_third_party_id": "", f"{prefix}_city": "",
                f"{prefix}_state": "", f"{prefix}_country": "",
                f"{prefix}_college": "", f"{prefix}_dob": "",
            }
        return {
            f"{prefix}_name": p["name"],
            f"{prefix}_gender": p["gender"],
            f"{prefix}_third_party_id": p["third_party_id"],
            f"{prefix}_city": p["city"],
            f"{prefix}_state": p["state"],
            f"{prefix}_country": p["country"],
            f"{prefix}_college": p["college"],
            f"{prefix}_dob": p["dob"],
        }

    rec = {
        "ball_type": tournament["ball_type"],
        "draw_bracket_value": tournament["draw_bracket_value"],
        "draw_name": tournament["draw_name"],
        "draw_team_type": draw_team_type,
        "date": tournament["date"],
        "score": score,
        "outcome": tournament["outcome"],
        "id_type": tournament["id_type"],
        "draw_gender": tournament["draw_gender"],
        "draw_bracket_type": tournament["draw_bracket_type"],
        "draw_type": tournament["draw_type"],
    }
    rec.update(player_fields("winner_1", winner))
    rec.update(player_fields("winner_2", winner2))
    rec.update(player_fields("loser_1", loser))
    rec.update(player_fields("loser_2", loser2))
    rec.update({
        "tournament_name": tournament["tournament_name"],
        "tournament_city": tournament["tournament_city"],
        "tournament_state": tournament["tournament_state"],
        "tournament_country": tournament["tournament_country"],
        "tournament_country_code": tournament["tournament_country_code"],
        "tournament_host": tournament["tournament_host"],
        "tournament_location_type": tournament["tournament_location_type"],
        "tournament_event_category": tournament["tournament_event_category"],
        "tournament_event_grade": tournament["tournament_event_grade"],
        "tournament_import_source": tournament["tournament_import_source"],
        "tournament_sanction_body": tournament["tournament_sanction_body"],
        "tournament_event_type": tournament["tournament_event_type"],
        "tournament_url": tournament["tournament_url"],
        "tournament_start_date": tournament["tournament_start_date"],
        "tournament_end_date": tournament["tournament_end_date"],
    })
    if draw_team_type == "Doubles":
        if winner2:
            rec["winner_2_country"] = winner2.get("country", "Czech")
        if loser2:
            rec["loser_2_country"] = loser2.get("country", "Czech")
    return rec


def _finalise_row(record, match_id):
    """Map a parsed record into a complete CSV row dict."""
    row = dict(record)
    row["match_id"] = match_id
    # The items schema spells the draw gender out; player genders stay M/F.
    row["draw_gender"] = "Female" if record.get("draw_gender") == "F" else "Male"
    row["tournament_surface"] = ""
    row.setdefault("round", "")
    return row


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _filter_by_years(text, filter_years):
    """Return the in-range season-name fragments (truthy when any match)."""
    result = []
    for item in [i.strip() for i in (text or "").split(",")]:
        found_years = set(int(y) for y in re.findall(r"\b\d{4}\b", item))
        if found_years and found_years.issubset(set(filter_years)):
            result.append(item)
    return result


def _season_index_link(menu_slug):
    return (
        f"{BASE}/jednotlivci/{menu_slug}"
        "?v=1&individualsControl-sort=datumOd&individualsControl-direction=asc"
    )


def _discover_seasons(client, log):
    """Return ``[{menu_slug, season_name, season_value}]`` for the current+prev year."""
    filter_years = [datetime.now().year, datetime.now().year - 1]
    seasons = []
    for menu_slug in MENU_SLUGS:
        sel = client.get_selector(_season_index_link(menu_slug))
        if sel is None:
            continue
        for opt in sel.xpath(
            '//select[@id="frm-individualsControl-individualsForm-sezona"]/option'
        ):
            season_name = _xp(opt, "./text()")
            season_value = _xp(opt, "./@value")
            if _filter_by_years(season_name, filter_years):
                seasons.append({
                    "menu_slug": menu_slug,
                    "season_name": season_name,
                    "season_value": season_value,
                })
    log("INFO", f"\U0001f5d3\ufe0f {len(seasons)} season listing(s) in range")
    return seasons


def _discover_tournaments(client, seasons, start_d, end_d, log):
    """Return ``[{tournament_url, tournament_type}]`` for draws inside the window."""
    tournaments = []
    seen_draws = set()
    for season in seasons:
        menu_slug = season["menu_slug"]
        season_value = season["season_value"]
        index_link = _season_index_link(menu_slug)
        data = {
            "sezona": season_value,
            "nazev": "",
            "_do": "individualsControl-individualsForm-submit",
        }
        resp = client.post(index_link, data=data)
        if resp is None or not (200 <= resp.status_code < 300):
            continue
        sel = client.selector(resp)
        # Column 5 = men's draw link, column 6 = women's.
        for td_index, ttype in ((5, "male"), (6, "female")):
            for d1 in sel.xpath('//table[@class="table__table"]//tr[not(th)]'):
                tdate = _xp(d1, "./td[1]/text()[1]")
                turl = _xp(
                    d1,
                    f'./td[{td_index}]/span[@class="icons"]'
                    '/a[contains(@href, "tab=seznam")]/@href',
                )
                if not (tdate and turl):
                    continue
                tid_match = re.search(r"/turnaj/(\d+)", turl)
                dedup_key = (tid_match.group(1), ttype) if tid_match else (turl, ttype)
                if dedup_key in seen_draws:
                    continue
                try:
                    included = is_range_included(tdate, start_d, end_d)
                except ValueError:
                    continue
                if included:
                    seen_draws.add(dedup_key)
                    # Resolve to an absolute URL so query params (sezona) survive.
                    full_url = turl if turl.startswith("http") else urljoin(BASE + "/", turl.lstrip("/"))
                    tournaments.append({
                        "tournament_url": full_url,
                        "tournament_type": ttype,
                    })
    log("INFO", f"\U0001f50e {len(tournaments)} tournament draw(s) in window")
    return tournaments


def _scrape_tournament(client, tournament):
    """Fetch a draw's seznam + results pages and return a list of CSV row dicts."""
    tournament_url = tournament.get("tournament_url", "")
    tournament_type = tournament.get("tournament_type", "")
    tid, results_url, seznam_url = _build_urls(tournament_url)
    if not tid:
        return []

    seznam_sel = client.get_selector(seznam_url)
    if seznam_sel is None:
        return []
    results_sel = client.get_selector(results_url)
    if results_sel is None:
        return []

    info = parse_tournament(seznam_sel, tid, tournament_url, tournament_type)
    players = parse_players(seznam_sel, info["draw_gender"])

    rows = []
    for position, match_div in enumerate(results_sel.css("div.bracket__match")):
        parsed = parse_match_block(match_div, players, info, position=position)
        if not parsed:
            continue
        record, match_id = parsed
        rows.append(_finalise_row(record, match_id))
    return rows


def run(run_obj, log):
    """Execute the Czech tennis scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    params = run_obj.params or {}
    tournament_url = (params.get("tournament_url") or "").strip()

    if tournament_url:
        log("INFO", "\U0001f3be Czech tennis starting \u2014 single tournament URL")
        start_d = end_d = None
    else:
        start_d = run_obj.date_from or timezone.localdate()
        end_d = run_obj.date_to or timezone.localdate()
        log("INFO", f"\U0001f3be Czech tennis starting \u2014 {start_d} \u2192 {end_d}")
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")
    proxies = build_proxies(scraper, log)

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 discovering tournaments \u2500\u2500\u2500\u2500")
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        if tournament_url:
            tournaments = [{"tournament_url": tournament_url, "tournament_type": ""}]
        else:
            seasons = _discover_seasons(discovery, log)
            tournaments = _discover_tournaments(discovery, seasons, start_d, end_d, log)

    total = len(tournaments)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} tournament draw(s) to scrape")

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
                    f"@ {row.get('tournament_name') or 'Czech tennis'}",
                )
        except Exception as exc:  # noqa: BLE001 - one bad draw can't kill the run
            tele.record_error(
                redact_secrets(
                    f"Tournament {tournament.get('tournament_url', '')} failed: {exc}"
                ),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(f"\u26a0\ufe0f draw failed: {exc.__class__.__name__}: {exc}"),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
            client.close()

    if tournaments:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping tournaments \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, tournaments))

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
