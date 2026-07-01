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
from datetime import time as dtime

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

    @property
    def display_host(self):
        """Just the host of the address — no scheme, credentials, or port.

        For compact UI labels (e.g. the per-scraper proxy dropdown) where the
        masked credentials would only add noise. Never exposes the password.
        """
        addr = (self.address or "").strip()
        if not addr:
            return ""
        if "://" in addr:
            addr = addr.split("://", 1)[1]
        if "@" in addr:
            addr = addr.rsplit("@", 1)[1]
        return addr.split("/", 1)[0].split(":", 1)[0]


class Scraper(models.Model):
    # Concurrency bounds for the scrape worker pool (Scraper.threads). The value
    # is editable from the Lab's Settings tab and clamped to this range before a
    # run launches, so it can never spawn an unbounded pool.
    THREADS_MIN = 1
    THREADS_MAX = 16
    THREADS_DEFAULT = 5

    # Per-request retry budget (Scraper.max_tries): total attempts the HTTP
    # client makes per request before giving up (the initial try plus retries),
    # editable from the Lab's Settings tab and clamped to this range before a run
    # launches. The worker applies it as the client-wide default for the run.
    TRIES_MIN = 1
    TRIES_MAX = 10
    TRIES_DEFAULT = 4

    class Mode(models.TextChoices):
        PRODUCTION = "production", "Production"
        MAINTENANCE = "maintenance", "Maintenance"

    slug = models.SlugField(unique=True, max_length=64)
    code = models.CharField(max_length=16)
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
    max_tries = models.PositiveSmallIntegerField(default=TRIES_DEFAULT)
    # API key(s) for AI-backed scrapers (e.g. college_dual_match Claude extraction).
    # Comma-separate to provide several keys the worker rotates across. Treat as a
    # secret: rendered in a password input with a reveal toggle, never logged. When
    # blank the runner falls back to the workspace GeneralConfig key (Settings page),
    # then to settings.CLAUDE_KEYS (env), else fails honestly.
    claude_api_key = models.CharField(max_length=1024, blank=True, default="")
    # Login credentials for scrapers that authenticate to an upstream portal
    # (e.g. usta_team_captains -> USTA TennisLink). Stored per scraper; treated as
    # secrets (password rendered masked with a reveal toggle, never logged). When
    # blank the runner falls back to the server's env credentials, else fails honestly.
    login_username = models.CharField(max_length=255, blank=True, default="")
    login_password = models.CharField(max_length=1024, blank=True, default="")
    # Single generic secret config value for scrapers that need one credential-bearing
    # string (e.g. australia_tennis -> Azure Blob SAS URL). Stored per scraper; treated
    # as a secret (rendered masked with a reveal toggle, never logged). When blank the
    # runner falls back to the server's env var, else fails honestly.
    secret_value = models.CharField(max_length=2048, blank=True, default="")
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

    @property
    def effective_tries(self):
        """Per-request try budget for the scrape client, clamped to a safe range."""
        value = self.max_tries or self.TRIES_DEFAULT
        return max(self.TRIES_MIN, min(value, self.TRIES_MAX))

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


