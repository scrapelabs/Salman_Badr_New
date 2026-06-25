"""PrestoSports (college dual-match) scraper.

Ports the production ``prestosports`` spider onto MatchMiner's shared HTTP client
(:mod:`accounts.live_scrapers._http`) + telemetry. PrestoSports is a **JSON API**
that wraps an XML stats document, so this scraper authenticates with JSON, lists
events as JSON, then parses the XML embedded inside each event's stats JSON.

Input is a **date range** (``date_from`` / ``date_to``); the API filters events
to that window server-side via its ``from`` / ``to`` query params.

Flow (over ``gameday-api.prestosports.com``):

1. ``POST /api/auth/token`` with ``{username, password}`` and read ``idToken``
   (the bearer for every subsequent request).
2. For each season (men + women) paginate
   ``GET /api/seasons/{season_id}/events?from&to&pageSize&pageNumber`` and keep
   completed ("Final") events that have a non-zero team score, recording the
   event id + gender (``mten`` → Male/M, ``wten`` → Female/F).
3. Per event (concurrently): ``GET /api/events/{event_id}/stats/`` returns XML
   embedded in JSON (``data.xml``); that XML is parsed for the singles/doubles
   match rows, one CSV row per played match.

**Deterministic / AI-free port.** The original fed every team/college name through
an OpenAI "official name" lookup; that is dropped — the raw ``team/@name`` from
the stats XML is used as-is for both the embedded ``tournament_name`` and the
per-player ``*_college`` fields.

Credentials come from ``settings.PRESTOSPORTS_USERNAME`` /
``settings.PRESTOSPORTS_PASSWORD`` (env vars). They are **never** hard-coded or
logged; when unset the run fails honestly (like the Ioncourt scraper without its
credentials).

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import io
import math
import threading
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db.models import F
from parsel import Selector

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

LOGIN_URL = "https://gameday-api.prestosports.com/api/auth/token"
EVENTS_URL = "https://gameday-api.prestosports.com/api/seasons/{season_id}/events"
STATS_URL = "https://gameday-api.prestosports.com/api/events/{event_id}/stats/"

# The events API paginates; the source counts with ceil(totalElements / 100) and
# then fetches 100 events per page, so the page size is fixed at 100.
PAGE_SIZE = 100

# The two NAIA tennis seasons the source walks (men's + women's). These are
# public season identifiers, not secrets, so they stay as constants.
SEASONS = (
    {"season_type": "men", "season_id": "kxetzl72d99ilqa9"},
    {"season_type": "women", "season_id": "5ubesra0lj72h4jd"},
)

# Items CSV columns — the shared MatchMiner items schema (same as Czech/Ioncourt),
# so downloaded files stay uniform across scrapers. PrestoSports' dual-match extras
# (raw team labels) survive via the per-player ``*_college`` fields and the embedded
# ``tournament_name`` ("Dual Match: A vs B - Male").
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
# XML / score helpers (deterministic ports of the source's parsers)
# ---------------------------------------------------------------------------
def _parse_field(node, query):
    """Return ``normalize-space(query)`` against ``node`` (an XML selector), or ''."""
    try:
        return node.xpath(f"normalize-space({query})").get() or ""
    except Exception:  # noqa: BLE001 - a bad xpath/node is non-fatal
        return ""


def _event_score(a, b):
    """Format the dual-match team score larger-first, e.g. (4, 3) -> "4-3;".

    Returns "" when either side is zero/empty — the source uses this both as the
    team score and as the "this event actually finished" gate.
    """
    try:
        if not a or not b:
            return ""
        a_int, b_int = int(a), int(b)
        return f"{max(a_int, b_int)}-{min(a_int, b_int)};"
    except Exception:  # noqa: BLE001 - malformed score is non-fatal
        return ""


def _reverse_set(input_string):
    """"v-h" -> "max-min" (winner-perspective set score), or '' on bad input."""
    try:
        a, b = map(int, input_string.split("-"))
        return f"{max(a, b)}-{min(a, b)}"
    except Exception:  # noqa: BLE001 - odd set value is non-fatal
        return ""


def _filter_valid_sets(sets1, sets2):
    """Keep only set pairs where at least one side has games (both > 0 dropped-out)."""
    out = []
    for s1, s2 in zip(sets1, sets2):
        if int(s1) > 0 or int(s2) > 0:
            out.append((int(s1), int(s2)))
    return out


def _build_teams_and_players(sel):
    """Return ``(teams, player_map, player_by_name)`` from the stats XML.

    - ``teams``: ``{vh: {"name": college, "total_won": singles+doubles wins}}``
    - ``player_map``: ``{(vh, uni): {playerId, name, college}}``
    - ``player_by_name``: ``{(vh, lower(name)): {playerId, name, college}}``
    """
    teams = {}
    for team_el in sel.xpath("//team"):
        vh = _parse_field(team_el, "./@vh")
        if not vh:
            continue
        name = _parse_field(team_el, "./@name")
        singles_won = int(_parse_field(team_el, "./totals/singles/@won") or 0)
        doubles_won = int(_parse_field(team_el, "./totals/doubles/@won") or 0)
        teams[vh] = {"name": name, "total_won": singles_won + doubles_won}

    player_map = {}
    player_by_name = {}
    for team_el in sel.xpath("//team"):
        vh = _parse_field(team_el, "./@vh")
        college_name = _parse_field(team_el, "./@name")
        for p in team_el.xpath("./player"):
            uni = _parse_field(p, "./@uni")
            pid = _parse_field(p, "./@playerId")
            name = _parse_field(p, "./@name")
            if uni:
                player_map[(vh, uni)] = {"playerId": pid, "name": name, "college": college_name}
            if name:
                player_by_name[(vh, name.strip().lower())] = {
                    "playerId": pid, "name": name, "college": college_name,
                }
    return teams, player_map, player_by_name


def _resolve_player(vh, uni, name, player_map, player_by_name):
    """Look a player up by (vh, uni) then (vh, name); fall back to a bare name."""
    uni = (uni or "").strip()
    name_key = (name or "").strip().lower()
    if uni and (vh, uni) in player_map:
        return player_map[(vh, uni)]
    if name_key and (vh, name_key) in player_by_name:
        return player_by_name[(vh, name_key)]
    return {"playerId": "", "name": (name or "").strip(), "college": ""}


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
def _build_row(event, event_url, date_str, tournament_name, match_no,
               draw_team_type, outcome, score, winner1, winner2, loser1, loser2):
    """Combine a single parsed match into a complete CSV row dict.

    ``winner2`` / ``loser2`` are ``None`` for singles (their slots stay empty).
    The draw gender is spelled out ("Male"/"Female"); player genders stay "M"/"F".
    """
    eg_long = event["event_gender_long"]
    eg_short = event["event_gender_short"]
    event_id = event["event_id"]

    def player_fields(prefix, p):
        if not p:
            return {
                f"{prefix}_name": "", f"{prefix}_gender": "",
                f"{prefix}_dob": "", f"{prefix}_third_party_id": "",
                f"{prefix}_city": "", f"{prefix}_state": "",
                f"{prefix}_country": "", f"{prefix}_college": "",
            }
        return {
            f"{prefix}_name": p.get("name", ""),
            f"{prefix}_gender": eg_short,
            f"{prefix}_dob": "",
            f"{prefix}_third_party_id": p.get("playerId", ""),
            f"{prefix}_city": "", f"{prefix}_state": "", f"{prefix}_country": "",
            f"{prefix}_college": p.get("college", ""),
        }

    ids = [p.get("playerId", "") for p in (winner1, winner2, loser1, loser2) if p]
    match_id = "-".join(
        [event_id, draw_team_type, str(match_no)] + [i for i in ids if i]
    ).strip("-")

    row = {
        "match_id": match_id,
        "ball_type": "Yellow",
        "id_type": "Presto Sports",
        "draw_bracket_value": "",
        "draw_name": f"#{match_no} {draw_team_type}",
        "draw_team_type": draw_team_type,
        "tournament_name": tournament_name,
        "date": date_str,
        "round": "",
        "score": score,
        "outcome": outcome,
        "draw_gender": eg_long,
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
        "tournament_import_source": "College",
        "tournament_sanction_body": "College",
        "tournament_event_type": "Dual Match",
        "tournament_url": event_url,
        "tournament_country": "USA",
        "tournament_start_date": date_str,
        "tournament_end_date": date_str,
    }
    row.update(player_fields("winner_1", winner1))
    row.update(player_fields("winner_2", winner2))
    row.update(player_fields("loser_1", loser1))
    row.update(player_fields("loser_2", loser2))
    return row


def _parse_event(sel, event, event_url):
    """Parse one stats XML document into a list of CSV row dicts."""
    rows = []
    date_str = _parse_field(sel, "//tngame/@generated")

    teams, player_map, player_by_name = _build_teams_and_players(sel)
    if not teams:
        return rows

    winner_vh = max(teams, key=lambda vh: teams[vh]["total_won"])
    loser_vh = min(teams, key=lambda vh: teams[vh]["total_won"])
    winner_team = teams[winner_vh]["name"]
    loser_team = teams[loser_vh]["name"]
    # Deterministic fallback: raw team/@name (the OpenAI normalization is dropped).
    tournament_name = (
        f"Dual Match: {winner_team} vs {loser_team} - {event['event_gender_long']}"
    )

    tied_singles = {
        int(_parse_field(p, "./@pair") or 0): int(_parse_field(p, "./@tied") or 0)
        for p in sel.xpath('//team[@vh="V"]/totals/singles/singles_pair')
    }
    tied_doubles = {
        int(_parse_field(p, "./@pair") or 0): int(_parse_field(p, "./@tied") or 0)
        for p in sel.xpath('//team[@vh="V"]/totals/doubles/doubles_pair')
    }

    # ---- singles -------------------------------------------------------
    for m in sel.xpath("//singles_matches/singles_match"):
        match_no = int(_parse_field(m, "./@match") or 0)
        score_data = {}
        for sc in m.xpath("./singles_score"):
            vh = _parse_field(sc, "./@vh")
            uni = _parse_field(sc, "./@uni")
            name = _parse_field(sc, "./@name")
            pdata = _resolve_player(vh, uni, name, player_map, player_by_name)
            sets = [int(_parse_field(sc, f"./@set_{i}") or 0) for i in range(1, 4)]
            score_data[vh] = {
                "name": pdata.get("name", name),
                "playerId": pdata.get("playerId", ""),
                "college": pdata.get("college", teams.get(vh, {}).get("name", "")),
                "sets": sets,
            }

        v_sets = score_data.get("V", {}).get("sets", [0, 0, 0])
        h_sets = score_data.get("H", {}).get("sets", [0, 0, 0])
        valid_sets = _filter_valid_sets(v_sets, h_sets)

        status = "tie" if tied_singles.get(match_no, 0) == 1 else "completed"
        if status == "completed":
            v_wins = sum(v > h for v, h in valid_sets)
            h_wins = sum(h > v for v, h in valid_sets)
            winner_side, loser_side = ("V", "H") if v_wins > h_wins else ("H", "V")
            outcome = "Completed"
            score = ", ".join(_reverse_set(f"{v}-{h}") for v, h in valid_sets)
        else:
            winner_side, loser_side = "V", "H"
            outcome = "Tie"
            score = ", ".join(f"{v}-{h}" for v, h in valid_sets)
        if score:
            score += ";"

        winner_info = score_data.get(winner_side, {})
        loser_info = score_data.get(loser_side, {})
        winner1 = {
            "name": winner_info.get("name", ""),
            "playerId": winner_info.get("playerId", ""),
            "college": winner_info.get("college", teams.get(winner_side, {}).get("name", "")),
        }
        loser1 = {
            "name": loser_info.get("name", ""),
            "playerId": loser_info.get("playerId", ""),
            "college": loser_info.get("college", teams.get(loser_side, {}).get("name", "")),
        }
        rows.append(_build_row(
            event, event_url, date_str, tournament_name, match_no,
            "Singles", outcome, score, winner1, None, loser1, None,
        ))

    # ---- doubles -------------------------------------------------------
    for m in sel.xpath("//doubles_matches/doubles_match"):
        match_no = int(_parse_field(m, "./@match") or 0)

        v_list = m.xpath('./doubles_score[@vh="V"]')
        h_list = m.xpath('./doubles_score[@vh="H"]')
        if not v_list or not h_list:
            continue
        v_sc, h_sc = v_list[0], h_list[0]

        v_p1 = _resolve_player("V", _parse_field(v_sc, "./@uni_1"), _parse_field(v_sc, "./@name_1"), player_map, player_by_name)
        v_p2 = _resolve_player("V", _parse_field(v_sc, "./@uni_2"), _parse_field(v_sc, "./@name_2"), player_map, player_by_name)
        h_p1 = _resolve_player("H", _parse_field(h_sc, "./@uni_1"), _parse_field(h_sc, "./@name_1"), player_map, player_by_name)
        h_p2 = _resolve_player("H", _parse_field(h_sc, "./@uni_2"), _parse_field(h_sc, "./@name_2"), player_map, player_by_name)

        v_sets = [int(_parse_field(v_sc, f"./@set_{i}") or 0) for i in range(1, 4)]
        h_sets = [int(_parse_field(h_sc, f"./@set_{i}") or 0) for i in range(1, 4)]
        valid_sets = _filter_valid_sets(v_sets, h_sets)

        status = "tie" if tied_doubles.get(match_no, 0) == 1 else "completed"
        if status == "completed":
            v_wins = sum(v > h for v, h in valid_sets)
            h_wins = sum(h > v for v, h in valid_sets)
            if v_wins > h_wins:
                winner_side, loser_side = "V", "H"
                w1, w2, l1, l2 = v_p1, v_p2, h_p1, h_p2
            else:
                winner_side, loser_side = "H", "V"
                w1, w2, l1, l2 = h_p1, h_p2, v_p1, v_p2
            outcome = "Completed"
            score = ", ".join(_reverse_set(f"{v}-{h}") for v, h in valid_sets)
        else:
            # Tie: keep V in the "winner" slot (mirrors the source), ids still parsed.
            winner_side, loser_side = "V", "H"
            w1, w2, l1, l2 = v_p1, v_p2, h_p1, h_p2
            outcome = "Tie"
            score = ", ".join(f"{v}-{h}" for v, h in valid_sets)
        if score:
            score += ";"

        winner1 = {
            "name": w1.get("name", ""), "playerId": w1.get("playerId", ""),
            "college": w1.get("college", teams.get(winner_side, {}).get("name", "")),
        }
        winner2 = {
            "name": w2.get("name", ""), "playerId": w2.get("playerId", ""),
            "college": w2.get("college", teams.get(winner_side, {}).get("name", "")),
        }
        loser1 = {
            "name": l1.get("name", ""), "playerId": l1.get("playerId", ""),
            "college": l1.get("college", teams.get(loser_side, {}).get("name", "")),
        }
        loser2 = {
            "name": l2.get("name", ""), "playerId": l2.get("playerId", ""),
            "college": l2.get("college", teams.get(loser_side, {}).get("name", "")),
        }
        rows.append(_build_row(
            event, event_url, date_str, tournament_name, match_no,
            "Doubles", outcome, score, winner1, winner2, loser1, loser2,
        ))

    return rows


# ---------------------------------------------------------------------------
# API calls
# ---------------------------------------------------------------------------
def _login(client, username, password):
    """Authenticate and return the ``idToken``, or ''."""
    resp = client.post(
        LOGIN_URL,
        headers={"accept": "*/*", "Content-Type": "application/json"},
        json={"username": username, "password": password},
    )
    if resp is not None and 200 <= resp.status_code < 300:
        try:
            return resp.json().get("idToken", "") or ""
        except Exception:  # noqa: BLE001 - body wasn't JSON
            return ""
    return ""


def _fetch_events_page(client, token, season_id, start_str, end_str, page):
    """Fetch one page of a season's events, or ``None``."""
    headers = {"accept": "application/json", "Authorization": f"Bearer {token}"}
    params = {
        "from": start_str, "to": end_str,
        "pageSize": PAGE_SIZE, "pageNumber": page,
    }
    return client.get_json(EVENTS_URL.format(season_id=season_id), headers=headers, params=params)


