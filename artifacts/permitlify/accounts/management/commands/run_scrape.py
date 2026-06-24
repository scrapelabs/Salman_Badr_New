"""Execute a Run in the background, streaming its log to the DB.

Launched as a detached subprocess (``python3 manage.py run_scrape <uuid>``) by
the real-time test view. Every log line is persisted as a ``RunLogLine`` so the
live console can poll for new output and survive page reloads / concurrent
viewers. When the run finishes the full log is also materialised onto
``Run.log_text`` and the CSV onto ``Run.csv_data``.
"""

import time
import traceback

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts import runs as sim
from accounts.live_scrapers import billiejeankingcup
from accounts.models import Run, RunLogLine


class _RunLogger:
    """Callable ``log(level, message)`` that persists each line and buffers it."""

    def __init__(self, run):
        self.run = run
        self.seq = 0
        self.buffer = []

    def __call__(self, level, message):
        self.seq += 1
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
        text = f"[{stamp}] {level:<5} {message}"
        RunLogLine.objects.create(
            run=self.run, seq=self.seq, level=level, text=text
        )
        self.buffer.append(text)
        print(text, flush=True)

    def full_text(self):
        return "\n".join(self.buffer) + ("\n" if self.buffer else "")


class Command(BaseCommand):
    help = "Run a scraper Run by UUID, streaming its log to the database."

    def add_arguments(self, parser):
        parser.add_argument("run_uuid")

    def handle(self, *args, **options):
        try:
            run = Run.objects.select_related("scraper").get(
                uuid=options["run_uuid"]
            )
        except Run.DoesNotExist:
            return

        run.started_at = timezone.now()
        run.status = Run.Status.RUNNING
        run.save(update_fields=["started_at", "status"])

        log = _RunLogger(run)
        t0 = time.time()
        try:
            scraper = run.scraper
            if scraper.slug == "billiejeankingcup":
                csv_text, row_count, status = billiejeankingcup.run(run, log)
            else:
                csv_text, row_count, status = sim.simulated_run(scraper, run, log)

            run.status = status
            run.row_count = row_count
            run.csv_data = csv_text
            run.output_size_bytes = len(csv_text.encode("utf-8"))
        except Exception:  # noqa: BLE001 - surface any failure in the run log
            for line in traceback.format_exc().splitlines():
                log("ERROR", line)
            run.status = Run.Status.FAILED
            run.row_count = 0
            run.csv_data = ""
            run.output_size_bytes = 0

        run.duration_ms = int((time.time() - t0) * 1000)
        run.finished_at = timezone.now()
        run.log_text = log.full_text()
        run.save()
