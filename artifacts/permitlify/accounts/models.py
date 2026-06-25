"""Database models for the MatchMiner scraper lab.

A `Scraper` is one mining script (mirrors a real spider like
`billiejeankingcup`). A `Run` is one execution of that scraper for a date
window / tournament, capturing the full log and the CSVs it produced (items
data, plus per-run ``requests`` and ``errors`` telemetry) so all can be
re-opened and downloaded later. Runs perform live network scrapes via
:mod:`accounts.live_scrapers`.
"""

import re
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

# Mask the password in an optionally-schemed "user:pass@host" address.
_CREDS_RE = re.compile(r"(^(?:[a-zA-Z][a-zA-Z0-9+.\-]*://)?[^/:@\s]+:)[^/@\s]+(@)")


class Proxy(models.Model):
    """A proxy pool the workspace can route scraper traffic through.

    Pools are managed on the Proxies page and selected per scraper from its
    Settings tab. ``address`` is optional: a pool with no address is a label
    only, and scrapers fall back to a direct connection until one is set.
    """

    class Kind(models.TextChoices):
        RESIDENTIAL = "residential", "Residential"
        DATACENTER = "datacenter", "Datacenter"
        MOBILE = "mobile", "Mobile"
        ISP = "isp", "ISP"

    name = models.CharField(max_length=80)
    kind = models.CharField(
        max_length=16, choices=Kind.choices, default=Kind.RESIDENTIAL
    )
    address = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "proxies"

    def __str__(self):
        return f"{self.name} ({self.get_kind_display()})"

    @property
    def display_address(self):
        """Address with any embedded password masked, safe to render in the UI."""
        addr = (self.address or "").strip()
        if not addr:
            return ""
        return _CREDS_RE.sub("\\g<1>\u2022\u2022\u2022\u2022\\g<2>", addr)


class Scraper(models.Model):
    # Concurrency bounds for the scrape worker pool (Scraper.threads). The value
    # is editable from the Lab's Settings tab and clamped to this range before a
    # run launches, so it can never spawn an unbounded pool.
    THREADS_MIN = 1
    THREADS_MAX = 16
    THREADS_DEFAULT = 5

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
    proxy = models.ForeignKey(
        "Proxy",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scrapers",
    )
    threads = models.PositiveSmallIntegerField(default=THREADS_DEFAULT)
    # API key(s) for AI-backed scrapers (e.g. college_dual_match Claude extraction).
    # Comma-separate to provide several keys the worker rotates across. Treat as a
    # secret: rendered in a password input with a reveal toggle, never logged. When
    # blank the runner falls back to settings.CLAUDE_KEYS (env), else fails honestly.
    claude_api_key = models.CharField(max_length=1024, blank=True, default="")
    # Secret bearer token for the public scheduled-trigger webhook (Schedule tab).
    # Auto-assigned on first save and rotatable from the UI; treat it as a password.
    trigger_token = models.CharField(max_length=128, unique=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} · {self.name}"

    @property
    def is_maintenance(self):
        return self.mode == self.Mode.MAINTENANCE

    @property
    def worker_count(self):
        """Thread count used to launch the scrape pool, clamped to a safe range."""
        value = self.threads or self.THREADS_DEFAULT
        return max(self.THREADS_MIN, min(value, self.THREADS_MAX))

    @staticmethod
    def generate_trigger_token():
        return secrets.token_urlsafe(32)

    def rotate_trigger_token(self, save=True):
        """Issue a fresh trigger token, invalidating the previous one."""
        self.trigger_token = self.generate_trigger_token()
        if save:
            self.save(update_fields=["trigger_token", "updated_at"])
        return self.trigger_token

    def save(self, *args, **kwargs):
        # Guarantee every scraper has a unique trigger token without a hard-coded
        # default (which would collide under the unique constraint).
        if not self.trigger_token:
            self.trigger_token = self.generate_trigger_token()
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "trigger_token" not in update_fields:
                kwargs["update_fields"] = list(update_fields) + ["trigger_token"]
        super().save(*args, **kwargs)


class Run(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        RUNNING = "running", "Running"
        STOPPED = "stopped", "Stopped"

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
    params = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.SUCCESS
    )
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    pid = models.PositiveIntegerField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    row_count = models.PositiveIntegerField(default=0)
    progress_done = models.PositiveIntegerField(default=0)
    progress_total = models.PositiveIntegerField(default=0)
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
    def progress_percent(self):
        if self.progress_total:
            return min(100, round(self.progress_done / self.progress_total * 100))
        return 0

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


class Ticket(models.Model):
    """A QA ticket: a bug / missing-data report the test team files for a scraper.

    ``body_html`` is rich text produced by the in-browser editor and **always**
    re-sanitised server-side before save (see :mod:`accounts.sanitize`), so it is
    safe to render with ``|safe``. Inline screenshots live in ``TicketAttachment``
    and are referenced from the HTML by URL, never embedded as base64.
    """

    class Status(models.TextChoices):
        TODO = "todo", "To Do"
        IN_PROGRESS = "in_progress", "In Progress"
        DONE = "done", "Done"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    scraper = models.ForeignKey(
        Scraper, related_name="tickets", on_delete=models.CASCADE
    )
    title = models.CharField(max_length=200)
    body_html = models.TextField(blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.TODO
    )
    priority = models.CharField(
        max_length=8, choices=Priority.choices, default=Priority.MEDIUM
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tickets_created",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tickets_assigned",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["scraper", "-created_at"]),
            models.Index(fields=["uuid"]),
        ]

    def __str__(self):
        return f"#{self.short_id} · {self.title}"

    @property
    def short_id(self):
        return self.uuid.hex[:8]

    @property
    def comment_count(self):
        return self.comments.count()


class TicketComment(models.Model):
    """One rich-text comment on a ticket (Jira-style thread)."""

    ticket = models.ForeignKey(
        Ticket, related_name="comments", on_delete=models.CASCADE
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_comments",
    )
    body_html = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["ticket", "created_at"])]

    def __str__(self):
        return f"comment on {self.ticket.short_id}"


class TicketAttachment(models.Model):
    """An uploaded image (screenshot) referenced inline from ticket/comment HTML.

    Bytes live in the DB (the project has no media/object storage) and are served
    by an auth-gated view. Only real raster images pass upload validation — never
    SVG (it can carry script).
    """

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    ticket = models.ForeignKey(
        Ticket,
        null=True,
        blank=True,
        related_name="attachments",
        on_delete=models.SET_NULL,
    )
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ticket_attachments",
    )
    content_type = models.CharField(max_length=64)
    data = models.BinaryField()
    byte_size = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["uuid"])]

    def __str__(self):
        return f"attachment {self.uuid.hex[:8]} ({self.content_type})"


class Notification(models.Model):
    """An in-app notification shown in the topbar bell.

    One row per recipient (fan-out): when a QA member files a ticket or comments,
    every other active user gets their own row with an independent read state.
    """

    class Kind(models.TextChoices):
        TICKET_CREATED = "ticket_created", "New ticket"
        COMMENT_ADDED = "comment_added", "New comment"
        STATUS_CHANGED = "status_changed", "Status changed"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="notifications",
        on_delete=models.CASCADE,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    ticket = models.ForeignKey(
        Ticket, null=True, blank=True, related_name="notifications", on_delete=models.CASCADE
    )
    kind = models.CharField(
        max_length=20, choices=Kind.choices, default=Kind.TICKET_CREATED
    )
    text = models.CharField(max_length=255)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "is_read", "-created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} → {self.recipient_id}"