def _harvest(results, season_type, season_id, events, seen):
    """Collect completed, in-window events from one events page into ``events``."""
    for result in results.get("data", []):
        event_id = result.get("eventId", "")
        if not event_id or event_id in seen:
            continue
        score = result.get("score", {}) or {}
        event_score = _event_score(score.get("home", 0), score.get("away", 0))
        if not event_score:
            continue
        sport = result.get("sportId", "")
        if sport == "mten":
            g_long, g_short = "Male", "M"
        elif sport == "wten":
            g_long, g_short = "Female", "F"
        else:
            g_long, g_short = "", ""
        if (result.get("status", "") or "").lower() != "final":
            continue
        seen.add(event_id)
        events.append({
            "season_type": season_type,
            "season_id": season_id,
            "event_id": event_id,
            "event_gender_long": g_long,
            "event_gender_short": g_short,
            "event_score": event_score,
        })


def _discover_events(client, token, start_str, end_str, log):
    """Return ``[event_dict]`` for every completed event inside the window."""
    events = []
    seen = set()
    for season in SEASONS:
        season_type = season["season_type"]
        season_id = season["season_id"]
        first = _fetch_events_page(client, token, season_id, start_str, end_str, 0)
        if not first:
            continue
        total = first.get("totalElements", 0) or 0
        pages = math.ceil(total / PAGE_SIZE) if total else 0
        log("INFO", f"\U0001f50e {total} {season_type} event(s) across {pages} page(s)")
        _harvest(first, season_type, season_id, events, seen)
        for page in range(1, pages):
            results = _fetch_events_page(client, token, season_id, start_str, end_str, page)
            if results:
                _harvest(results, season_type, season_id, events, seen)
    return events


