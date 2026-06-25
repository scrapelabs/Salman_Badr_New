"""Tennis Australia (Azure Blob result submissions) scraper.

Ports the production ``australia_tennis`` pipeline onto MatchMiner's shared HTTP
client (:mod:`accounts.live_scrapers._http`) + telemetry. Tennis Australia drops
one JSON "result submission" file per played fixture into an Azure Blob Storage
container (``result-submissions``), under the ``tennis_australia/`` virtual
folder. Each blob is named ``YYYYMMDD_HHMMSSmmm.json`` — the leading
``YYYYMMDD`` is the filename date the source filters on.

Flow (over the Azure Blob REST API, no ``azure-storage-blob`` dependency):

1. **List** — ``GET <container>?restype=container&comp=list&prefix=tennis_australia/``
   returns an ``<EnumerationResults>`` XML document of ``<Blob>`` entries; the
   ``<NextMarker>`` is followed for pagination. Each blob's ``<Name>`` (and
   ``<Properties><Last-Modified>``) is read.
2. **Filter** — keep only ``.json`` blobs whose filename date (``YYYYMMDD``)
   falls inside the run window ``[date_from, date_to]`` (inclusive), exactly the
   field the source's ``file_date__range`` filter uses.
3. **Download + parse** — per blob (concurrently): ``GET <container>/<blobname>``,
   ``json.loads`` the body, navigate ``data["Matches"]["Match"]`` and emit one
   CSV row per match (singles or doubles).

The JSON→row logic is a faithful, **deterministic / AI-free** port of
``parser/details.py`` and ``mohamed/parse_blob (FIXED).py``: ``build_name`` →
"Last, First"; ``normalize_gender`` (Male→M / Female→F, numeric kept verbatim,
else blank); ``build_score`` (winner side first, tie-break sets rendered
"G1-G2 (TB1-TB2)"); ``choose_third_party_id`` prefers ``TennisConnectID`` (id
type "Tennis Connect") then ``UniqueID`` (id type "Tennis Australia"); college
fields are always "". The single ``id_type`` column carries the first (winner 1)
player's id type — the source stored one id type per player but the shared
items schema has only one column.

The SAS URL is **never** hard-coded — it is read from
``settings.AUSTRALIA_TENNIS_SAS_URL`` and split into a container URL + SAS query
with :mod:`urllib.parse`. The SAS query (which carries the ``sig`` signature) is
passed to the client as request *params*, never embedded in the logged/recorded
URL, so neither the request CSV nor the run log can leak it.

``run(run_obj, log)`` returns the standard ``(items_csv, requests_csv,
errors_csv, row_count, status)`` tuple.
"""

import csv
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from django.conf import settings
from django.db.models import F
from parsel import Selector

from accounts.models import Run

from ._http import ScraperClient, build_proxies
from .telemetry import Telemetry, redact_secrets, sanitize_cell

# The Azure "folder" the result submissions live under.
PREFIX = "tennis_australia/"

# Items CSV columns — the shared MatchMiner items schema (same as Czech/Presto),
# so downloaded files stay uniform across scrapers.
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
# Resilient field access (deterministic ports of the source's helpers)
# ---------------------------------------------------------------------------
def _lookup_case_insensitive(d, key):
    key_lower = key.lower()
    for k, v in d.items():
        if k.lower() == key_lower:
            return True, v
    return False, None


def _safe_get(match, key):
    """Exact key, then case-insensitive fallback; ``None`` becomes ''."""
    if key in match:
        val = match[key]
    else:
        found, val = _lookup_case_insensitive(match, key)
        if not found:
            return ""
    return "" if val is None else val


def _safe_int(match, key):
    raw = _safe_get(match, key)
    if raw == "":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(float(str(raw)))
        except Exception:  # noqa: BLE001 - any non-numeric value counts as 0
            return 0


def _safe_bool(match, key):
    raw = _safe_get(match, key)
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in {"1", "true", "t", "yes", "y"}:
        return True
    if s in {"0", "false", "f", "no", "n"}:
        return False
    return False


