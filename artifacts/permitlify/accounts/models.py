"""Database models for the MatchMiner scraper lab.

A `Scraper` is one mining script (mirrors a real spider like
`billiejeankingcup`). A `Run` is one execution of that scraper for a date
window / tournament, capturing the full log and the CSVs it produced (items
data, plus per-run ``requests`` and ``errors`` telemetry) so all can be
re-opened and downloaded later. Runs perform live network scrapes via
:mod:`accounts.live_scrapers`.
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Scraper(models.Model):
    class Mode(models.TextChoices):
        PRODUCTION = "production", "Production"
        MAINTENANCE = "maintenance", "Maintenance"

    slug = models.SlugField(unique=True, max_length=64)
    code = models.CharField(max_length=8)
    name = models.CharField(max_length=120)
    tour = models.CharField(max_length=60)
    domain = models.CharField(max_length=120)
    vendor_url = models.URLField(blank=True)
    description = models.TextField(blank=True)
    returns = models.CharField(max_length=12, default="CSV")
    tournaments = models.JSONField(default=list, blank=True)
    mode = models.CharField(
        max_length=12, choices=Mode.choices, default=Mode.PRODUCTION
    )
    maintenance_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} · {self.name}"

    @property
    def is_maintenance(self):
        return self.mode == self.Mode.MAINTENANCE


class Run(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        RUNNING = "running", "Running"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    scraper = models.ForeignKey(
        Scraper, related_name="runs", on_delete=models.CASCADE
    )
    launched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="runs",
    )
    tournament = models.CharField(max_length=120, default="All tournaments")
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.SUCCESS
    )
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    row_count = models.PositiveIntegerField(default=0)
    output_size_bytes = models.PositiveIntegerField(default=0)
    log_text = models.TextField(blank=True)
    csv_data = models.TextField(blank=True)
    requests_csv = models.TextField(blank=True)
    errors_csv = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["scraper", "-started_at"]),
            models.Index(fields=["uuid"]),
            models.Index(fields=["status"]),
        ]
        constraints = [
            # At most one in-flight run per scraper, enforced atomically so two
            # concurrent POSTs can't both launch a worker.
            models.UniqueConstraint(
                fields=["scraper"],
                condition=models.Q(status="running"),
                name="uniq_running_run_per_scraper",
            ),
        ]

    def __str__(self):
        return f"Run {self.short_id} · {self.scraper.code}"

    @property
    def short_id(self):
        return self.uuid.hex[:8]

    @property
    def duration_label(self):
        ms = self.duration_ms or 0
        if ms < 1000:
            return f"{ms} ms"
        seconds = ms / 1000
        if seconds < 60:
            return f"{seconds:.1f}s"
        minutes, secs = divmod(int(round(seconds)), 60)
        return f"{minutes}m {secs:02d}s"

    @property
    def size_label(self):
        size = self.output_size_bytes or 0
        if size < 1024:
            return f"{size} B"
        kb = size / 1024
        if kb < 1024:
            return f"{kb:.1f} KB"
        return f"{kb / 1024:.1f} MB"

    @property
    def has_csv(self):
        return bool(self.csv_data)

    @property
    def has_requests(self):
        return bool(self.requests_csv)

    @property
    def has_errors(self):
        return bool(self.errors_csv)

    @property
    def request_count(self):
        # requests CSV has no embedded newlines, so a line count is exact.
        return max(0, len(self.requests_csv.splitlines()) - 1) if self.requests_csv else 0

    @property
    def is_running(self):
        return self.status == self.Status.RUNNING


class RunLogLine(models.Model):
    """One streamed log line for a Run.

    Written incrementally by the ``run_scrape`` background command so the live
    console (and any concurrent viewer) can poll for new lines. The full log is
    also materialised onto ``Run.log_text`` when the run finishes, so legacy and
    completed runs read from a single snapshot.
    """

    run = models.ForeignKey(
        Run, related_name="log_lines", on_delete=models.CASCADE
    )
    seq = models.PositiveIntegerField()
    level = models.CharField(max_length=8, default="INFO")
    text = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "seq"], name="uniq_run_logline_seq"
            ),
        ]
        indexes = [models.Index(fields=["run", "seq"])]

    def __str__(self):
        return f"{self.run.short_id} #{self.seq}"
