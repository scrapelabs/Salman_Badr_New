# College Dual Match — historical CSV import drop folder

Drop your historical **College Dual Match** CSV exports in this folder, then load
them into the match database with either:

- the website is *not* used for bulk import — it's a command-line / local step;
- **Windows:** double-click `bat_files/10_import_college_matches.bat`;
- **any OS:** from `artifacts/permitlify/` run:

  ```
  python manage.py import_college_matches
  ```

  (pass a file or folder path to import from somewhere else, e.g.
  `python manage.py import_college_matches /path/to/export.csv`).

## What happens

Every row is upserted into the match database and **deduplicated** by a normalized
identity hash (date, gender, draw, players, score, teams). So:

- re-importing the same file inserts **nothing new**;
- importing a file that overlaps a live scrape only adds the rows not already
  stored;
- imported rows are tagged **`import`** so the Lab → *Match database* tab can tell
  them apart from scraped rows.

## CSV format

The expected header is the canonical **65-column** export format. Headers are
matched case-insensitively (`Winner 1 Name` and `winner_1_name` both work).
Unknown columns are ignored; missing columns are left blank — nothing is
fabricated.

## Git

CSV files in this folder are **git-ignored** (they can be large and are your data,
not source). This README and the `.gitignore` are kept so the folder always
exists.