# ---------------------------------------------------------------------------
# Date / name / gender helpers
# ---------------------------------------------------------------------------
def _parse_date_to_mmddyyyy(value):
    """Robust date parser → 'MM/DD/YYYY'.

    Handles 'YYYY-MM-DD', ISO datetimes (with 'Z'/offsets/fractional seconds)
    and 'YYYY-MM-DD HH:MM:SS'. Returns '' when empty/invalid or for the
    placeholder year ≤ 1.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s or s.lower() == "null":
        return ""

    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            if dt.year <= 1:
                return ""
            return dt.strftime("%m/%d/%Y")
        except Exception:  # noqa: BLE001 - fall through to the other formats
            pass

    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt).date()
            if dt.year <= 1:
                return ""
            return dt.strftime("%m/%d/%Y")
        except Exception:  # noqa: BLE001 - try the next format
            continue

    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso).date()
        if dt.year <= 1:
            return ""
        return dt.strftime("%m/%d/%Y")
    except Exception:  # noqa: BLE001 - unparseable date
        return ""


def _normalize_gender(g):
    """'Male'→'M', 'Female'→'F', numeric kept verbatim, else ''."""
    if g is None:
        return ""

    if isinstance(g, (int, float)) and not isinstance(g, bool):
        try:
            if float(g).is_integer():
                return str(int(g))
            return str(g)
        except Exception:  # noqa: BLE001 - odd numeric, stringify as-is
            return str(g)

    s = str(g).strip()
    if not s or s.lower() == "null":
        return ""
    if s.isdigit():
        return s

    sl = s.lower()
    if sl == "male":
        return "M"
    if sl == "female":
        return "F"
    if sl in {"m", "f"}:
        return sl.upper()
    return ""


def _build_name(last_name, first_name):
    """'Lastname, Firstname' (either part may be missing)."""
    last_name = (last_name or "").strip()
    first_name = (first_name or "").strip()
    if not last_name and not first_name:
        return ""
    if not last_name:
        return first_name
    if not first_name:
        return last_name
    return f"{last_name}, {first_name}"


def _choose_third_party_id(match, prefix):
    """``TennisConnectID`` → (id, 'Tennis Connect'); else ``UniqueID`` →
    (id, 'Tennis Australia'); else ('', '')."""
    tc_str = str(_safe_get(match, prefix + "TennisConnectID")).strip()
    unique_str = str(_safe_get(match, prefix + "UniqueID")).strip()
    if tc_str.lower() == "null":
        tc_str = ""
    if unique_str.lower() == "null":
        unique_str = ""
    if tc_str:
        return tc_str, "Tennis Connect"
    if unique_str:
        return unique_str, "Tennis Australia"
    return "", ""


# ---------------------------------------------------------------------------
# Result-side / player-prefix logic
# ---------------------------------------------------------------------------
def _get_result_sides(match):
    """Return ``(winner_side, loser_side, outcome)`` — sides are "1"/"2"."""
    res = str(_safe_get(match, "Match_Result") or "").upper()
    if res == "TEAM1WIN":
        return "1", "2", "Completed"
    if res == "TEAM2WIN":
        return "2", "1", "Completed"
    return "1", "2", "Tie"


def _get_player_prefix(result, role, person):
    """Field prefix ('Player1Team1_' …) for a winner/loser side + person index."""
    res = (result or "").upper()
    if role == "winner":
        if person == 1:
            if res == "TEAM2WIN":
                return "Player1Team2_"
            return "Player1Team1_"
        if res == "TEAM2WIN":
            return "Player2Team2_"
        return "Player2Team1_"
    # loser
    if person == 1:
        if res == "TEAM2WIN":
            return "Player1Team1_"
        return "Player1Team2_"
    if res == "TEAM2WIN":
        return "Player2Team1_"
    return "Player2Team2_"


def _build_score(match):
    """Winner-side-first set score; tie-breaks render 'G1-G2 (TB1-TB2)'.

    A set is included when either side won games, the set is flagged a
    tie-break, or non-zero tie-break points exist.
    """
    winner_side, _, _ = _get_result_sides(match)
    first_side = winner_side
    second_side = "1" if winner_side == "2" else "2"

    parts = []
    for i in range(1, 8):
        g1 = _safe_int(match, f"Match_Team{first_side}Set{i}GamesWon")
        g2 = _safe_int(match, f"Match_Team{second_side}Set{i}GamesWon")
        is_tb = _safe_bool(match, f"Match_Set{i}IsTieBreak")
        tb1 = _safe_int(match, f"Match_Team{first_side}Set{i}TieBreakPoints")
        tb2 = _safe_int(match, f"Match_Team{second_side}Set{i}TieBreakPoints")

        if not (g1 != 0 or g2 != 0 or is_tb or tb1 != 0 or tb2 != 0):
            continue

        set_str = f"{g1}-{g2}"
        if is_tb or tb1 != 0 or tb2 != 0:
            set_str = f"{set_str} ({tb1}-{tb2})"
        parts.append(set_str)

    return ", ".join(parts)


def _build_tournament_url(match):
    data_type = str(_safe_get(match, "Match_DataType") or "").lower()
    league_id = str(_safe_get(match, "Match_LeagueID") or "").strip()
    if not league_id:
        return ""
    if data_type == "league":
        return f"https://matchcentre.tennis.com.au/divisions/{league_id}"
    if data_type == "tournament":
        return f"https://tournaments.tennis.com.au/tournament/{league_id}"
    return ""


def _is_doubles_match(match):
    """DOUBLES when ``Match_Type`` says so, or a second player exists either side."""
    if str(_safe_get(match, "Match_Type") or "").upper() == "DOUBLES":
        return True
    p2t1 = str(_safe_get(match, "Player2Team1_FirstName") or "").strip()
    p2t2 = str(_safe_get(match, "Player2Team2_FirstName") or "").strip()
    return bool(p2t1 or p2t2)


# ---------------------------------------------------------------------------
# Row assembly
# ---------------------------------------------------------------------------
def _extract_player(match, result_upper, role, person):
    """Return a player dict for one slot, or ``None``.

    Person-2 slots return ``None`` when both names are blank (the source leaves
    the second doubles player empty in that case). Person-1 slots are always
    returned (even if blank), mirroring the source.
    """
    prefix = _get_player_prefix(result_upper, role, person)
    first = str(_safe_get(match, prefix + "FirstName")).strip()
    last = str(_safe_get(match, prefix + "LastName")).strip()
    if person == 2 and not first and not last:
        return None
    tpid, id_type = _choose_third_party_id(match, prefix)
    return {
        "name": _build_name(last, first),
        "gender": _normalize_gender(_safe_get(match, prefix + "Gender")),
        "dob": _parse_date_to_mmddyyyy(_safe_get(match, prefix + "DateOfBirth")),
        "third_party_id": tpid,
        "id_type": id_type,
        "city": str(_safe_get(match, prefix + "Suburb")),
        "state": str(_safe_get(match, prefix + "AddressState")),
        "country": str(_safe_get(match, prefix + "Country")),
    }


def _player_cols(prefix, p):
    if not p:
        return {
            f"{prefix}_name": "", f"{prefix}_gender": "", f"{prefix}_dob": "",
            f"{prefix}_third_party_id": "", f"{prefix}_city": "",
            f"{prefix}_state": "", f"{prefix}_country": "",
        }
    return {
        f"{prefix}_name": p["name"],
        f"{prefix}_gender": p["gender"],
        f"{prefix}_dob": p["dob"],
        f"{prefix}_third_party_id": p["third_party_id"],
        f"{prefix}_city": p["city"],
        f"{prefix}_state": p["state"],
        f"{prefix}_country": p["country"],
    }


def _match_to_row(match, import_source_name):
    """Map one ``Match`` dict into a complete CSV row dict."""
    result_upper = str(_safe_get(match, "Match_Result") or "").upper()
    _, _, outcome = _get_result_sides(match)
    doubles = _is_doubles_match(match)

    score_raw = _build_score(match)
    score = score_raw + ";" if (score_raw and not score_raw.endswith(";")) else score_raw

    winner1 = _extract_player(match, result_upper, "winner", 1)
    loser1 = _extract_player(match, result_upper, "loser", 1)
    winner2 = _extract_player(match, result_upper, "winner", 2) if doubles else None
    loser2 = _extract_player(match, result_upper, "loser", 2) if doubles else None

    event_type_raw = str(_safe_get(match, "Match_DataType"))
    event_type = event_type_raw.capitalize() if event_type_raw else ""

    row = {
        "match_id": str(_safe_get(match, "Match_ID")),
        "ball_type": str(_safe_get(match, "Match_BallType")),
        # Single id_type column carries the first (winner 1) player's id type.
        "id_type": winner1["id_type"] if winner1 else "",
        "draw_bracket_value": "",
        "draw_name": str(_safe_get(match, "Match_DivisionName")),
        "draw_team_type": str(_safe_get(match, "Match_Type")),
        "tournament_name": str(_safe_get(match, "Match_LeagueName")),
        "date": _parse_date_to_mmddyyyy(_safe_get(match, "Match_Date")),
        "round": "",
        "score": score,
        "outcome": outcome,
        "draw_gender": "",
        "draw_bracket_type": "",
        "draw_type": "",
        "tournament_city": "",
        "tournament_state": "",
        "tournament_country_code": "AUS",
        "tournament_host": "",
        "tournament_location_type": "",
        "tournament_surface": "",
        "tournament_event_category": "",
        "tournament_event_grade": "",
        "tournament_import_source": import_source_name,
        "tournament_sanction_body": "Tennis Australia",
        "winner_2_college": "",
        "loser_2_college": "",
        "tournament_event_type": event_type,
        "winner_1_college": "",
        "loser_1_college": "",
        "tournament_url": _build_tournament_url(match),
        "tournament_country": "AUS",
        "tournament_start_date": "",
        "tournament_end_date": "",
    }
    row.update(_player_cols("winner_1", winner1))
    row.update(_player_cols("winner_2", winner2))
    row.update(_player_cols("loser_1", loser1))
    row.update(_player_cols("loser_2", loser2))
    return row


def extract_matches(data):
    """Navigate ``data["Matches"]["Match"]`` into a list of match dicts."""
    matches_obj = data.get("Matches") or data.get("matches") or {}
    if not isinstance(matches_obj, dict):
        return []
    matches = matches_obj.get("Match") or matches_obj.get("match") or []
    if isinstance(matches, dict):
        return [matches]
    if isinstance(matches, list):
        return matches
    return []


# ---------------------------------------------------------------------------
# Azure Blob REST helpers (no azure-storage-blob dependency)
# ---------------------------------------------------------------------------
def _split_sas_url(sas_url):
    """Split a container SAS URL into ``(container_url, sas_query_pairs)``.

    ``container_url`` has no query/fragment; ``sas_query_pairs`` is the parsed
    SAS query (list of tuples) so it can be sent as request *params* — keeping
    the ``sig`` out of any logged/recorded URL.
    """
    parsed = urlparse(sas_url)
    container_url = urlunparse(parsed._replace(query="", fragment=""))
    sas_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    return container_url, sas_pairs


def _file_date(blob_name):
    """Parse the filename date ('YYYYMMDD' prefix) into a ``date``, or ``None``."""
    file_name = blob_name.rsplit("/", 1)[-1]
    stem = file_name.split("_")[0]
    try:
        return datetime.strptime(stem, "%Y%m%d").date()
    except ValueError:
        return None


def _list_blobs(client, container_url, sas_pairs, log):
    """List every blob under ``PREFIX``, following ``NextMarker`` pagination.

    Returns ``[{"blob_name", "last_modified"}]``. The SAS query is passed as
    params so the recorded request URL never contains the signature.
    """
    blobs = []
    marker = ""
    pages = 0
    while True:
        params = [
            ("restype", "container"),
            ("comp", "list"),
            ("prefix", PREFIX),
        ]
        if marker:
            params.append(("marker", marker))
        params.extend(sas_pairs)

        resp = client.get(container_url, params=params)
        if resp is None or not (200 <= resp.status_code < 300):
            break

        sel = Selector(text=resp.text, type="xml")
        sel.remove_namespaces()
        for blob in sel.xpath("//Blob"):
            name = (blob.xpath("./Name/text()").get() or "").strip()
            if not name:
                continue
            last_mod = (
                blob.xpath("./Properties/*[name()='Last-Modified']/text()").get() or ""
            ).strip()
            blobs.append({"blob_name": name, "last_modified": last_mod})

        pages += 1
        marker = (sel.xpath("//NextMarker/text()").get() or "").strip()
        if not marker:
            break

    log("INFO", f"\U0001f5c2\ufe0f {len(blobs)} blob(s) across {pages} listing page(s)")
    return blobs


def _scrape_blob(client, container_url, sas_pairs, blob):
    """Download + parse one blob into a list of CSV row dicts."""
    blob_name = blob["blob_name"]
    # blob_name is not secret; the SAS query rides in params (never logged).
    blob_url = f"{container_url}/{quote(blob_name, safe='/')}"
    resp = client.get(blob_url, params=list(sas_pairs))
    if resp is None or not (200 <= resp.status_code < 300):
        return []
    try:
        data = json.loads(resp.content)
    except Exception:  # noqa: BLE001 - a non-JSON / corrupt blob is non-fatal
        return []

    import_source_name = blob_name.rsplit("/", 1)[-1]
    rows = []
    for match in extract_matches(data):
        if not isinstance(match, dict):
            continue
        rows.append(_match_to_row(match, import_source_name))
    return rows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(run_obj, log):
    """Execute the Tennis Australia scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    workers = scraper.worker_count
    start_d = run_obj.date_from
    end_d = run_obj.date_to

    log(
        "INFO",
        f"\U0001f3be Tennis Australia (Azure Blob results) starting \u2014 window "
        f"{start_d} \u2192 {end_d}",
    )
    log("INFO", f"\U0001f9f5 Concurrency: {workers} worker thread(s)")

    if not (start_d and end_d):
        msg = "Tennis Australia needs a date range (date_from / date_to) to filter result files."
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    sas_url = (getattr(settings, "AUSTRALIA_TENNIS_SAS_URL", "") or "").strip()
    if not sas_url:
        msg = "Set AUSTRALIA_TENNIS_SAS_URL to run the Australia Tennis scraper."
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    container_url, sas_pairs = _split_sas_url(sas_url)
    if not (container_url and sas_pairs):
        msg = (
            "AUSTRALIA_TENNIS_SAS_URL is malformed \u2014 expected "
            "'https://<acct>.blob.core.windows.net/result-submissions?<sas-query>'."
        )
        log("ERROR", f"\U0001f6d1 {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    proxies = build_proxies(scraper, log)

    # ---- phase 1 · discovery ------------------------------------------
    log("INFO", "\u2500\u2500\u2500\u2500 phase 1 \u00b7 listing result blobs \u2500\u2500\u2500\u2500")
    with ScraperClient(log=log, tele=tele, proxies=proxies) as discovery:
        all_blobs = _list_blobs(discovery, container_url, sas_pairs, log)

    # ---- filter by filename date (the field the source filters on) ----
    in_window = []
    seen_names = set()
    for blob in all_blobs:
        name = blob["blob_name"]
        file_name = name.rsplit("/", 1)[-1]
        if not file_name.lower().endswith(".json"):
            continue
        fd = _file_date(name)
        if fd is None or not (start_d <= fd <= end_d):
            continue
        if file_name in seen_names:
            continue
        seen_names.add(file_name)
        in_window.append(blob)

    total = len(in_window)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    log("INFO", f"\U0001f4cb {total} result file(s) in window")

    if not in_window:
        msg = (
            "No Tennis Australia result files found in the date window "
            f"{start_d} \u2192 {end_d} (filtered by the blob filename date)."
        )
        log("WARN", f"\u26a0\ufe0f {msg}")
        tele.record_error(msg)
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.FAILED

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    lock = threading.Lock()
    seen = set()
    counter = {"rows": 0}

    def process(blob):
        client = ScraperClient(log=log, tele=tele, proxies=proxies)
        try:
            rows = _scrape_blob(client, container_url, sas_pairs, blob)
            for row in rows:
                key = row.get("match_id") or (
                    row.get("tournament_import_source", ""),
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
                    f"@ {row.get('tournament_name') or 'Tennis Australia'}",
                )
        except Exception as exc:  # noqa: BLE001 - one bad file can't kill the run
            tele.record_error(
                redact_secrets(
                    f"Result file {blob.get('blob_name', '')} failed: {exc}"
                ),
                exc=exc,
            )
            log(
                "WARN",
                redact_secrets(f"\u26a0\ufe0f file failed: {exc.__class__.__name__}: {exc}"),
            )
        finally:
            Run.objects.filter(pk=run_obj.pk).update(progress_done=F("progress_done") + 1)
            client.close()

    log("INFO", "\u2500\u2500\u2500\u2500 phase 2 \u00b7 downloading + parsing files \u2500\u2500\u2500\u2500")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(process, in_window))

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
