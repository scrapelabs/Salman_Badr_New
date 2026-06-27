"""Tennis South Africa (SportyHQ) scraper — queue-driven over tournament keys.

Unlike the date/year sources, this scraper is **queue-driven**: it works through
a list of SportyHQ *tournament keys* (one 32-hex key per tournament), each of
which unlocks that tournament's full result set from the public SportyHQ results
API:

    GET https://www.sportyhq.com/api/result/search
        ?X-API-KEY=<public key>&tournament_key=<key>
    -> {"status": "success", "num_of_results": N, "data": [ {result}, ... ]}

The ``X-API-KEY`` is the site's **public** results-feed key (it travels in the
page's own client-side requests), so it's not secret — it rides in the request
URL and may appear in the requests CSV.

The work list comes from :class:`accounts.models.SAKey` rows. A run either
processes the pending queue (``run_all``) or an explicit set of pasted keys; in
both cases each key's :class:`SAKey` row is upserted and marked ``done`` /
``failed`` with its result count, so the Lab's "Key queue" tab shows progress.

Each result is mapped to the 64-column Tennis South Africa schema (see
``HEADER``). ``run(run_obj, log)`` returns the standard 5-tuple
``(items_csv, requests_csv, errors_csv, row_count, status)``.
"""

import csv
import io
from datetime import datetime

from django.db.models import F
from django.utils import timezone

from accounts.models import Run, SAKey

from ._http import ScraperClient, build_proxies
from .registry import KEY_BATCH_MAX_KEYS, KEY_BATCH_MAX_ROWS
from .telemetry import Telemetry, redact_secrets, sanitize_cell

# Public results-feed endpoint + key (not secret — used by the site's own
# client-side requests). Constructed by us, never user-supplied, so no SSRF
# allowlist is needed beyond ScraperClient's central public-IP guard.
API_BASE = "https://www.sportyhq.com/api/result/search"
API_KEY = "8d42a289cda5179b09952121126497ce"
TOURNAMENT_VIEW_BASE = "https://tsa.sportyhq.com/tournament/view/"

COUNTRY = "South Africa"
COUNTRY_CODE = "RSA"
IMPORT_SOURCE = "Tennis South Africa"
SANCTION_BODY = "Tennis South Africa"
BALL_TYPE = "Yellow"
EVENT_TYPE = "Tournament"

# Exact 64-column Tennis South Africa schema, header verbatim from the spec
# (NOT derived by title-casing snake_case — the wording is authoritative).
HEADER = [
    "Match ID", "Ball Type", "Draw Bracket Value", "Draw Name",
    "Draw Team Type", "Tournament Name", "Date", "Score",
    "Winner 1 Name", "Winner 1 Gender", "Winner 1 DOB", "Winner 1 Third Party ID",
    "Winner 1 ID Type", "Winner 1 City", "Winner 1 State", "Winner 1 Country",
    "Winner 2 Name", "Winner 2 Gender", "Winner 2 DOB", "Winner 2 Third Party ID",
    "Winner 2 ID Type", "Winner 2 City", "Winner 2 State", "Winner 2 Country",
    "Loser 1 Name", "Loser 1 Gender", "Loser 1 DOB", "Loser 1 Third Party ID",
    "Loser 1 ID Type", "Loser 1 City", "Loser 1 State", "Loser 1 Country",
    "Loser 2 Name", "Loser 2 Gender", "Loser 2 DOB", "Loser 2 Third Party ID",
    "Loser 2 ID Type", "Loser 2 City", "Loser 2 State", "Loser 2 Country",
    "Outcome", "Draw Gender", "Draw Bracket Type", "Draw Type",
    "Tournament City", "Tournament State", "Tournament Country Code",
    "Tournament Host", "Tournament Location Type", "Tournament Surface",
    "Tournament Event Category", "Tournament Event Grade",
    "Tournament Import Source", "Tournament Sanction Body",
    "Winner 2 College", "Loser 2 College", "Tournament Event Type",
    "Winner 1 College", "Loser 1 College", "Tournament URL",
    "Tournament Country", "Tournament Start Date", "Tournament End Date", "Key",
]
assert len(HEADER) == 64, f"expected 64 columns, got {len(HEADER)}"


def _norm_token(s):
    """Title-case ALL-CAPS tokens, leave already-mixed-case ones untouched.

    Player names arrive in inconsistent casing (some all-uppercase). Title-case
    a token only when it's fully uppercase so genuinely mixed-case surnames
    (``McCulloch``, ``van der Merwe``) survive intact.
    """
    return " ".join(t.title() if t.isupper() else t for t in (s or "").split())


def _last_first(user):
    """Build ``"Last, First Middle"`` from a SportyHQ user object."""
    if not user:
        return ""
    last = _norm_token(user.get("last_name"))
    first = _norm_token(user.get("first_name"))
    middle = _norm_token(user.get("middle_name"))
    given = " ".join(p for p in (first, middle) if p)
    if last and given:
        return f"{last}, {given}"
    return last or given


