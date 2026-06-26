"""Canonical store for College Dual Match results.

The ``college_dual_match`` scraper (and the historical-CSV importer) both persist
their rows here, deduped by a *normalized identity* digest so that re-running a
scrape — or importing the same export twice — only ever inserts genuinely new
matches.

The canonical record is the **65-column** schema the user's production exports
use (see :data:`COLUMNS`). Everything funnels through this one module so the DB
model, the live scraper's run output, the "Match database" Lab tab, and the bulk
importer all agree on the column set, the dedup key, and the CSV format.

Why a *normalized* identity hash (and not a full-row hash): the same real match
is reported by both schools' athletics sites, with different ``tournament_url``s
and different date spellings (``05/24/2026`` vs ``5/24/2026``). A naive full-row
hash would treat those as two matches. The identity hash below normalizes the
date, lower-cases names/teams, sorts each doubles pair, and strips score
formatting, then digests only the *identifying* fields (date, gender, draw,
players, score, teams) — so cross-source duplicates collapse to one row while
genuinely different matches stay distinct. Volatile/metadata fields
(``tournament_url``, third-party ids, cities, DOBs, …) are deliberately excluded.
"""

import csv
import hashlib
import io
from datetime import datetime

from django.db import transaction

# Source tags (kept in sync with CollegeMatch.SOURCE_* on the model).
SOURCE_SCRAPE = "scrape"
SOURCE_IMPORT = "import"

# --- canonical 65-column schema (exact order of the user's export) ----------
COLUMNS = [
    "match_id", "ball_type", "draw_bracket_value", "draw_name", "draw_team_type",
    "tournament_name", "date", "score",
    "winner_1_name", "winner_1_gender", "winner_1_third_party_id", "winner_1_city",
    "winner_1_country", "winner_1_state",
    "winner_2_name", "winner_2_gender", "winner_2_third_party_id", "winner_2_city",
    "winner_2_state",
    "loser_1_name", "loser_1_gender", "loser_1_third_party_id", "loser_1_city",
    "loser_1_state", "loser_1_country",
    "loser_2_name", "loser_2_gender", "loser_2_third_party_id", "loser_2_city",
    "loser_2_state",
    "outcome", "id_type", "draw_gender", "draw_bracket_type", "draw_type",
    "tournament_city", "tournament_state", "tournament_country_code",
    "tournament_host", "tournament_location_type", "tournament_surface",
    "tournament_event_category", "tournament_event_grade", "tournament_import_source",
    "tournament_sanction_body",
    "winner_2_country", "winner_2_college", "loser_2_country", "loser_2_college",
    "tournament_event_type", "winner_1_college", "loser_1_college", "tournament_url",
    "winner_1_dob", "winner_2_dob", "loser_1_dob", "loser_2_dob",
    "tournament_country", "tournament_start_date", "tournament_end_date",
    "winner_team", "loser_team", "team_score", "winner_team_type", "loser_team_type",
]

# Date spellings seen across sources, tried in order. ``strptime`` is lenient on
# leading zeros so a single ``%m/%d/%Y`` covers both "05/24/2026" and "5/24/2026".
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%y")


def _collapse(value):
    """Trim and collapse internal whitespace to single spaces."""
    return " ".join(str(value or "").split())


def _norm_text(value):
    """Lower-cased, whitespace-collapsed text for identity comparison."""
    return _collapse(value).lower()


def _norm_date(value):
    """Best-effort parse of a date string to ISO ``YYYY-MM-DD``.

    Falls back to the lower-cased, collapsed raw string when no known format
    matches (so unparseable-but-identical spellings still hash equally).
    """
    raw = _collapse(value)
    if not raw:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw.lower()


def _norm_score(value):
    """Score with all whitespace removed and trailing separators stripped."""
    return "".join(str(value or "").split()).rstrip(";").lower()


def _norm_gender(value):
    """Collapse gender spellings to a single leading letter (f/m/...)."""
    text = _norm_text(value)
    return text[:1] if text else ""


