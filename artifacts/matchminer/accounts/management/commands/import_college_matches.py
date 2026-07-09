"""Bulk-import historical College Dual Match CSV exports into the match database.

Reads one or more CSV files (the user's canonical 65-column export format) and
upserts every row into :class:`accounts.models.CollegeMatch` via
:func:`accounts.college_store.ingest` — so importing the same file twice, or a
file that overlaps a live scrape, only ever inserts genuinely new matches
(deduped by the normalized identity hash). Imported rows are tagged
``source="import"`` so the Lab "Match database" tab can tell them apart from
scraped rows.

By default it scans the project's ``imports/college_dual_match/`` drop folder for
``*.csv`` (drop your historical exports there and run this). Point it elsewhere
with positional path args (files or directories)::

    python manage.py import_college_matches
    python manage.py import_college_matches /path/to/export.csv
    python manage.py import_college_matches /path/to/a/folder

The CSV header is matched to the canonical columns case-insensitively and with
spaces/dashes normalised to underscores, so both ``winner_1_name`` and
``Winner 1 Name`` headers work. Unknown columns are ignored; missing columns are
left blank — nothing is fabricated.
"""

import csv
import glob
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts import college_store

# Default drop folder (relative to the Django project root).
DEFAULT_DIR = os.path.join(settings.BASE_DIR, "imports", "college_dual_match")


def _canon_key(header):
    """Normalise a CSV header cell to a canonical column key.

    Lower-cases, trims, and turns spaces/dashes into underscores so headers like
    ``"Winner 1 Name"`` map onto the canonical ``winner_1_name``.
    """
    return "_".join(str(header or "").strip().lower().replace("-", " ").split())


# Canonical column lookup keyed by its own normalised form (identity for the
# already-snake_case canonical names, but lets Title-Case headers match too).
_CANON_LOOKUP = {_canon_key(col): col for col in college_store.COLUMNS}


class Command(BaseCommand):
    help = "Bulk-import historical College Dual Match CSV exports (deduped)."

    def add_arguments(self, parser):
        parser.add_argument(
            "paths",
            nargs="*",
            help=(
                "CSV files or directories to import. Defaults to the "
                "imports/college_dual_match/ drop folder."
            ),
        )

    def _csv_files(self, paths):
        """Expand the given paths (or the default folder) to a list of CSVs."""
        targets = paths or [DEFAULT_DIR]
        files = []
        for target in targets:
            if os.path.isdir(target):
                files.extend(sorted(glob.glob(os.path.join(target, "*.csv"))))
            elif os.path.isfile(target):
                files.append(target)
            else:
                raise CommandError(f"No such file or directory: {target}")
        return files

    def _rows_from(self, path):
        """Yield canonical-keyed dicts from one CSV file."""
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return
            colmap = {
                name: _CANON_LOOKUP[_canon_key(name)]
                for name in reader.fieldnames
                if _canon_key(name) in _CANON_LOOKUP
            }
            for raw in reader:
                yield {col: raw.get(name, "") for name, col in colmap.items()}

    def handle(self, *args, **opts):
        files = self._csv_files(opts["paths"])
        if not files:
            self.stdout.write(
                self.style.WARNING(
                    f"No CSV files found. Drop exports into {DEFAULT_DIR} "
                    "or pass a path."
                )
            )
            return

        grand_new = 0
        grand_skipped = 0
        for path in files:
            rows = list(self._rows_from(path))
            if not rows:
                self.stdout.write(self.style.WARNING(f"  {path}: no data rows"))
                continue
            new_rows, skipped = college_store.ingest(
                rows, source=college_store.SOURCE_IMPORT
            )
            grand_new += len(new_rows)
            grand_skipped += skipped
            self.stdout.write(
                f"  {os.path.basename(path)}: "
                f"{len(new_rows)} new, {skipped} duplicate(s) skipped "
                f"({len(rows)} rows read)"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. Imported {grand_new} new match(es); "
                f"skipped {grand_skipped} duplicate(s) across {len(files)} file(s)."
            )
        )
