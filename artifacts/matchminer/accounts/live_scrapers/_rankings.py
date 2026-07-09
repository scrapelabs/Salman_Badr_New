"""Shared helpers for the player-ranking scrapers (wtatennis, atptour).

Unlike the match-results scrapers, these don't return ties/matches — they
return a **player-ranking snapshot**. Both emit the same 9-column schema, both
take a single snapshot date, and both scrape *singles and doubles in one run*
(the ``Ranktype`` column distinguishes the two tables). This module centralises
the column schema, thread-safe CSV assembly, and the date helpers so the two
runners stay byte-for-byte consistent.
"""

import csv
import io
import threading
from datetime import datetime, timedelta

from django.utils import timezone

from .telemetry import sanitize_cell

# Both ranking tables every run collects. The value is stored verbatim in the
# Ranktype column; each source upper/lower/title-cases it where the upstream
# endpoint needs a different casing.
RANK_TYPES = ("singles", "doubles")

# Items CSV columns for a ranking snapshot. Title-cased for the header exactly
# like the match-results scrapers, so downloaded files read uniformly:
# Birthdate, Gender, Player Id, Name, Nationality, Points, Rank, Rankdate, Ranktype
COLUMNS = [
    "birthdate", "gender", "player_id", "name",
    "nationality", "points", "rank", "rankdate", "ranktype",
]
HEADER = [c.replace("_", " ").title() for c in COLUMNS]


def snapshot_date(run_obj):
    """Resolve the snapshot date (a ``date``) for a run.

    Prefers the explicit ``single_date`` the start form / webhook records;
    falls back to ``date_from`` and finally today, so a run always has a date.
    """
    params = run_obj.params or {}
    raw = (params.get("single_date") or "").strip()
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return run_obj.date_from or timezone.localdate()


def snapshot_dates(run_obj):
    """Resolve the list of ranking dates a run should collect.

    ATP/WTA publish a fresh ranking every **Monday**, so a date-range run expands
    to one snapshot per Monday inside ``[date_from, date_to]`` (inclusive) — e.g.
    6/08 → 6/22 yields ``[6/08, 6/15, 6/22]``. A single-snapshot run (the
    ``single_date`` param the rank-snapshot form / webhook records) yields exactly
    that one date, and a run with no usable range falls back to one date so it
    never collects nothing by accident.
    """
    params = run_obj.params or {}
    raw = (params.get("single_date") or "").strip()
    if raw:
        try:
            return [datetime.strptime(raw, "%Y-%m-%d").date()]
        except ValueError:
            pass
    start = run_obj.date_from
    end = run_obj.date_to
    if start and end and end >= start:
        # Advance to the first Monday on/after the start, then step weekly.
        first_monday = start + timedelta(days=(0 - start.weekday()) % 7)
        out = []
        cur = first_monday
        while cur <= end:
            out.append(cur)
            cur += timedelta(days=7)
        # A sub-week range that straddles no Monday still gets one date so the
        # run reflects the user's window rather than silently collecting nothing.
        return out or [start]
    return [snapshot_date(run_obj)]


def resolve_rank_types(run_obj):
    """The ranking tables to collect for a run.

    Honours an optional ``rank_type`` param (``singles`` / ``doubles`` / ``both``)
    recorded by the start form / webhook; anything else (blank / ``both`` /
    unknown) collects both tables, preserving the historical default.
    """
    params = run_obj.params or {}
    rt = (params.get("rank_type") or "").strip().lower()
    if rt == "singles":
        return ("singles",)
    if rt == "doubles":
        return ("doubles",)
    return RANK_TYPES


def to_mdy(raw, in_format):
    """Reformat ``raw`` from ``in_format`` to ``m/d/Y`` (zero-padded) or ''.

    Mirrors the production ``convert_string_to_date_format`` helper: a value the
    parser can't read becomes an empty cell rather than aborting the row.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, in_format).strftime("%m/%d/%Y")
    except (TypeError, ValueError):
        return ""


class RankingsCsv:
    """Thread-safe accumulator that writes ranking rows to the items CSV.

    The atptour runner fans player lookups out across worker threads, so the
    writer is guarded by a lock; the (single-threaded) wtatennis runner pays a
    negligible cost for the same guard.
    """

    def __init__(self):
        self._buf = io.StringIO()
        self._writer = csv.writer(self._buf, lineterminator="\n")
        self._writer.writerow(HEADER)
        self._lock = threading.Lock()
        self.row_count = 0

    def add(self, row):
        """Append one row dict (keyed by :data:`COLUMNS`) to the CSV."""
        with self._lock:
            self._writer.writerow([sanitize_cell(row.get(c, "")) for c in COLUMNS])
            self.row_count += 1

    def value(self):
        """Return the CSV text, or ``""`` when the run produced no rows."""
        return self._buf.getvalue() if self.row_count else ""
