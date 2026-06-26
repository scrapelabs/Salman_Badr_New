"""Run a scraper synchronously from the command line and populate the DB.

This is a **local helper** for inspecting scrapers one at a time (e.g. from the
``bat_files/`` double-click helpers on Windows). It does exactly what the web
"Real-time test" button does, but in-process and blocking — no detached
subprocess, no live console polling — so you can watch the log stream straight
to your terminal and then open the resulting CSVs.

It reuses the *same* validation and dispatch path as the website:

- inputs are normalised + SSRF-guarded by ``accounts.views.validate_run_params``;
- the run is dispatched by the existing ``run_scrape`` worker via ``call_command``;

so a CLI run and a website run are byte-for-byte equivalent. Unwired slugs fail
honestly (no fabricated rows), just like everywhere else.

Examples (run from ``artifacts/permitlify`` with the venv active)::

    python manage.py scrape_now brazil_results --year 2025 --month 0
    python manage.py scrape_now croatia_league --url "https://hts.tournamentsoftware.com/...."
    python manage.py scrape_now croatia_league --date-from 2025-01-01 --date-to 2025-03-31
    python manage.py scrape_now billiejeankingcup --year 2025

Blank inputs fall back to sensible defaults (current year / all months / a
trailing date window). Pass ``--out DIR`` to also dump the run's
``data.csv`` / ``requests.csv`` / ``errors.csv`` / ``log.txt`` under
``DIR/<slug>/<run-id>/``.
"""

import os

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from accounts.live_scrapers.registry import get_spec
from accounts.models import Run, Scraper


class Command(BaseCommand):
    help = "Run a scraper synchronously and populate the database (local helper)."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="Scraper slug, e.g. brazil_results")
        parser.add_argument("--year", default="", help="Season year (e.g. 2025)")
        parser.add_argument(
            "--month", default="", help="Month 1-12, or 0 for the whole year"
        )
        parser.add_argument(
            "--date-from", dest="date_from", default="", help="Start date YYYY-MM-DD"
        )
        parser.add_argument(
            "--date-to", dest="date_to", default="", help="End date YYYY-MM-DD"
        )
        parser.add_argument(
            "--url",
            dest="tournament_url",
            default="",
            help="A single tournament URL (for URL-input scrapers)",
        )
        parser.add_argument(
            "--out",
            dest="out",
            default="",
            help="Directory to also write data.csv/requests.csv/errors.csv/log.txt",
        )

    def handle(self, *args, **opts):
        # Imported here (not at module load) so this command stays cheap to load
        # and we don't pull the view stack into unrelated management commands.
        from accounts.views import (
            RunStartError,
            _create_guarded_run,
            validate_run_params,
        )

        slug = opts["slug"]
        try:
            scraper = Scraper.objects.get(slug=slug)
        except Scraper.DoesNotExist:
            known = ", ".join(
                Scraper.objects.order_by("slug").values_list("slug", flat=True)
            )
            raise CommandError(
                f"No scraper with slug '{slug}'.\nKnown scrapers: {known or '(none)'}"
            )

        spec = get_spec(slug)
        if spec is None or not spec.runner_path:
            raise CommandError(
                f"'{slug}' has no wired runner in this build, so it can't produce "
                f"real data. (Only scrapers registered in live_scrapers/registry.py "
                f"can run.)"
            )

        data = {
            "year": opts["year"],
            "month": opts["month"],
            "date_from": opts["date_from"],
            "date_to": opts["date_to"],
            "tournament_url": opts["tournament_url"],
        }
        # webhook=True => blank inputs become sensible defaults (current year /
        # all months / trailing date window) instead of erroring.
        try:
            inputs = validate_run_params(spec, data, webhook=True)
        except RunStartError as exc:
            raise CommandError(str(exc))

        # The single guarded create path shared with the website + webhook:
        # maintenance, stale-run reaping, the single-in-flight rule, AND the
        # browser-exclusivity rule (a browser source like the itftennis family
        # needs the host to itself — it can't start while any other run is live,
        # and nothing else can start while it is). RunStartError → CommandError.
        try:
            run = _create_guarded_run(scraper, inputs=inputs, launched_by=None)
        except RunStartError as exc:
            raise CommandError(str(exc))

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\n\u25b6 {scraper.code} ({slug}) \u2014 {run.tournament}\n"
            )
        )

        # Run the existing worker in-process (blocking). It streams every log
        # line to stdout AND persists it, exactly as the website does.
        try:
            call_command("run_scrape", str(run.uuid))
        except KeyboardInterrupt:
            # Ctrl+C in the console: the in-process worker is aborted before its
            # final save, leaving the row RUNNING. Mark it stopped now so it
            # doesn't block the next run until the 20-min stale-run reaper.
            run.refresh_from_db()
            if run.status == Run.Status.RUNNING:
                run.status = Run.Status.STOPPED
                run.finished_at = timezone.now()
                run.save(update_fields=["status", "finished_at"])
            self.stdout.write(
                self.style.WARNING("\n\u23f9 Interrupted \u2014 run marked stopped.")
            )
            raise CommandError("Aborted by user.")

        run.refresh_from_db()
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("\u2500\u2500\u2500\u2500 result \u2500\u2500\u2500\u2500"))
        self.stdout.write(f"  status   : {run.status}")
        self.stdout.write(f"  rows     : {run.row_count}")
        self.stdout.write(f"  duration : {(run.duration_ms or 0) / 1000:.1f}s")
        self.stdout.write(f"  run id   : {run.short_id}  (uuid {run.uuid})")

        out = (opts.get("out") or "").strip()
        if out:
            self._write_outputs(out, slug, run)

        self.stdout.write(
            "\n  View it in the app: Scrapers \u2192 "
            f"{scraper.code} \u2192 Open lab \u2192 Calls history.\n"
        )

        if run.status == Run.Status.SUCCESS:
            self.stdout.write(self.style.SUCCESS("Done \u2713"))
        else:
            self.stdout.write(
                self.style.ERROR(
                    "Run did not succeed \u2014 read the log above / errors.csv."
                )
            )

    def _write_outputs(self, out, slug, run):
        base = os.path.join(out, slug, run.short_id)
        os.makedirs(base, exist_ok=True)
        files = {
            "data.csv": run.csv_data or "",
            "requests.csv": run.requests_csv or "",
            "errors.csv": run.errors_csv or "",
            "log.txt": run.log_text or "",
        }
        for name, content in files.items():
            with open(
                os.path.join(base, name), "w", encoding="utf-8", newline=""
            ) as fh:
                fh.write(content)
        self.stdout.write(f"  files    : {os.path.abspath(base)}")