def _gender(user):
    g = (user or {}).get("gender") or ""
    g = g.strip().lower()
    if g == "male":
        return "M"
    if g == "female":
        return "F"
    return ""


def _dob(user):
    yob = (user or {}).get("year_of_birth")
    return str(yob).strip() if yob not in (None, "") else ""


def _third_party_id(user):
    uid = (user or {}).get("user_id")
    return str(uid).strip() if uid not in (None, "") else ""


def _us_date(raw):
    """``"2020-09-27"`` (optionally with a time) -> ``"09/27/2020"``."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    head = raw.split(" ")[0].split("T")[0]
    try:
        return datetime.strptime(head, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return raw


def _tournament_url(name):
    name = (name or "").strip()
    if not name:
        return ""
    return TOURNAMENT_VIEW_BASE + "-".join(name.split())


def _player_cols(user):
    """The 8 per-player columns (Name, Gender, DOB, Third Party ID, ID Type,
    City, State, Country) for one user, or 8 blanks when absent."""
    if not user:
        return ["", "", "", "", "", "", "", ""]
    name = _last_first(user)
    return [
        name,
        _gender(user),
        _dob(user),
        _third_party_id(user),
        "",                      # ID Type — always blank
        "",                      # City — always blank
        "",                      # State — always blank
        COUNTRY if name else "",
    ]


def _teams(result):
    """Resolve (winner_a, winner_b, loser_a, loser_b) user objects.

    Singles: two users (u1 vs u2). Doubles: detected when both user_3 AND
    user_4 are present — team1 = (u1, u2), team2 = (u3, u4). ``winner`` (1/2)
    selects which side won.
    """
    u1 = result.get("user_1")
    u2 = result.get("user_2")
    u3 = result.get("user_3")
    u4 = result.get("user_4")
    is_doubles = bool(u3) and bool(u4)
    try:
        winner = int(result.get("winner"))
    except (TypeError, ValueError):
        winner = 1
    if is_doubles:
        team1, team2 = (u1, u2), (u3, u4)
        if winner == 2:
            return team2[0], team2[1], team1[0], team1[1]
        return team1[0], team1[1], team2[0], team2[1]
    # Singles — no second player on either side.
    if winner == 2:
        return u2, None, u1, None
    return u1, None, u2, None


def _row_for(result, key):
    """Map one SportyHQ result dict to the 64-column row list."""
    tournament = result.get("tournament") or {}
    draw = tournament.get("draw") or {}
    club = result.get("club") or {}

    w1, w2, l1, l2 = _teams(result)

    row = [
        str(result.get("result_id") or ""),          # Match ID
        BALL_TYPE,                                    # Ball Type
        "",                                           # Draw Bracket Value
        draw.get("name") or "",                       # Draw Name
        result.get("discipline") or "",               # Draw Team Type
        tournament.get("name") or "",                 # Tournament Name
        _us_date(result.get("match_date")),           # Date
        result.get("game_scores_winner_first") or "", # Score
    ]
    row += _player_cols(w1)                            # Winner 1 (8)
    row += _player_cols(w2)                            # Winner 2 (8)
    row += _player_cols(l1)                            # Loser 1 (8)
    row += _player_cols(l2)                            # Loser 2 (8)
    row += [
        "",                                           # Outcome
        "",                                           # Draw Gender
        "",                                           # Draw Bracket Type
        "",                                           # Draw Type
        club.get("city") or "",                       # Tournament City
        "",                                           # Tournament State
        COUNTRY_CODE,                                 # Tournament Country Code
        club.get("name") or "",                       # Tournament Host
        "",                                           # Tournament Location Type
        "",                                           # Tournament Surface
        "",                                           # Tournament Event Category
        "",                                           # Tournament Event Grade
        IMPORT_SOURCE,                                # Tournament Import Source
        SANCTION_BODY,                                # Tournament Sanction Body
        "",                                           # Winner 2 College
        "",                                           # Loser 2 College
        EVENT_TYPE,                                   # Tournament Event Type
        "",                                           # Winner 1 College
        "",                                           # Loser 1 College
        _tournament_url(tournament.get("name")),      # Tournament URL
        COUNTRY,                                      # Tournament Country
        _us_date(tournament.get("start_date")),       # Tournament Start Date
        _us_date(tournament.get("end_date")),         # Tournament End Date
        key,                                          # Key (the queried key)
    ]
    return row


def _resolve_keys(run_obj, scraper, log):
    """Return the ordered list of keys to actually process this run.

    ``run_all`` walks the ENTIRE queue and processes every key that isn't
    already done — in a single run, with no per-run key cap. Otherwise the
    pasted keys are upserted into the queue and processed. In both modes a key
    that's already marked ``done`` is skipped (and logged) so it isn't
    re-scraped.
    """
    params = run_obj.params or {}

    if params.get("run_all"):
        rows = list(
            SAKey.objects.filter(scraper=scraper)
            .order_by("tournament_key")
            .values_list("tournament_key", "status")
        )
        todo = [k for k, st in rows if st != SAKey.Status.DONE]
        done = len(rows) - len(todo)
        if done:
            log(
                "INFO",
                f"\u23ed\ufe0f {done} key(s) already processed \u2014 skipping "
                f"(no need to run them again).",
            )
        log(
            "INFO",
            f"\U0001f5c2\ufe0f Running the entire queue in one run: "
            f"{len(todo)} key(s) to process.",
        )
        return todo

    # Explicit paste: upsert each key, skip any that are already done.
    pasted = list(dict.fromkeys(params.get("keys") or []))[:KEY_BATCH_MAX_KEYS]
    todo = []
    skipped = 0
    for k in pasted:
        obj, _ = SAKey.objects.get_or_create(
            tournament_key=k, defaults={"scraper": scraper}
        )
        if obj.status == SAKey.Status.DONE:
            skipped += 1
            log(
                "INFO",
                f"\u23ed\ufe0f {k} already processed "
                f"({obj.num_results or 0} result(s)) \u2014 skipping.",
            )
            continue
        todo.append(k)
    log(
        "INFO",
        f"\U0001f4cb Pasted keys: {len(todo)} to process"
        + (f", {skipped} already done (skipped)." if skipped else "."),
    )
    return todo


def run(run_obj, log):
    """Execute the Tennis South Africa scrape. Returns the standard 5-tuple."""
    tele = Telemetry()
    scraper = run_obj.scraper
    log("INFO", "\U0001f3be Tennis South Africa (SportyHQ) starting")
    proxies = build_proxies(scraper, log)

    keys = _resolve_keys(run_obj, scraper, log)
    total = len(keys)
    Run.objects.filter(pk=run_obj.pk).update(progress_total=total, progress_done=0)
    if not keys:
        # Nothing left to do — every requested key is already processed (or the
        # queue is empty). A no-op, not a failure.
        log(
            "INFO",
            "\u2705 Nothing to run \u2014 all requested keys are already "
            "processed. No re-scrape needed.",
        )
        return "", tele.requests_csv(), tele.errors_csv(), 0, Run.Status.SUCCESS

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)

    row_count = 0
    keys_ok = 0
    keys_failed = 0
    capped = False

    with ScraperClient(log=log, tele=tele, proxies=proxies) as client:
        for idx, key in enumerate(keys, start=1):
            if row_count >= KEY_BATCH_MAX_ROWS:
                log(
                    "WARN",
                    f"\u26a0\ufe0f Safety row ceiling ({KEY_BATCH_MAX_ROWS}) "
                    f"reached \u2014 stopping after {idx - 1} key(s).",
                )
                capped = True
                break

            url = f"{API_BASE}?X-API-KEY={API_KEY}&tournament_key={key}"
            data = client.get_json(url)
            now = timezone.now()

            if not isinstance(data, dict) or data.get("status") != "success":
                keys_failed += 1
                tele.record_error(f"Key {key}: API did not return a success payload")
                log("WARN", f"   \u2717 {key} \u2014 no usable response")
                SAKey.objects.filter(tournament_key=key).update(
                    scraper=scraper,
                    status=SAKey.Status.FAILED,
                    last_run=run_obj,
                    scraped_at=now,
                )
                Run.objects.filter(pk=run_obj.pk).update(
                    progress_done=F("progress_done") + 1
                )
                continue

            results = data.get("data") or []
            written = 0
            for result in results:
                try:
                    writer.writerow(
                        [sanitize_cell(c) for c in _row_for(result, key)]
                    )
                    written += 1
                except Exception as exc:  # noqa: BLE001 - a bad row can't kill the run
                    tele.record_error(
                        redact_secrets(f"Key {key}: row failed: {exc}"), exc=exc
                    )
            row_count += written
            keys_ok += 1
            SAKey.objects.filter(tournament_key=key).update(
                scraper=scraper,
                status=SAKey.Status.DONE,
                num_results=written,
                last_run=run_obj,
                scraped_at=now,
            )
            log(
                "INFO",
                f"   \U0001f3c6 {key} \u2014 {written} match(es) "
                f"[{idx}/{total}]",
            )
            Run.objects.filter(pk=run_obj.pk).update(
                progress_done=F("progress_done") + 1
            )

    log("INFO", "\u2500\u2500\u2500\u2500 summary \u2500\u2500\u2500\u2500")
    log(
        "INFO",
        f"\U0001f4be {row_count} row(s) from {keys_ok} key(s) "
        f"({keys_failed} failed)",
    )
    log(
        "INFO",
        f"\U0001f4ca Telemetry: {tele.request_count} request(s), "
        f"{tele.error_count} error(s)",
    )

    if keys_ok == 0:
        status = Run.Status.FAILED
    elif keys_failed or capped:
        status = Run.Status.PARTIAL
    else:
        status = Run.Status.SUCCESS
    icon = "\U0001f3c1" if status == Run.Status.SUCCESS else "\U0001f6a9"
    log("INFO", f"{icon} Run finished \u2014 status={status}, rows={row_count}")
    items_csv = buf.getvalue() if row_count else ""
    return items_csv, tele.requests_csv(), tele.errors_csv(), row_count, status
