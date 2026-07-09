"""Execute a Run in the background, streaming its log to the DB.

Launched as a detached subprocess (``python3 manage.py run_scrape <uuid>``) by
the real-time test view. Every log line is persisted as a ``RunLogLine`` so the
live console can poll for new output and survive page reloads / concurrent
viewers. When the run finishes the full log is materialised onto
``Run.log_text`` and the three CSVs (items data, requests telemetry, errors
telemetry) onto ``Run.csv_data`` / ``Run.requests_csv`` / ``Run.errors_csv``.

There is no simulated/demo data: a scraper either has a real implementation in
:mod:`accounts.live_scrapers` (and is dispatched below) or the run fails
honestly with a clear message — it never fabricates rows.
"""

import os
import threading
import time
import traceback

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.live_scrapers import _http
from accounts.live_scrapers.registry import spec_for
from accounts.live_scrapers.telemetry import Telemetry, redact_secrets
from accounts.models import Run, RunLogLine


class _RunLogger:
    """Callable ``log(level, message)`` that persists each line and buffers it.

    Thread-safe: the scraper fetches ties concurrently, so several worker
    threads may log at once. The lock keeps ``seq`` monotonic and unique (the
    live console polls by ``seq``) and serialises the per-line DB insert.
    """

    def __init__(self, run):
        self.run = run
        self.seq = 0
        self.buffer = []
        self._lock = threading.Lock()

    def __call__(self, level, message):
        with self._lock:
            self.seq += 1
            seq = self.seq
            stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")
            text = f"[{stamp}] {level:<5} {message}"
            RunLogLine.objects.create(
                run=self.run, seq=seq, level=level, text=text
            )
            self.buffer.append(text)
            try:
                print(text, flush=True)
            except (UnicodeEncodeError, OSError, ValueError):
                # The DB row above is the real record. On Windows the worker's
                # stdout is the legacy-encoded ``nul`` device, which can't encode
                # emoji — a failed console echo must never kill the run.
                pass

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
        run.pid = os.getpid()
        run.save(update_fields=["started_at", "status", "pid"])

        log = _RunLogger(run)
        t0 = time.time()
        try:
            scraper = run.scraper
            _http.set_default_tries(scraper.effective_tries)
            log(
                "INFO",
                f"\U0001f680 Worker online \u2014 Run #{run.short_id} "
                f"\u00b7 {scraper.code}",
            )
            log(
                "INFO",
                f"\U0001f501 Retry budget: {scraper.effective_tries} "
                f"tries per request",
            )
            runner = spec_for(scraper.slug).load_runner()
            if runner is None:
                tele = Telemetry()
                msg = (
                    f"No live scraper is wired for '{scraper.slug}' in this "
                    f"environment yet."
                )
                log("ERROR", "\u274c " + msg)
                tele.record_error(msg)
                run.status = Run.Status.FAILED
                run.row_count = 0
                run.csv_data = ""
                run.requests_csv = ""
                run.errors_csv = tele.errors_csv()
                run.output_size_bytes = 0
            else:
                items_csv, requests_csv, errors_csv, row_count, status = runner(
                    run, log
                )
                run.status = status
                run.row_count = row_count
                run.csv_data = items_csv
                run.requests_csv = requests_csv
                run.errors_csv = errors_csv
                run.output_size_bytes = len(items_csv.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surface any failure in the run log
            tb = redact_secrets(traceback.format_exc())
            summary = redact_secrets(f"{exc.__class__.__name__}: {exc}")
            log("ERROR", f"\u274c Run crashed \u2014 {summary}")
            for line in tb.splitlines():
                log("ERROR", line)
            tele = Telemetry()
            tele.record_error(f"Run crashed: {exc}", exc=exc)
            run.status = Run.Status.FAILED
            run.row_count = 0
            run.csv_data = ""
            run.requests_csv = ""
            run.errors_csv = tele.errors_csv()
            run.output_size_bytes = 0

        run.duration_ms = int((time.time() - t0) * 1000)
        run.finished_at = timezone.now()
        run.log_text = log.full_text()
        # Explicit fields so a late finish can never clobber pid / started_at — and,
        # critically, can't silently undo a STOPPED/FAILED status set elsewhere when
        # this worker survived (it normally won't: the Stop path SIGKILLs it first).
        run.save(
            update_fields=[
                "status",
                "row_count",
                "csv_data",
                "requests_csv",
                "errors_csv",
                "output_size_bytes",
                "duration_ms",
                "finished_at",
                "log_text",
            ]
        )