class ScraperModelFile(models.Model):
    """A large binary model asset uploaded for a scraper via the Settings tab.

    Some scrapers need a sizeable ML model to run (e.g. ``belgium_results`` uses a
    ~43 MB Keras CNN to solve the Zenedge captcha). Rather than committing that
    binary to the repo, an admin uploads it here; the bytes live in Postgres, so
    they survive redeploys and are visible to the run worker (which shares the same
    ``DATABASE_URL``). One file per scraper.

    The ``data`` blob is deliberately on its own table (one-to-one) so ordinary
    ``Scraper`` queries never drag the megabytes along — fetch it explicitly, and
    ``defer("data")`` when you only need the metadata.
    """

    scraper = models.OneToOneField(
        Scraper, on_delete=models.CASCADE, related_name="model_file"
    )
    filename = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=80, blank=True, default="")
    size = models.PositiveIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    data = models.BinaryField()
    uploaded_at = models.DateTimeField(auto_now=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "scraper model file"

    def __str__(self):
        return f"{self.scraper.slug} · {self.filename or 'model'} ({self.size} bytes)"


class Run(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        RUNNING = "running", "Running"
        STOPPED = "stopped", "Stopped"
        QUEUED = "queued", "Queued"

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

    def eta_seconds(self, now=None):
        """Estimated seconds remaining for a *running* run, or ``None`` when it
        cannot yet be estimated (not running, total unknown, or nothing done).

        Uses the cumulative average completion rate ``progress_done / elapsed``
        rather than an instantaneous rate: scrapers process their work units
        (ties / tournaments / matches) concurrently in a thread pool, so the
        running average is far steadier than a momentary one. ``elapsed`` is
        measured from ``started_at`` (it therefore folds in the short discovery
        phase, which makes the estimate slightly conservative — the safe
        direction for "when should I check back?").
        """
        if self.status != self.Status.RUNNING:
            return None
        if not (self.progress_total and self.progress_done):
            return None
        started = self.started_at
        if started is None:
            return None
        now = now or timezone.now()
        elapsed = (now - started).total_seconds()
        if elapsed <= 0:
            return None
        remaining = self.progress_total - self.progress_done
        if remaining <= 0:
            return 0.0
        rate = self.progress_done / elapsed  # work units per second
        if rate <= 0:
            return None
        return remaining / rate

    @property
    def eta_label(self):
        """Human ``~Xm Ys left`` estimate for the live progress bar (blank when
        not yet computable)."""
        secs = self.eta_seconds()
        if secs is None:
            return ""
        if secs <= 0:
            return "finishing…"
        secs = int(round(secs))
        if secs < 60:
            return f"~{max(secs, 1)}s left"
        minutes, s = divmod(secs, 60)
        if minutes < 60:
            return f"~{minutes}m {s:02d}s left"
        hours, m = divmod(minutes, 60)
        return f"~{hours}h {m:02d}m left"

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


class QueueState(models.Model):
    """Singleton holding cross-process job-queue state.

    Currently just the request-thread hysteresis gate. Production runs several
    gunicorn workers, so a per-process flag would diverge between them; this row
    is read and written *only* inside the dispatcher's advisory-locked
    transaction (``_dispatch_next``), giving every worker one shared gate. The
    gate is closed once the global request-thread usage reaches the HIGH cap and
    reopens only when it drains back to the LOW watermark.

    ``seeded`` records whether the gate has been reconciled from the live thread
    count at least once. A brand-new row (fresh DB, or the deploy that first
    creates this singleton) defaults the gate *open*; if that deploy lands while
    request jobs are already mid-band the open default would wrongly admit more
    churn. The dispatcher therefore treats an unseeded row as "unknown" and
    rebuilds the gate from live state on its first pass (see ``_dispatch_next``).
    """

    SINGLETON_ID = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_ID)
    request_gate_open = models.BooleanField(default=True)
    seeded = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"QueueState(request_gate_open={self.request_gate_open})"

    @classmethod
    def load(cls):
        """Fetch (creating on first use) the singleton row."""
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_ID)
        return obj


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
        QA_REVIEW = "qa_review", "QA Review"
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
        MENTIONED = "mentioned", "Mention"

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


class CollegeMatch(models.Model):
    """A persisted College Dual Match result (canonical 65-column schema).

    Every ``college_dual_match`` scrape and every historical-CSV import upserts
    here, deduped by :attr:`match_hash` (a normalized identity digest computed in
    :mod:`accounts.college_store`) so re-runs only insert genuinely new matches.
    The full 65-column record is kept verbatim in :attr:`data` (JSON); a handful
    of identity columns are promoted out for the Lab "Match database" tab's
    listing + stats and for indexing. ``first_seen_run`` points at the run that
    first inserted the row (null for imported rows).
    """

    SOURCE_SCRAPE = "scrape"
    SOURCE_IMPORT = "import"
    SOURCE_CHOICES = [
        (SOURCE_SCRAPE, "Scrape"),
        (SOURCE_IMPORT, "Import"),
    ]

    match_hash = models.CharField(max_length=64, unique=True)
    data = models.JSONField(default=dict)
    date_norm = models.CharField(max_length=32, blank=True, db_index=True)
    tournament_name = models.CharField(max_length=300, blank=True)
    draw_name = models.CharField(max_length=120, blank=True)
    draw_gender = models.CharField(max_length=20, blank=True)
    winner_team = models.CharField(max_length=200, blank=True)
    loser_team = models.CharField(max_length=200, blank=True)
    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default=SOURCE_SCRAPE
    )
    first_seen_run = models.ForeignKey(
        Run,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="inserted_matches",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["-created_at"])]

    def __str__(self):
        return f"{self.winner_team} def. {self.loser_team} ({self.date_norm})"