def match_hash(row):
    """A stable sha256 identity digest for a (full or partial) canonical row.

    Built only from *identifying* fields, each normalized; doubles pairs are
    sorted so partner order never matters. See the module docstring for why
    volatile fields (URL/ids/cities/DOBs) are excluded.
    """
    g = row.get
    winners = sorted(
        n for n in (_norm_text(g("winner_1_name")), _norm_text(g("winner_2_name"))) if n
    )
    losers = sorted(
        n for n in (_norm_text(g("loser_1_name")), _norm_text(g("loser_2_name"))) if n
    )
    parts = [
        _norm_date(g("date", "")),
        _norm_gender(g("draw_gender", "")),
        _norm_text(g("draw_name", "")),
        _norm_text(g("draw_team_type", "")),
        "|".join(winners),
        "|".join(losers),
        _norm_score(g("score", "")),
        _norm_text(g("winner_team", "")),
        _norm_text(g("loser_team", "")),
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8"))
    return digest.hexdigest()


def build_full_row(data):
    """Return a dict with every canonical column present (missing -> "").

    Values are stringified and flattened to a single line so the row round-trips
    cleanly through CSV.
    """
    out = {}
    for col in COLUMNS:
        value = data.get(col, "")
        if value is None:
            value = ""
        out[col] = str(value).replace("\r", " ").replace("\n", " ").strip()
    return out


def map_extracted(extracted):
    """Map the live scraper's extracted match dict onto the canonical schema.

    The scraper emits a compact key set (``tournament_date``, ``winner_1_name``,
    …); this widens it to the 65-column canonical row, carrying the few domain
    constants the source always sets for college dual matches (yellow ball,
    College team types, "Dual Match" event type) and copying the match date into
    the start/end date columns. Fields the scraper doesn't produce stay blank —
    nothing is fabricated.
    """
    g = lambda key: (extracted.get(key) or "")  # noqa: E731
    match_date = g("tournament_date")
    return {
        "date": match_date,
        "tournament_start_date": match_date,
        "tournament_end_date": match_date,
        "tournament_name": g("tournament_name"),
        "draw_name": g("draw_name"),
        "draw_gender": g("draw_gender") or g("tournament_gender"),
        "draw_team_type": g("draw_team_type"),
        "draw_type": g("draw_team_type"),
        "winner_1_name": g("winner_1_name"),
        "winner_1_gender": g("winner_1_gender"),
        "winner_1_college": g("winner_1_college"),
        "winner_2_name": g("winner_2_name"),
        "winner_2_gender": g("winner_2_gender"),
        "winner_2_college": g("winner_2_college"),
        "loser_1_name": g("loser_1_name"),
        "loser_1_gender": g("loser_1_gender"),
        "loser_1_college": g("loser_1_college"),
        "loser_2_name": g("loser_2_name"),
        "loser_2_gender": g("loser_2_gender"),
        "loser_2_college": g("loser_2_college"),
        "score": g("score"),
        "winner_team": g("winner_team"),
        "loser_team": g("loser_team"),
        "team_score": g("team_score"),
        "outcome": g("outcome"),
        "ball_type": "Yellow",
        "tournament_event_type": "Dual Match",
        "winner_team_type": "College",
        "loser_team_type": "College",
    }


def ingest(rows, *, run=None, source=SOURCE_SCRAPE):
    """Persist ``rows`` (canonical dicts), inserting only never-before-seen ones.

    Returns ``(new_rows, skipped)`` where ``new_rows`` is the list of full
    canonical dicts that were inserted this call (in input order) and ``skipped``
    is how many input rows were duplicates (in-batch or already stored).

    Uses ``bulk_create(ignore_conflicts=True)`` under the unique ``match_hash``
    constraint, so concurrent inserts of the same match are the DB's problem to
    resolve, not a crash.
    """
    from accounts.models import CollegeMatch

    full = []
    seen = set()
    for raw in rows:
        row = build_full_row(raw)
        digest = match_hash(row)
        if digest in seen:
            continue
        seen.add(digest)
        full.append((digest, row))

    total_in = len(rows)
    if not full:
        return [], total_in

    hashes = [h for h, _ in full]
    existing = set()
    for i in range(0, len(hashes), 5000):
        existing.update(
            CollegeMatch.objects.filter(
                match_hash__in=hashes[i : i + 5000]
            ).values_list("match_hash", flat=True)
        )

    new_pairs = [(h, row) for h, row in full if h not in existing]
    if not new_pairs:
        return [], total_in

    # ``new_pairs`` is what we believe is new after subtracting the rows that
    # already existed at SELECT time. The unique ``match_hash`` constraint keeps
    # the DB correct no matter what, so ``ignore_conflicts=True`` can never create
    # a duplicate. The only theoretical gap is attribution: if a *concurrent*
    # ingest inserted one of these hashes between our SELECT and our INSERT, the
    # DB drops it here but we'd still count it as "new". That race can't happen in
    # this app — scrapes are single-in-flight per scraper (the
    # ``uniq_running_run_per_scraper`` constraint) and imports are a manual,
    # single-process CLI step — so the reported ``new_rows`` is exact in practice.
    objs = [
        CollegeMatch(
            match_hash=h,
            data=row,
            date_norm=_norm_date(row.get("date", "")),
            tournament_name=row.get("tournament_name", "")[:300],
            draw_name=row.get("draw_name", "")[:120],
            draw_gender=row.get("draw_gender", "")[:20],
            winner_team=row.get("winner_team", "")[:200],
            loser_team=row.get("loser_team", "")[:200],
            source=source,
            first_seen_run=run,
        )
        for h, row in new_pairs
    ]
    with transaction.atomic():
        CollegeMatch.objects.bulk_create(objs, ignore_conflicts=True, batch_size=1000)

    new_rows = [row for _, row in new_pairs]
    return new_rows, total_in - len(new_rows)


def to_csv(rows):
    """Render canonical rows to a 65-column CSV string (with header)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMNS)
    for raw in rows:
        row = build_full_row(raw)
        writer.writerow([row[col] for col in COLUMNS])
    return buf.getvalue()