def _scrape_event(client, token, event):
    """Fetch an event's stats XML and return its list of CSV row dicts."""
    event_id = event.get("event_id", "")
    if not event_id:
        return []
    event_url = STATS_URL.format(event_id=event_id)
    headers = {"accept": "application/json", "Authorization": f"Bearer {token}"}
    data = client.get_json(event_url, headers=headers)
    if not data:
        return []
    try:
        xml_str = data["data"]["xml"]
    except (KeyError, TypeError):
        return []
    if not xml_str:
        return []
    sel = Selector(text=xml_str, type="xml")
    return _parse_event(sel, event, event_url)


def run(run_obj, log):
    """Execute the PrestoSports scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    start_d = run_obj.date_from
    end_d = run_obj.date_to

    log(
        "INFO",
        f"\U0001f3be PrestoSports (college dual matches) starting \u2014 window "
        f"{start_d} \u2192 {end_d}",
    )
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    if not (start_d and end_d):
        msg = "PrestoSports needs a date range (date_from / date_to) to filter events."
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    username = (getattr(settings, "PRESTOSPORTS_USERNAME", "") or "").strip()
    password = getattr(settings, "PRESTOSPORTS_PASSWORD", "") or ""
    if not (username and password):
        msg = (
            "PrestoSports credentials missing \u2014 set PRESTOSPORTS_USERNAME and "
            "PRESTOSPORTS_PASSWORD (Replit secrets, or the local .env) to enable "
            "this source."
        )
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    start_str = start_d.strftime("%Y-%m-%d")
    end_str = end_d.strftime("%Y-%m-%d")

    proxies = build_proxies(scraper, log)

    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 login + discovering events \u2500\u2500\u2500\u2500")
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        token = _login(discovery, username, password)
        if not token:
            msg = "PrestoSports login failed \u2014 no idToken returned (check credentials)."
            log("ERROR", f"\U0001f6d1 {msg}")
            tele.record_error(msg)
            return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED
        log("INFO", "\U0001f511 Authenticated \u2014 idToken acquired")
        events = _discover_events(discovery, token, start_str, end_str, log)

    total = len(events)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} event(s) in window")

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def process(event):
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            rows = _scrape_event(client, token, event)
            for row in rows:
                key = row.get("match_id", "")
                with lock:
                    if key and key in seen:
                        continue
                    seen.add(key)
                    writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
                    counter["rows"] += 1
                log(
                    "INFO",
                    f"   \U0001f3be {row.get('draw_team_type', '')}: "
                    f"{row.get('winner_1_name') or '?'} def. "
                    f"{row.get('loser_1_name') or '?'} [{row.get('score', '')}] "
                    f"\u2014 {row.get('tournament_name') or 'PrestoSports'}",
                )
        except Exception as exc:  # noqa: BLE001 - one bad event can't kill the run
            tele.record_error(
                redact_secrets(f"Event {event.get('event_id', '')} failed: {exc}"),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(f"\u26a0\ufe0f event failed: {exc.__class__.__name__}: {exc}"),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
            client.close()

    if events:
        log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 scraping events \u2500\u2500\u2500\u2500")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, events))

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