class SAKey(models.Model):
    """One SportyHQ *tournament key* in a queue-driven scraper's work list.

    The South Africa (Tennis South Africa / SportyHQ) scraper is queue-driven:
    instead of a date range it works through a list of tournament keys, each of
    which unlocks one tournament's full result set from the public SportyHQ
    results API. The queue is seeded once from the vendored key list; every key
    tracks whether it has been scraped, how many matches it yielded, and which
    run last touched it. Pasting keys in the Lab's Real-time tab upserts rows
    here too. The Lab's "Key queue" tab surfaces the queue + its progress.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    scraper = models.ForeignKey(
        "Scraper", related_name="sa_keys", on_delete=models.CASCADE
    )
    tournament_key = models.CharField(max_length=64, unique=True, db_index=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    name = models.CharField(max_length=300, blank=True)
    num_results = models.IntegerField(null=True, blank=True)
    last_run = models.ForeignKey(
        "Run",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    scraped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["status", "tournament_key"]
        indexes = [models.Index(fields=["scraper", "status"])]

    def __str__(self):
        return f"{self.tournament_key} ({self.status})"


class ScraperSchedule(models.Model):
    """Per-scraper recurring-run configuration for the in-app scheduler.

    One row per scraper, created on demand from the Lab's Schedule tab. When
    ``enabled`` the background scheduler thread (:mod:`accounts.scheduler`)
    launches the scraper on the chosen cadence via the *same* run-start path as
    the Real-time button and the trigger webhook, so every guard (maintenance,
    single-in-flight, browser-exclusivity) still applies.

    ``next_run_at`` is the authoritative UTC instant the next run is due; it is
    recomputed whenever the schedule is saved and after every fire. There is no
    backfill — a schedule missed while the app was offline fires once on
    recovery, then resumes its cadence. ``anchor_date`` pins the fortnight parity
    for the ``biweekly`` cadence and is unused by the other frequencies.
    """

    class Frequency(models.TextChoices):
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Every 2 weeks"
        MONTHLY = "monthly", "Monthly"

    # 0=Mon … 6=Sun, matching Python's ``date.weekday()``.
    WEEKDAYS = [
        (0, "Monday"),
        (1, "Tuesday"),
        (2, "Wednesday"),
        (3, "Thursday"),
        (4, "Friday"),
        (5, "Saturday"),
        (6, "Sunday"),
    ]

    scraper = models.OneToOneField(
        Scraper, on_delete=models.CASCADE, related_name="schedule"
    )
    enabled = models.BooleanField(default=False)
    frequency = models.CharField(
        max_length=12, choices=Frequency.choices, default=Frequency.DAILY
    )
    time_of_day = models.TimeField(default=dtime(6, 0))
    # Used by weekly + biweekly cadences (ignored otherwise).
    weekday = models.PositiveSmallIntegerField(default=0)
    # Used by the monthly cadence (clamped to the month's length at compute time).
    day_of_month = models.PositiveSmallIntegerField(default=1)
    timezone = models.CharField(max_length=64, default="UTC")
    # Fortnight parity anchor for the biweekly cadence (the first scheduled local
    # date); recomputed on every save of a biweekly schedule, else NULL.
    anchor_date = models.DateField(null=True, blank=True)
    # Authoritative next-due instant, stored UTC. NULL when disabled.
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_fired_at = models.DateTimeField(null=True, blank=True)
    last_run = models.ForeignKey(
        "Run",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["enabled", "next_run_at"])]

    def __str__(self):
        return (
            f"Schedule({self.scraper_id}, {self.frequency}, "
            f"enabled={self.enabled})"
        )


class ScheduleEvent(models.Model):
    """One row per in-app-scheduler fire attempt — the Lab's "Cron history".

    The scheduler thread (:mod:`accounts.scheduler`) writes one of these every
    time a schedule comes due, recording what happened so operators can see the
    cron is alive and why any given cycle did or didn't start a fresh run:

    - ``LAUNCHED`` — a run started immediately (it streams live on the Real-time
      tab).
    - ``QUEUED`` — a job was enqueued but capacity was full, so it waits in the
      Batch-jobs queue and starts automatically when a slot frees up.
    - ``SKIPPED_IN_FLIGHT`` — a run was already in progress, so the cycle was a
      healthy skip (the cron passed — a job is already working).
    - ``SKIPPED_MAINTENANCE`` — the source is in maintenance.
    - ``SKIPPED_DISABLED`` — the schedule was turned off between claim and launch.
    - ``FAILED`` — the run could not be started (``detail`` says why).

    ``created_at`` is when the cron fired; ``scheduled_for`` is the UTC instant
    the cycle was due for.
    """

    class Outcome(models.TextChoices):
        LAUNCHED = "launched", "Launched"
        QUEUED = "queued", "Queued"
        SKIPPED_IN_FLIGHT = "skipped_in_flight", "Skipped — run already in progress"
        SKIPPED_MAINTENANCE = "skipped_maintenance", "Skipped — in maintenance"
        SKIPPED_DISABLED = "skipped_disabled", "Skipped — schedule disabled"
        FAILED = "failed", "Failed to start"

    scraper = models.ForeignKey(
        Scraper, on_delete=models.CASCADE, related_name="schedule_events"
    )
    outcome = models.CharField(max_length=24, choices=Outcome.choices)
    detail = models.CharField(max_length=500, blank=True, default="")
    # UTC instant the cycle was due for (the schedule's next_run_at at claim
    # time); the actual fire time is ``created_at``.
    scheduled_for = models.DateTimeField(null=True, blank=True)
    run = models.ForeignKey(
        "Run", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["scraper", "-created_at"])]

    @property
    def is_failure(self):
        return self.outcome == self.Outcome.FAILED

    def __str__(self):
        return f"ScheduleEvent({self.scraper_id}, {self.outcome})"


class PlayerGenderCache(models.Model):
    """Cached name -> gender inference for tournamentsoftware scrapers.

    The tournamentsoftware player profile carries no gender field, so gender is
    inferred from the player's *name* by Claude (see
    :mod:`accounts.live_scrapers._claude_gender`). Because that inference is a
    pure function of the name, the result is cached here by a normalised name
    key so each distinct player is looked up at most once, ever.

    ``gender`` stores the raw inference code: ``"M"`` / ``"F"`` / ``"U"`` (U =
    ambiguous / unknown). Ambiguous answers are cached too so they are never
    re-asked; callers map ``"U"`` to an empty output gender.
    """

    class Code(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"
        UNKNOWN = "U", "Unknown"

    name_key = models.CharField(max_length=255, unique=True)
    gender = models.CharField(max_length=1, choices=Code.choices)
    display_name = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"PlayerGenderCache({self.name_key!r} -> {self.gender})"


class GeneralConfig(models.Model):
    """Singleton, workspace-wide configuration edited from the Settings page.

    Currently stores the Anthropic (Claude) API key used by the AI scrapers
    (name-based gender inference and the college box-score parser). When it is
    blank the code falls back to the environment-sourced
    ``settings.CLAUDE_KEYS`` (``CLAUDE_KEYS`` / ``ANTHROPIC_API_KEY``), so the
    hosted secret keeps working until an admin overrides it here.

    The key is a secret: never log it. Status text and summaries use
    :attr:`masked_anthropic_key`; the one exception is the superuser-only
    Settings key input, which prefills the raw value behind a Reveal/Hide
    toggle so an admin can view and edit it (mirroring the per-scraper Claude
    key field in the Lab).
    """

    SINGLETON_PK = 1

    anthropic_api_key = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "general configuration"
        verbose_name_plural = "general configuration"

    def __str__(self):
        return "GeneralConfig"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj

    @classmethod
    def claude_keys(cls):
        """Workspace Anthropic keys as a list: the UI-configured value if set,
        else the env-sourced ``settings.CLAUDE_KEYS``. Comma-separate to rotate.
        """
        try:
            configured = (cls.get_solo().anthropic_api_key or "").strip()
        except Exception:
            configured = ""
        if configured:
            return [k.strip() for k in configured.split(",") if k.strip()]
        return [k for k in (getattr(settings, "CLAUDE_KEYS", []) or []) if k]

    @property
    def masked_anthropic_key(self):
        """Masked form of the stored key for display; ``""`` when unset."""
        key = (self.anthropic_api_key or "").strip()
        if not key:
            return ""
        if len(key) <= 10:
            return "•" * len(key)
        return key[:6] + "…" + key[-4:]
