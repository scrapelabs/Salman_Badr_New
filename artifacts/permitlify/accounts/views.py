import hmac
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import (
    authenticate,
    get_user_model,
    login,
    logout,
    update_session_auth_hash,
)
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, connection, transaction
from django.db.models import (
    Case,
    Count,
    DateTimeField,
    Exists,
    F,
    IntegerField,
    Max,
    OuterRef,
    Subquery,
    Value,
    When,
)
from django.http import Http404, HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import college_store, scheduling
from .live_scrapers import _ssrf, registry
from .models import (
    CollegeMatch,
    GeneralConfig,
    Proxy,
    QueueState,
    Run,
    SAKey,
    Scraper,
    ScraperModelFile,
    ScraperSchedule,
    Ticket,
)
from .system_stats import collect_system_stats, gauge_card
from .runs import ALL_TOURNAMENTS

# A run is only reaped once its worker has gone *silent* for this long (no new
# streamed log line). This is an inactivity window, NOT a cap on total run
# duration — an actively-streaming worker keeps advancing its last-activity time
# and is never reaped, so legitimately long scrapes run to completion.
RUN_INACTIVITY_TIMEOUT = timezone.timedelta(minutes=30)
# Postgres advisory-lock key that serializes run-start decisions so the
# in-flight / browser-exclusivity checks in ``_start_scraper_run`` are race-free
# even when several triggers (e.g. scheduled webhooks) fire at the same instant.
RUN_START_LOCK_KEY = 0x6D6D7273  # "mmrs" — MatchMiner run-start
# Global request-thread budget for the job queue. Request-based scrapers run
# concurrently as long as the sum of their worker-pool sizes stays within these
# bounds, with hysteresis: once the live thread count reaches HIGH the dispatcher
# stops admitting new request jobs until it drains back down to LOW. This avoids
# thrashing at the ceiling. Browser-based scrapers ignore this — they run alone.
REQUEST_THREAD_CAP_HIGH = 30
REQUEST_THREAD_RESUME_LOW = 10
# The hysteresis gate for request-job admission (True == admitting) is persisted
# in the QueueState singleton, read/written only inside the dispatcher's
# advisory-locked transaction so all gunicorn workers share one gate and the
# LOW/HIGH band is honoured across processes, not just within one worker.
YEAR_MIN = 2000
YEAR_MAX = 2030
IS_WINDOWS = os.name == "nt"

# Curated IANA timezones offered in the in-app scheduler's dropdown. The chosen
# value is validated against this set on save (anything else falls back to UTC),
# which also keeps an attacker from stuffing an arbitrary string into the field.
SCHEDULE_TIMEZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Phoenix",
    "America/Toronto",
    "America/Mexico_City",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Athens",
    "Europe/Moscow",
    "Europe/Istanbul",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Australia/Sydney",
    "Pacific/Auckland",
]

# Date-range run inputs (date_range / date_range_or_url scrapers).
DEFAULT_RANGE_DAYS = 30   # webhook window when a scheduled call omits dates
BIWEEKLY_DEFAULT_DAYS = 15  # rolling-window size when bi_weekly omits a day count
MAX_RANGE_DAYS = 400      # reject absurd windows
MAX_URL_LEN = 2048
MAX_API_KEY_LEN = 200     # cap a user-supplied feed API key
MAX_TEXT_FIELD_LEN = 100  # cap a free-text inert field (state / country)
FEED_GENDERS = ("both", "boys", "girls")  # feed gender selector options
MONTHS = [
    (1, "January"), (2, "February"), (3, "March"), (4, "April"),
    (5, "May"), (6, "June"), (7, "July"), (8, "August"),
    (9, "September"), (10, "October"), (11, "November"), (12, "December"),
]

TAB_LABELS = {
    "batch": "Batch jobs",
    "real-time": "Real-time test",
    "calls": "Calls history",
    "data": "Match database",
    "keys": "Key queue",
    "schedule": "Schedule",
    "settings": "Settings",
    "status": "Status",
}

CALLS_PER_PAGE = 12
JOBS_PER_PAGE = 15        # rows per page in the "Batch jobs" tab
MATCHES_PER_PAGE = 50     # rows per page in the "Match database" tab
KEYS_PER_PAGE = 50        # rows per page in the "Key queue" tab
MAX_KEY_BATCH_PASTE = 20000  # cap how many chars/keys we scan from a paste
LOG_LINES_PER_PAGE = 150
# Live console keeps only the most recent N streamed lines in the DOM (and only
# fetches that many on initial load) so an in-flight run with a huge log stays light.
LIVE_CONSOLE_CAP = 1200


def _counts():
    return {
        "scrapers": Scraper.objects.count(),
        "proxies": Proxy.objects.count(),
        "apis": 6,
        "logs": Run.objects.count(),
        "users": get_user_model().objects.count(),
        "qa_open": Ticket.objects.exclude(status=Ticket.Status.DONE).count(),
    }


def _app_ctx(active_nav, **extra):
    ctx = {"counts": _counts(), "active_nav": active_nav}
    ctx.update(extra)
    return ctx


def _scrapers_annotated():
    latest_status = (
        Run.objects.filter(scraper=OuterRef("pk"))
        .order_by("-started_at")
        .values("status")[:1]
    )
    return Scraper.objects.select_related("proxy").annotate(
        run_count=Count("runs"),
        last_run_at=Max("runs__started_at"),
        latest_status=Subquery(latest_status),
        is_running=Exists(
            Run.objects.filter(scraper=OuterRef("pk"), status=Run.Status.RUNNING)
        ),
    )


# Per-scraper health badge shown on the Scrapers table + Overview monitor.
RUN_STATE_LABELS = {
    "running": "Running",
    "healthy": "Healthy",
    "failed": "Failed",
    "stopped": "Stopped",
    "idle": "Idle",
}


def _derive_run_state(is_running, latest_status):
    """Collapse a scraper's in-flight flag + last run status into one badge state."""
    if is_running:
        return "running"
    if latest_status is None:
        return "idle"
    if latest_status == Run.Status.FAILED:
        return "failed"
    if latest_status == Run.Status.STOPPED:
        return "stopped"
    if latest_status in (Run.Status.SUCCESS, Run.Status.PARTIAL):
        return "healthy"
    return "idle"


def _run_status_state(run):
    """Badge state for a single Run row (Overview "recently active" table)."""
    if run.status == Run.Status.RUNNING:
        return "running"
    if run.status == Run.Status.QUEUED:
        return "queued"
    if run.status == Run.Status.FAILED:
        return "failed"
    if run.status == Run.Status.STOPPED:
        return "stopped"
    return "healthy"  # success / partial


def _job_state(run):
    """Badge state for a Batch-jobs table row (adds the queued state)."""
    if run.status == Run.Status.RUNNING:
        return "running"
    if run.status == Run.Status.QUEUED:
        return "queued"
    if run.status == Run.Status.FAILED:
        return "failed"
    if run.status == Run.Status.STOPPED:
        return "stopped"
    return "healthy"  # success / partial


def _run_params_label(run):
    """Short, human label of a run's inputs for the Batch-jobs table.

    Prefers the stored display label (``Run.tournament``), which is always built
    without any secret (a feed API key never enters it). Falls back to the run's
    year (year-only sources store the generic "all tournaments" label) so queued
    jobs stay distinguishable, and otherwise to a dash. A feed ``api_key`` lives
    only in ``Run.params`` and is never surfaced here.
    """
    label = (run.tournament or "").strip()
    params = run.params if isinstance(run.params, dict) else {}
    if (not label or label == ALL_TOURNAMENTS) and params.get("year"):
        year = params["year"]
        return f"{ALL_TOURNAMENTS} · {year}" if label else str(year)
    return label or "—"


def _with_run_state(scrapers):
    """Materialise an annotated scraper queryset, attaching badge state attrs."""
    items = list(scrapers)
    for s in items:
        s.run_state = _derive_run_state(s.is_running, s.latest_status)
        s.run_state_label = RUN_STATE_LABELS[s.run_state]
    return items


def _threads_running():
    """(total worker threads, scraper count) for scrapers with an in-flight run.

    Each running scraper contributes its worker-pool size, so 5 scrapers running
    5 threads each reports 25 — the live concurrency across the platform.
    """
    running = list(
        Scraper.objects.filter(runs__status=Run.Status.RUNNING).distinct()
    )
    return sum(s.worker_count for s in running), len(running)


def _recent_runs(finished_limit=5):
    """All in-flight runs, then the latest N finished runs (newest first)."""
    running = list(
        Run.objects.filter(status=Run.Status.RUNNING)
        .select_related("scraper")
        .order_by("-started_at")
    )
    finished = list(
        Run.objects.exclude(
            status__in=[Run.Status.RUNNING, Run.Status.QUEUED]
        )
        .select_related("scraper")
        .order_by("-started_at")[:finished_limit]
    )
    for r in running + finished:
        r.run_state = _run_status_state(r)
    return running + finished


def _run_brief(run):
    return {
        "slug": run.scraper.slug,
        "code": run.scraper.code,
        "name": run.scraper.name,
        "status": run.status,
        "status_label": run.get_status_display(),
        "state": _run_status_state(run),
        "started_human": f"{timesince(run.started_at)} ago",
        "rows": run.row_count,
        "duration_label": run.duration_label,
        "log_url": reverse("run_log", args=[run.scraper.slug, run.uuid]),
        "detail_url": f"{reverse('scraper_detail', args=[run.scraper.slug])}?tab=batch",
    }


def _queued_jobs(limit=5):
    """The latest jobs waiting in the batch queue (status=queued), newest first."""
    jobs = list(
        Run.objects.filter(status=Run.Status.QUEUED)
        .select_related("scraper")
        .order_by("-created_at")[:limit]
    )
    for r in jobs:
        r.run_state = "queued"
        r.params_label = _run_params_label(r)
    return jobs


def _queued_brief(run):
    return {
        "slug": run.scraper.slug,
        "code": run.scraper.code,
        "name": run.scraper.name,
        "status_label": run.get_status_display(),
        "state": "queued",
        "params": _run_params_label(run),
        "queued_human": f"{timesince(run.created_at)} ago",
        "detail_url": f"{reverse('scraper_detail', args=[run.scraper.slug])}?tab=batch",
    }


def _monitor_cards(sys_stats):
    """Gauge-ready CPU / Memory / Disk cards from a collect_system_stats() dict."""
    cpu, mem, disk = sys_stats["cpu"], sys_stats["mem"], sys_stats["disk"]
    return {
        "cpu": gauge_card(
            "CPU", cpu, f"{cpu.get('cores', 0)} cores · system load"
        ),
        "mem": gauge_card(
            "Memory", mem, f"{mem.get('used_gb', 0)} / {mem.get('total_gb', 0)} GB"
        ),
        "disk": gauge_card(
            "Disk", disk, f"{disk.get('used_gb', 0)} / {disk.get('total_gb', 0)} GB"
        ),
    }


def _launch_run(run):
    """Spawn the run as a detached ``manage.py run_scrape <uuid>`` subprocess.

    The child is placed in its own process group so the real-time Stop button can
    force-kill it (POSIX: ``start_new_session`` → setsid; Windows:
    ``CREATE_NEW_PROCESS_GROUP``); we persist its PID on the Run.
    """
    popen_kwargs = {
        "cwd": str(settings.BASE_DIR),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [sys.executable, "manage.py", "run_scrape", str(run.uuid)],
        **popen_kwargs,
    )
    run.pid = proc.pid
    run.save(update_fields=["pid"])


def _reap_stale_runs(scraper=None):
    """Fail runs whose worker has gone silent (died), based on *inactivity*.

    A run is reaped only after it has streamed no new log line for
    ``RUN_INACTIVITY_TIMEOUT``. This is deliberately NOT a cap on total run
    duration: a worker actively streaming match results keeps advancing its
    last-activity timestamp, so legitimately long scrapes are never killed —
    only genuinely stuck/dead workers (the original reason for reaping) are.

    Also force-kills any surviving worker process group so a stuck worker can't
    keep running — or resurface its status — after we release the RUNNING lock.

    With ``scraper=None`` the sweep covers **every** scraper, not just one. The
    run-start path uses that so a crashed run on another source can't hold the
    cross-source browser-exclusivity lock (below) forever.
    """
    now = timezone.now()
    cutoff = now - RUN_INACTIVITY_TIMEOUT
    running = (
        Run.objects.filter(status=Run.Status.RUNNING)
        if scraper is None
        else scraper.runs.filter(status=Run.Status.RUNNING)
    )
    for run in running:
        last_line = run.log_lines.order_by("-seq").first()
        # Last sign of life: the newest streamed log line, else the start time.
        last_activity = last_line.created_at if last_line else run.started_at
        if last_activity is None or last_activity >= cutoff:
            # Still streaming (or only just started) — leave it running.
            continue
        _terminate_run_worker(run, settle=0)
        run.status = Run.Status.FAILED
        run.finished_at = now
        if run.started_at:
            run.duration_ms = int((now - run.started_at).total_seconds() * 1000)
        if not run.log_text:
            lines = list(run.log_lines.order_by("seq").values_list("text", flat=True))
            idle_min = int(RUN_INACTIVITY_TIMEOUT.total_seconds() // 60)
            lines.append(
                f"[reaper] Worker streamed no new output for {idle_min} min and "
                "was assumed dead; run marked failed. (This is not a duration cap "
                "— actively-streaming runs are never reaped.)"
            )
            run.log_text = "\n".join(lines) + "\n"
        run.save(update_fields=["status", "finished_at", "duration_ms", "log_text"])


def _capacity_snapshot():
    """Current live-run state used to decide what the queue can promote next.

    Returns ``(running, browser_running, threads_in_use, running_scraper_ids)``:

    - ``running`` — the list of RUNNING runs (scraper preselected),
    - ``browser_running`` — True if any RUNNING run is a browser-based source
      (which holds the whole host to itself),
    - ``threads_in_use`` — the sum of ``worker_count`` across RUNNING *non*-browser
      runs (the live slice of the global request-thread budget),
    - ``running_scraper_ids`` — the set of scraper ids with a RUNNING run (the
      one-run-per-scraper guard).
    """
    running = list(
        Run.objects.filter(status=Run.Status.RUNNING).select_related("scraper")
    )
    browser_running = False
    threads_in_use = 0
    running_scraper_ids = set()
    for r in running:
        running_scraper_ids.add(r.scraper_id)
        spec = registry.spec_for(r.scraper.slug)
        if spec and spec.uses_browser:
            browser_running = True
        else:
            threads_in_use += r.scraper.worker_count
    return running, browser_running, threads_in_use, running_scraper_ids


def _enqueue_run(scraper, *, inputs, launched_by):
    """Create a QUEUED run — the single admission point for the job queue.

    Shared by the real-time form, the batch form, the trigger webhook and the
    scheduler so every path lands one queued row. The only pre-queue guard is
    maintenance (a source in maintenance refuses work outright); the concurrency
    rules (one run per scraper, browser exclusivity, the thread budget) are NOT
    enforced here — that's the dispatcher's job. ``inputs`` is a validated
    :class:`RunInputs`. Returns the created QUEUED :class:`Run`; raises
    RunStartError(503) when the source is in maintenance.

    ``started_at`` is left at its model default (now) and is *reset* to the real
    start instant when the run is promoted to RUNNING; queue order is by
    ``created_at`` (auto, insert order), so it is stable regardless.
    """
    if scraper.is_maintenance:
        raise RunStartError(
            "maintenance", "This source is in maintenance — runs are blocked.", 503
        )
    return Run.objects.create(
        scraper=scraper,
        launched_by=launched_by,
        tournament=(inputs.tournament or ALL_TOURNAMENTS)[:120],
        date_from=inputs.date_from,
        date_to=inputs.date_to,
        params=inputs.params,
        status=Run.Status.QUEUED,
    )


def _promote_run(run):
    """Flip a QUEUED run to RUNNING and stamp a fresh start instant.

    ``pid`` is cleared (the worker sets its own once launched); ``started_at`` is
    reset to now so durations/Overview counts reflect the real start, not the time
    the job was enqueued.
    """
    run.status = Run.Status.RUNNING
    run.started_at = timezone.now()
    run.pid = None
    run.save(update_fields=["status", "started_at", "pid"])


def _dispatch_next(*, blocking=True):
    """Promote every currently-eligible QUEUED run to RUNNING and launch it.

    The single place that turns queued jobs into live ones. Honours, in order:

    - **one run per scraper** — a scraper with a live run is skipped,
    - **browser exclusivity** — a browser-based run needs the whole host, so it
      starts only when nothing else is live, and nothing else starts while it is,
    - **request-thread budget** — non-browser jobs are admitted FIFO while the
      global thread count stays within the LOW/HIGH hysteresis band.

    FIFO-strict: a head-of-queue browser job that can't start yet *blocks* the
    jobs behind it (the scan stops), so it can't be starved by a steady stream of
    later request jobs. Serialised across web workers by the run-start advisory
    lock; ``blocking=False`` (used by poll pumps) takes the lock only if free and
    otherwise defers to whichever caller holds it. Returns the list of launched
    runs. Workers are spawned *after* the promotion transaction commits.
    """
    _reap_stale_runs()
    to_launch = []
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                if blocking:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(%s)", [RUN_START_LOCK_KEY]
                    )
                else:
                    cur.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)", [RUN_START_LOCK_KEY]
                    )
                    if not cur.fetchone()[0]:
                        return []

            (
                running,
                browser_running,
                threads_in_use,
                running_scraper_ids,
            ) = _capacity_snapshot()

            # Shared hysteresis gate (persisted so all gunicorn workers agree).
            # Safe to read without row-locking: the advisory lock above already
            # serialises every dispatcher, so we are the only writer right now.
            gate = QueueState.load()
            gate_open = gate.request_gate_open
            need_seed = not gate.seeded

            if need_seed:
                # First dispatch since this gate row was created — fresh DB, or
                # the deploy/migration that first introduced the singleton. The
                # default is "open", but if that landed while request jobs were
                # already mid-band we'd wrongly admit more churn. We can't
                # reconstruct the pre-restart band, so reconcile conservatively
                # from the live thread count: only the drained-to-LOW state is
                # safely "open"; anything above LOW (mid-band or at/over HIGH)
                # starts closed so the queue drains before re-admitting. Persist
                # the reconciliation so every worker stops treating it as unknown.
                gate_open = threads_in_use <= REQUEST_THREAD_RESUME_LOW
            else:
                # Steady-state hysteresis: close at the HIGH cap, reopen only
                # once drained to LOW, otherwise hold the previous state.
                if threads_in_use >= REQUEST_THREAD_CAP_HIGH:
                    gate_open = False
                elif threads_in_use <= REQUEST_THREAD_RESUME_LOW:
                    gate_open = True

            queued = (
                Run.objects.select_for_update(skip_locked=True)
                .filter(status=Run.Status.QUEUED)
                .select_related("scraper")
                .order_by("created_at")
            )
            for run in queued:
                if run.scraper_id in running_scraper_ids:
                    # That source already has a live (or just-promoted) run —
                    # one run per scraper. Skip it; try the next source.
                    continue
                spec = registry.spec_for(run.scraper.slug)
                uses_browser = bool(spec and spec.uses_browser)

                if uses_browser:
                    # Needs the whole host: only startable when nothing is live or
                    # promoted this pass. FIFO-strict — if it can't go now, stop,
                    # never promote a later job past it (no starvation).
                    if running or to_launch:
                        break
                    _promote_run(run)
                    to_launch.append(run)
                    break  # it owns the server; nothing else starts this pass

                # Request-based job.
                if browser_running:
                    # A browser run owns the host — no request job may start. Stop.
                    break
                want = run.scraper.worker_count
                if not gate_open:
                    break
                if threads_in_use + want > REQUEST_THREAD_CAP_HIGH:
                    # Admitting this would breach the hard cap. FIFO: stop here.
                    break
                _promote_run(run)
                to_launch.append(run)
                running_scraper_ids.add(run.scraper_id)
                threads_in_use += want
                if threads_in_use >= REQUEST_THREAD_CAP_HIGH:
                    gate_open = False

            # Persist the gate only when it actually flipped (or on the first
            # reconciliation), so we avoid a write — and an updated_at churn — on
            # every dispatch cycle.
            if gate_open != gate.request_gate_open or need_seed:
                gate.request_gate_open = gate_open
                gate.seeded = True
                gate.save(
                    update_fields=["request_gate_open", "seeded", "updated_at"]
                )
    except Exception:  # noqa: BLE001
        logger.exception("Queue dispatch failed.")
        return []

    # Launch workers outside the lock/transaction. A spawn failure fails just that
    # run (and frees its slot for the next dispatch) instead of the whole batch.
    for run in to_launch:
        try:
            _launch_run(run)
        except Exception:  # noqa: BLE001
            run.status = Run.Status.FAILED
            run.finished_at = timezone.now()
            run.log_text = "Failed to launch the scraper process.\n"
            run.save(update_fields=["status", "finished_at", "log_text"])
            logger.exception("Failed to launch queued run %s.", run.short_id)
    return to_launch


def login_view(request):
    if request.user.is_authenticated:
        return redirect("overview")

    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            return redirect("overview")
        error = "Invalid username or password."

    return render(request, "login.html", {"error": error})


@login_required
def overview_view(request):
    today = timezone.localdate()
    threads_running, running_scrapers = _threads_running()
    ctx = _app_ctx(
        "overview",
        active_scrapers=Scraper.objects.filter(mode=Scraper.Mode.PRODUCTION).count(),
        runs_today=Run.objects.exclude(status=Run.Status.QUEUED)
        .filter(started_at__date=today)
        .count(),
        maint_count=Scraper.objects.filter(mode=Scraper.Mode.MAINTENANCE).count(),
        monitor=_monitor_cards(collect_system_stats()),
        threads_running=threads_running,
        running_scrapers=running_scrapers,
        queued_jobs=_queued_jobs(),
        recent_runs=_recent_runs(),
        live_stats_url=reverse("live_stats"),
    )
    return render(request, "overview.html", ctx)


@login_required
def scrapers_view(request):
    scrapers = _with_run_state(_scrapers_annotated().order_by("name"))
    threads_running, running_scrapers = _threads_running()
    return render(
        request,
        "scrapers.html",
        _app_ctx(
            "scrapers",
            scrapers=scrapers,
            threads_running=threads_running,
            running_scrapers=running_scrapers,
            live_stats_url=reverse("live_stats"),
        ),
    )


@login_required
def live_stats_view(request):
    """JSON feed polled by the Overview + Scrapers pages for real-time stats."""
    # Opportunistically pump the queue from these frequently-polled pages so the
    # batch queue keeps draining even when no Lab tab is open (non-blocking).
    _dispatch_next(blocking=False)
    threads_running, running_scrapers = _threads_running()
    today = timezone.localdate()
    scr_map = {
        s.slug: {
            "state": (state := _derive_run_state(s.is_running, s.latest_status)),
            "label": RUN_STATE_LABELS[state],
        }
        for s in _scrapers_annotated()
    }
    return JsonResponse(
        {
            "system": collect_system_stats(),
            "threads_running": threads_running,
            "running_scrapers": running_scrapers,
            "overview": {
                "active_scrapers": Scraper.objects.filter(
                    mode=Scraper.Mode.PRODUCTION
                ).count(),
                "runs_today": Run.objects.exclude(status=Run.Status.QUEUED)
                .filter(started_at__date=today)
                .count(),
                "maint_count": Scraper.objects.filter(
                    mode=Scraper.Mode.MAINTENANCE
                ).count(),
                "queued_jobs": [_queued_brief(r) for r in _queued_jobs()],
                "recent_runs": [_run_brief(r) for r in _recent_runs()],
            },
            "scrapers": scr_map,
        }
    )


MODEL_UPLOAD_MAX_BYTES = 100 * 1024 * 1024  # 100 MB ceiling for an uploaded model
# Accepted model containers, sniffed by magic bytes: Keras v3 / zip (``PK``) and
# legacy HDF5 (``.h5`` / ``.hdf5``).
_MODEL_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x89HDF\r\n\x1a\n")
_MODEL_EXTS = (".keras", ".h5", ".hdf5")


def _save_model_upload(scraper, upload, user):
    """Validate an uploaded model file and store it (DB blob). Returns an error
    string on rejection, or ``""`` on success."""
    import hashlib

    name = (upload.name or "").strip()
    if not name.lower().endswith(_MODEL_EXTS):
        return f"Unsupported file type — upload a {', '.join(_MODEL_EXTS)} model."
    if upload.size and upload.size > MODEL_UPLOAD_MAX_BYTES:
        mb = MODEL_UPLOAD_MAX_BYTES // (1024 * 1024)
        return f"File is too large (max {mb} MB)."

    data = upload.read()
    if len(data) > MODEL_UPLOAD_MAX_BYTES:
        mb = MODEL_UPLOAD_MAX_BYTES // (1024 * 1024)
        return f"File is too large (max {mb} MB)."
    if not data:
        return "The uploaded file is empty."
    if not data.startswith(_MODEL_MAGIC):
        return "That doesn't look like a Keras/HDF5 model file."

    ScraperModelFile.objects.update_or_create(
        scraper=scraper,
        defaults={
            "filename": name[:255],
            "content_type": (upload.content_type or "")[:80],
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": data,
            "uploaded_by": user,
        },
    )
    return ""


def _run_form_ctx(s, spec, ctx):
    """Populate the run-parameters form context shared by the Real-time and Batch
    tabs (both include ``partials/run_params_fields.html``).

    Adds the input-kind flag set, the year/month/date defaults and the feed-key /
    gender knobs. The Batch form posts the same field names, so it needs the same
    context as the Real-time form.
    """
    current_year = timezone.localdate().year
    today = timezone.localdate()
    ctx["input_kind"] = spec.input_kind
    # Queue-driven start form (south_africa): show how many keys are still
    # pending so the "run the pending queue" control has a live count.
    if spec.has_key_store:
        ctx["pending_key_count"] = SAKey.objects.filter(
            scraper=s, status=SAKey.Status.PENDING
        ).count()
        ctx["KEY_BATCH_MAX_KEYS"] = registry.KEY_BATCH_MAX_KEYS
    ctx["allows_url"] = spec.input_kind == registry.INPUT_DATE_RANGE_OR_URL
    ctx["url_required"] = spec.url_required
    ctx["accepts_sheet"] = spec.accepts_sheet
    ctx["years"] = list(range(YEAR_MAX, YEAR_MIN - 1, -1))
    ctx["default_year"] = min(max(current_year, YEAR_MIN), YEAR_MAX)
    ctx["months"] = MONTHS
    ctx["default_month"] = 0
    ctx["default_date_to"] = today.isoformat()
    ctx["default_date_from"] = (
        today - timedelta(days=DEFAULT_RANGE_DAYS)
    ).isoformat()
    # Rankings publish weekly on Mondays, so default the snapshot picker to
    # the most recent Monday rather than an arbitrary mid-week day.
    ctx["default_snapshot_date"] = (
        today - timedelta(days=today.weekday())
    ).isoformat()
    ctx["feed_api_key"] = spec.feed_api_key
    ctx["feed_api_key_default"] = spec.feed_api_key_default
    ctx["feed_gender"] = spec.feed_gender
    ctx["default_gender"] = "both"
    # Singles / Doubles / Both selector (rank-snapshot + maxpreps).
    ctx["rank_type"] = spec.rank_type
    ctx["default_rank_type"] = "both"
    # bi_weekly rolling-window toggle (date-range scrapers).
    ctx["bi_weekly"] = spec.bi_weekly
    ctx["default_bi_weekly_days"] = BIWEEKLY_DEFAULT_DAYS
    ctx["max_range_days"] = MAX_RANGE_DAYS
    # Inert State / Country text fields (brazil/uruguay year+month forms).
    ctx["wants_state"] = spec.wants_state
    ctx["wants_country"] = spec.wants_country
    ctx["default_state"] = ""
    ctx["default_country"] = ""
    return ctx


@login_required
@require_http_methods(["GET", "POST"])
def scraper_detail_view(request, slug):
    s = get_object_or_404(Scraper, slug=slug)

    # POST handles the tab forms, told apart by a hidden ``form`` field.
    if request.method == "POST":
        # Schedule tab: rotate the webhook trigger token.
        if request.POST.get("form") == "schedule-rotate-token":
            s.rotate_trigger_token()
            messages.success(
                request,
                "Trigger token regenerated — update the GitHub secret with the new "
                "value or scheduled runs will start failing.",
            )
            return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=schedule")

        # Settings tab: save the per-scraper proxy selection (admins only).
        if request.POST.get("form") == "settings":
            if not request.user.is_superuser:
                messages.error(
                    request,
                    "Only administrators can change routing & performance settings.",
                )
                return redirect(
                    f"{reverse('scraper_detail', args=[slug])}?tab=real-time"
                )
            proxy = None
            proxy_id = (request.POST.get("proxy") or "").strip()
            if proxy_id.isdigit():
                proxy = Proxy.objects.filter(pk=int(proxy_id)).first()
            s.proxy = proxy

            try:
                threads = int((request.POST.get("threads") or "").strip())
            except (TypeError, ValueError):
                threads = s.threads or Scraper.THREADS_DEFAULT
            s.threads = max(
                Scraper.THREADS_MIN, min(threads, Scraper.THREADS_MAX)
            )

            try:
                max_tries = int((request.POST.get("max_tries") or "").strip())
            except (TypeError, ValueError):
                max_tries = s.max_tries or Scraper.TRIES_DEFAULT
            s.max_tries = max(
                Scraper.TRIES_MIN, min(max_tries, Scraper.TRIES_MAX)
            )

            update_fields = ["proxy", "threads", "max_tries", "updated_at"]
            # Display labels: rename the scraper's name and badge (cosmetic only —
            # the slug, registry key and behaviour are unchanged). Empty submissions
            # keep the existing value so a blank field never wipes a label.
            # Collapse any whitespace/newlines to single spaces so a label stays a
            # single printable line (it's interpolated into the Schedule-tab YAML).
            if "name" in request.POST:
                new_name = " ".join((request.POST.get("name") or "").split())
                if new_name:
                    s.name = new_name[:120]
                    update_fields.append("name")
            if "code" in request.POST:
                new_code = " ".join((request.POST.get("code") or "").split())
                if new_code:
                    s.code = new_code[:16]
                    update_fields.append("code")
            # AI scrapers (e.g. college_dual_match) carry a Claude API key field.
            # Only persist it for scrapers that surface it, and only when the field
            # is present in the POST (so other settings saves never clobber it).
            if registry.spec_for(s.slug).needs_claude and "claude_api_key" in request.POST:
                s.claude_api_key = (request.POST.get("claude_api_key") or "").strip()
                update_fields.append("claude_api_key")
            if registry.spec_for(s.slug).needs_login:
                if "login_username" in request.POST:
                    s.login_username = (request.POST.get("login_username") or "").strip()
                    update_fields.append("login_username")
                if "login_password" in request.POST:
                    s.login_password = request.POST.get("login_password") or ""
                    update_fields.append("login_password")
            # Scrapers needing a single secret config string (e.g. australia_tennis ->
            # Azure Blob SAS URL) surface one masked field; only persist when present.
            if registry.spec_for(s.slug).secret_label and "secret_value" in request.POST:
                s.secret_value = (request.POST.get("secret_value") or "").strip()
                update_fields.append("secret_value")

            s.save(update_fields=update_fields)
            messages.success(request, "Scraper settings saved.")
            return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=settings")

        # Settings tab: upload / replace a large model file (e.g. the Belgium
        # captcha CNN). Admins only; stored in the DB so hosted runs can use it
        # without committing a multi-MB binary to the repo.
        if request.POST.get("form") == "model-upload":
            if not request.user.is_superuser:
                messages.error(
                    request, "Only administrators can upload scraper models."
                )
                return redirect(
                    f"{reverse('scraper_detail', args=[slug])}?tab=real-time"
                )
            if not registry.spec_for(s.slug).model_upload_label:
                messages.error(request, "This scraper doesn't take a model upload.")
                return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=settings")
            upload = request.FILES.get("model_file")
            if not upload:
                messages.error(request, "Choose a model file to upload.")
                return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=settings")
            err = _save_model_upload(s, upload, request.user)
            if err:
                messages.error(request, err)
            else:
                messages.success(request, f"Model “{upload.name}” uploaded.")
            return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=settings")

        # Settings tab: remove the uploaded model file.
        if request.POST.get("form") == "remove-model":
            if not request.user.is_superuser:
                messages.error(
                    request, "Only administrators can remove scraper models."
                )
                return redirect(
                    f"{reverse('scraper_detail', args=[slug])}?tab=real-time"
                )
            ScraperModelFile.objects.filter(scraper=s).delete()
            messages.success(request, "Uploaded model removed.")
            return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=settings")

        # Schedule tab: save the in-app recurring-run configuration. Available to
        # any logged-in user (parity with pressing "Start" / the rotate-token
        # control). Every field is validated server-side and next_run_at is
        # recomputed so the background scheduler picks it up on its next tick.
        if request.POST.get("form") == "schedule-config":
            sched, _ = ScraperSchedule.objects.get_or_create(scraper=s)
            enabled = request.POST.get("enabled") == "on"

            frequency = request.POST.get("frequency", sched.frequency)
            if frequency not in ScraperSchedule.Frequency.values:
                frequency = ScraperSchedule.Frequency.DAILY

            time_of_day = _parse_time_of_day(
                request.POST.get("time_of_day"), sched.time_of_day
            )

            try:
                weekday = int(request.POST.get("weekday", sched.weekday))
            except (TypeError, ValueError):
                weekday = sched.weekday
            weekday = weekday if 0 <= weekday <= 6 else 0

            try:
                day_of_month = int(
                    request.POST.get("day_of_month", sched.day_of_month)
                )
            except (TypeError, ValueError):
                day_of_month = sched.day_of_month
            day_of_month = max(1, min(day_of_month, 31))

            tz_name = (
                request.POST.get("timezone") or sched.timezone or "UTC"
            ).strip()
            if tz_name not in SCHEDULE_TIMEZONES:
                tz_name = "UTC"

            sched.enabled = enabled
            sched.frequency = frequency
            sched.time_of_day = time_of_day
            sched.weekday = weekday
            sched.day_of_month = day_of_month
            sched.timezone = tz_name

            if enabled:
                now = timezone.now()
                # Biweekly pins its fortnight parity to the first scheduled local
                # date; the other cadences don't use an anchor.
                if frequency == ScraperSchedule.Frequency.BIWEEKLY:
                    sched.anchor_date = scheduling.first_anchor_date(
                        time_of_day=time_of_day,
                        weekday=weekday,
                        tz_name=tz_name,
                        after_utc=now,
                    )
                else:
                    sched.anchor_date = None
                sched.next_run_at = scheduling.compute_next_run(
                    frequency=frequency,
                    time_of_day=time_of_day,
                    weekday=weekday,
                    day_of_month=day_of_month,
                    tz_name=tz_name,
                    anchor_date=sched.anchor_date,
                    after_utc=now,
                )
            else:
                sched.next_run_at = None

            sched.save()
            messages.success(
                request,
                "Automatic schedule saved — the next run is queued."
                if enabled
                else "Automatic schedule turned off.",
            )
            return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=schedule")

        # Status tab: save Production/Maintenance status.
        mode = request.POST.get("mode", s.mode)
        if mode in (Scraper.Mode.PRODUCTION, Scraper.Mode.MAINTENANCE):
            s.mode = mode
        s.maintenance_message = request.POST.get(
            "maintenance_message", s.maintenance_message
        )
        s.save(update_fields=["mode", "maintenance_message", "updated_at"])
        messages.success(request, "Status updated.")
        return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=status")

    # Clicking a scraper lands on the Batch-jobs tab so the current status of its
    # jobs (running / queued / recent) is the first thing shown.
    tab = request.GET.get("tab", "batch")
    if tab not in TAB_LABELS:
        tab = "batch"
    # The Settings (routing & performance) tab is admin-only.
    if tab == "settings" and not request.user.is_superuser:
        return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=real-time")
    # The Match database tab only exists for scrapers that persist matches.
    if tab == "data" and not registry.spec_for(slug).has_match_store:
        return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=real-time")
    # The Key queue tab is retired from the UI — its live queue table was heavy
    # over a networked DB. Any ?tab=keys link (old bookmarks etc.) routes to the
    # Real-time tab, which still launches and monitors the queue-driven scraper.
    if tab == "keys":
        return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=real-time")

    ctx = _app_ctx("scrapers", s=s, tab=tab, tab_label=TAB_LABELS[tab])
    # Drives the nav: the "Match database" tab link only renders for scrapers
    # whose runner persists to CollegeMatch (currently college_dual_match).
    ctx["has_match_store"] = registry.spec_for(slug).has_match_store
    # Drives the nav: the "Key queue" tab link only renders for queue-driven
    # scrapers (currently south_africa).
    ctx["has_key_store"] = registry.spec_for(slug).has_key_store

    if tab == "real-time":
        # Reap dead runs across ALL scrapers so the browser-exclusivity check
        # below reflects reality (a crashed browser run elsewhere mustn't keep
        # every other source's start button disabled forever).
        _reap_stale_runs()
        active_run = (
            s.runs.filter(status=Run.Status.RUNNING).order_by("-started_at").first()
        )
        # The console is always attached: stream the live run, or replay the most
        # recent finished run when nothing is in flight.
        display_run = active_run or s.runs.order_by("-started_at").first()
        ctx["active_run"] = active_run
        ctx["display_run"] = display_run
        if active_run is not None:
            # Live stream: only fetch (and keep) the most recent LIVE_CONSOLE_CAP
            # lines so opening a long in-flight run stays light.
            latest_seq = (
                active_run.log_lines.order_by("-seq")
                .values_list("seq", flat=True)
                .first()
                or 0
            )
            ctx["console_after"] = max(0, latest_seq - LIVE_CONSOLE_CAP)
            ctx["console_cap"] = LIVE_CONSOLE_CAP
        elif display_run is not None:
            # Replay the most recent finished run, paginated so the page stays
            # light even when a run produced tens of thousands of log lines.
            log_lines = _run_lines(display_run)
            paginator = Paginator(log_lines, LOG_LINES_PER_PAGE)
            ctx["log_page"] = paginator.get_page(request.GET.get("logpage"))
            ctx["log_total"] = len(log_lines)
        spec = registry.spec_for(slug)
        # "Run now" always enqueues: a job that can't start immediately (this
        # source already running, or a browser source holding the host) WAITS in
        # the queue and dispatches automatically — it is never 409-blocked. So the
        # start controls are disabled ONLY for maintenance; capacity contention is
        # surfaced as an informational note, not a disabled button.
        blocker, this_uses_browser = _exclusivity_blocker(s)
        ctx["exclusivity_blocker"] = blocker
        ctx["start_disabled"] = bool(s.is_maintenance)
        if blocker is not None:
            ctx["start_block_msg"] = _exclusivity_block_msg(blocker, this_uses_browser)
        # The Real-time header carries a live badge of the currently-running job
        # for this scraper (kept in sync by the start-status poll).
        ctx["running_run"] = active_run
        ctx["uses_browser"] = bool(spec.uses_browser)
        _run_form_ctx(s, spec, ctx)
    elif tab == "batch":
        spec = registry.spec_for(slug)
        _reap_stale_runs()
        # One paginated table, ordered: RUNNING first (newest), then QUEUED
        # (oldest-first = FIFO dispatch order), then finished (newest). Mixed
        # sort directions per bucket are expressed with two NULL-gated keys.
        jobs_qs = (
            s.runs.annotate(
                _bucket=Case(
                    When(status=Run.Status.RUNNING, then=Value(0)),
                    When(status=Run.Status.QUEUED, then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField(),
                ),
                _q_ord=Case(
                    When(status=Run.Status.QUEUED, then=F("created_at")),
                    default=Value(None),
                    output_field=DateTimeField(),
                ),
                _f_ord=Case(
                    When(status=Run.Status.QUEUED, then=Value(None)),
                    default=F("started_at"),
                    output_field=DateTimeField(),
                ),
            )
            .select_related("scraper")
            .order_by("_bucket", "_q_ord", "-_f_ord")
        )
        paginator = Paginator(jobs_qs, JOBS_PER_PAGE)
        page_obj = paginator.get_page(request.GET.get("page"))
        for r in page_obj:
            r.params_label = _run_params_label(r)
            r.job_state = _job_state(r)
        ctx["page_obj"] = page_obj
        ctx["job_total"] = paginator.count
        queued_uuids = list(
            jobs_qs.filter(status=Run.Status.QUEUED)
            .order_by("created_at")
            .values_list("uuid", flat=True)
        )
        ctx["queued_count"] = len(queued_uuids)
        # Structural fingerprint for the live poller: a change to the running run
        # or the ordered queue (a promotion, cancel, or new enqueue) triggers a
        # table re-render. The page only shows one slice; this spans all queued.
        ctx["queued_uuids_csv"] = ",".join(str(u) for u in queued_uuids)
        ctx["running_run"] = (
            s.runs.filter(status=Run.Status.RUNNING).order_by("-started_at").first()
        )
        ctx["uses_browser"] = bool(spec.uses_browser)
        ctx["thread_cap_high"] = REQUEST_THREAD_CAP_HIGH
        ctx["thread_resume_low"] = REQUEST_THREAD_RESUME_LOW
        ctx["worker_count"] = s.worker_count
        _run_form_ctx(s, spec, ctx)
    elif tab == "calls":
        # Calls history is the record of runs that actually executed — queued
        # (not-yet-started) jobs live on the Batch jobs tab instead.
        paginator = Paginator(
            s.runs.exclude(status=Run.Status.QUEUED), CALLS_PER_PAGE
        )
        ctx["page_obj"] = paginator.get_page(request.GET.get("page"))
        ctx["run_total"] = paginator.count
    elif tab == "data":
        # The match database: all stored CollegeMatch rows for this scraper's
        # store, with headline stats + a paginated listing. Newest first.
        qs = CollegeMatch.objects.all()
        total = qs.count()
        last_run = (
            s.runs.filter(status=Run.Status.SUCCESS).order_by("-started_at").first()
        )
        last_match = qs.first()  # qs is ordered -created_at
        paginator = Paginator(qs, MATCHES_PER_PAGE)
        ctx["page_obj"] = paginator.get_page(request.GET.get("page"))
        ctx["match_total"] = total
        ctx["match_imported"] = qs.filter(
            source=CollegeMatch.SOURCE_IMPORT
        ).count()
        ctx["match_scraped"] = qs.filter(
            source=CollegeMatch.SOURCE_SCRAPE
        ).count()
        # Most recent successful run's row_count == matches it newly inserted.
        ctx["match_last_new"] = last_run.row_count if last_run else None
        ctx["match_last_run"] = last_run
        ctx["match_last_added"] = last_match.created_at if last_match else None
        # Defaults + presets for the "Download by date" panel (match date, not
        # scrape date): the form prefills the last 7 days; quick links cover
        # today / last 7 / last 30 days.
        today = timezone.localdate()
        ctx["dl_today"] = today.isoformat()
        ctx["dl_last7_from"] = (today - timedelta(days=6)).isoformat()
        ctx["dl_last30_from"] = (today - timedelta(days=29)).isoformat()
        ctx["dl_from"] = ctx["dl_last7_from"]
        ctx["dl_to"] = ctx["dl_today"]
    elif tab == "keys":
        # The key queue: a paginated listing of every SAKey for this scraper,
        # ordered pending-first then by key (the model's default Meta ordering).
        # Table only -- no headline stat cards, so the only DB work is the
        # paginator (one count + one page slice). Keeps the tab lean on a
        # networked DB.
        qs = s.sa_keys.select_related("last_run").all()
        paginator = Paginator(qs, KEYS_PER_PAGE)
        ctx["page_obj"] = paginator.get_page(request.GET.get("page"))
    elif tab == "schedule":
        spec = registry.spec_for(slug)
        trigger_url = request.build_absolute_uri(
            reverse("scraper_trigger", args=[s.slug])
        )
        default_year = min(max(timezone.localdate().year, YEAR_MIN), YEAR_MAX)
        today = timezone.localdate()
        sched_defaults = {
            "year": default_year,
            "month": 0,
            "date_from": (today - timedelta(days=DEFAULT_RANGE_DAYS)).isoformat(),
            "date_to": today.isoformat(),
            "snapshot_date": (today - timedelta(days=today.weekday())).isoformat(),
            "tournament_url": "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit",
            "api_key": spec.feed_api_key_default,
            "gender": "both",
        }
        secret_name = (
            "MATCHMINER_"
            + "".join(c if c.isalnum() else "_" for c in s.code.upper())
            + "_TRIGGER_TOKEN"
        )
        ctx["trigger_url"] = trigger_url
        ctx["default_year"] = default_year
        ctx["secret_name"] = secret_name
        ctx["input_kind"] = spec.input_kind
        ctx["schedule_curl_json"] = _trigger_example_json(
            spec.input_kind,
            sched_defaults,
            url_required=spec.url_required,
            feed_api_key=spec.feed_api_key,
            feed_gender=spec.feed_gender,
        )
        ctx["workflow_filename"] = f"{s.slug}-schedule.yml"
        ctx["workflow_yaml"] = _github_workflow_yaml(
            code=s.code,
            trigger_url=trigger_url,
            secret_name=secret_name,
            input_kind=spec.input_kind,
            defaults=sched_defaults,
            url_required=spec.url_required,
            feed_api_key=spec.feed_api_key,
            feed_gender=spec.feed_gender,
            bi_weekly=spec.bi_weekly,
        )
        # In-app scheduler config + dropdown choices for the "Run automatically"
        # card. The schedule row is created lazily so every scraper has one.
        sched, _ = ScraperSchedule.objects.get_or_create(scraper=s)
        ctx["schedule"] = sched
        ctx["freq_choices"] = ScraperSchedule.Frequency.choices
        ctx["weekday_choices"] = ScraperSchedule.WEEKDAYS
        ctx["dom_choices"] = list(range(1, 32))
        ctx["tz_choices"] = SCHEDULE_TIMEZONES
        ctx["schedule_time_value"] = sched.time_of_day.strftime("%H:%M")
        ctx["next_run_local"] = (
            sched.next_run_at.astimezone(scheduling.get_zone(sched.timezone))
            if (sched.enabled and sched.next_run_at)
            else None
        )
        # Cron history: the most recent scheduler fire attempts for this scraper,
        # each annotated with what happened (launched / healthy skip / failure).
        ctx["cron_events"] = list(
            s.schedule_events.select_related("run").order_by("-created_at")[:25]
        )
    elif tab == "settings":
        ctx["proxies"] = Proxy.objects.filter(is_active=True).order_by("name")
        ctx["thread_min"] = Scraper.THREADS_MIN
        ctx["thread_max"] = Scraper.THREADS_MAX
        ctx["tries_min"] = Scraper.TRIES_MIN
        ctx["tries_max"] = Scraper.TRIES_MAX
        ctx["needs_claude"] = registry.spec_for(slug).needs_claude
        ctx["needs_login"] = registry.spec_for(slug).needs_login
        ctx["login_label"] = registry.spec_for(slug).login_label
        ctx["login_user_label"] = registry.spec_for(slug).login_user_label
        ctx["secret_label"] = registry.spec_for(slug).secret_label
        ctx["secret_env_var"] = registry.spec_for(slug).secret_env_var
        ctx["model_upload_label"] = registry.spec_for(slug).model_upload_label
        ctx["model_filename"] = registry.spec_for(slug).model_filename
        ctx["model_file"] = (
            ScraperModelFile.objects.filter(scraper=s).defer("data").first()
            if registry.spec_for(slug).model_upload_label
            else None
        )

    return render(request, "scraper_detail.html", ctx)


def _exclusivity_blocker(scraper):
    """A RUNNING run on another scraper that blocks ``scraper`` from starting
    under the browser-exclusivity rule, or ``None``.

    Mirrors the guard in :func:`_create_guarded_run` so the Lab UI can disable the
    start controls in lockstep: a browser source (the itftennis family) can't
    start while ANY other run is live, and no source can start while a browser run
    is live. Returns ``(blocker_run_or_None, this_uses_browser)``.
    """
    spec = registry.spec_for(scraper.slug)
    this_uses_browser = bool(spec and spec.uses_browser)
    others = (
        Run.objects.filter(status=Run.Status.RUNNING)
        .exclude(scraper=scraper)
        .select_related("scraper")
    )
    if this_uses_browser:
        return others.first(), this_uses_browser
    for other in others:
        ospec = registry.spec_for(other.scraper.slug)
        if ospec and ospec.uses_browser:
            return other, this_uses_browser
    return None, this_uses_browser


def _exclusivity_block_msg(blocker, this_uses_browser):
    """Human explanation for why a new job would queue rather than start now."""
    if this_uses_browser:
        return (
            "This is a browser-based source and needs the server to itself, but "
            f"“{blocker.scraper.name}” is running. A new run will wait in the queue "
            "and start automatically once the server is free."
        )
    return (
        f"A browser-based scrape (“{blocker.scraper.name}”) is running and needs the "
        "server to itself. A new run will wait in the queue and start automatically "
        "once it finishes."
    )


@login_required
@require_http_methods(["GET"])
def scraper_start_status_view(request, slug):
    """JSON poll for the real-time tab: is this scraper's start blocked right now?

    Lets an open Lab page disable/re-enable its start controls live as browser
    runs elsewhere come and go — without a full page reload.
    """
    s = get_object_or_404(Scraper, slug=slug)
    # Opportunistically pump the queue so an idle Lab page left open helps drain
    # pending jobs even when nothing else is polling (non-blocking: defers if
    # another dispatcher holds the lock).
    _dispatch_next(blocking=False)
    _reap_stale_runs()
    blocker, this_uses_browser = _exclusivity_blocker(s)
    running = (
        s.runs.filter(status=Run.Status.RUNNING).order_by("-started_at").first()
    )
    return JsonResponse(
        {
            "own_running": running is not None,
            # Live data for the Real-time header job badge.
            "running_short_id": running.short_id if running else None,
            "running_uuid": str(running.uuid) if running else None,
            "blocked": blocker is not None,
            "block_msg": (
                _exclusivity_block_msg(blocker, this_uses_browser) if blocker else None
            ),
            "maintenance": s.is_maintenance,
        }
    )


class RunStartError(Exception):
    """A run could not be started; carries a machine code, message, and HTTP status.

    Lets the browser form (which renders a flash message) and the trigger webhook
    (which returns JSON + status) share one validation/launch path.
    """

    def __init__(self, code, message, status, run=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        # The created-but-failed Run when a launch fails after the row exists,
        # so callers (e.g. the scheduler's Cron history) can link to it.
        self.run = run


def _parse_year(raw, *, default_current=False):
    """Validate a season year. Returns an int or raises RunStartError(400).

    When ``default_current`` is set, an empty value falls back to the current year
    (clamped to the supported range) — used by the webhook where year is optional.
    """
    raw = (raw or "").strip()
    if not raw and default_current:
        return min(max(timezone.localdate().year, YEAR_MIN), YEAR_MAX)
    try:
        year = int(raw)
    except (TypeError, ValueError):
        raise RunStartError("invalid_year", "Select a year to run.", 400)
    if not (YEAR_MIN <= year <= YEAR_MAX):
        raise RunStartError(
            "invalid_year", f"Pick a year between {YEAR_MIN} and {YEAR_MAX}.", 400
        )
    return year


@dataclass
class RunInputs:
    """Validated, normalized inputs for one run.

    ``params`` is the canonical machine-readable form persisted on ``Run.params``.
    ``date_from`` / ``date_to`` / ``tournament`` mirror it for display in the UI
    and for scrapers that still read the date fields (e.g. Stadion).
    """

    params: dict
    date_from: object
    date_to: object
    tournament: str


def _parse_month(raw, *, default_all=False):
    """Validate a month: 1–12, or 0 meaning "all months"."""
    raw = (raw or "").strip()
    if not raw:
        if default_all:
            return 0
        raise RunStartError("invalid_month", "Select a month to run.", 400)
    try:
        month = int(raw)
    except (TypeError, ValueError):
        raise RunStartError(
            "invalid_month", "Pick a month 1–12, or 0 for the whole year.", 400
        )
    if month == 0 or 1 <= month <= 12:
        return month
    raise RunStartError(
        "invalid_month", "Pick a month 1–12, or 0 for the whole year.", 400
    )


def _parse_iso_date(raw, label):
    """Parse a ``YYYY-MM-DD`` calendar date or raise RunStartError(400)."""
    raw = (raw or "").strip()
    if not raw:
        raise RunStartError("invalid_date", f"Provide a {label} date.", 400)
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise RunStartError(
            "invalid_date", f"The {label} date must be in YYYY-MM-DD format.", 400
        )


def _month_end(year, month):
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _validate_tournament_url(raw, allowed_hosts):
    """Validate a user-supplied tournament URL (SSRF guard).

    Only ``http(s)`` URLs to an allowlisted host are accepted; IP-literal and
    local/internal hosts are rejected so the URL input can't be turned into a
    probe of the internal network.
    """
    url = (raw or "").strip()
    if not url:
        raise RunStartError("invalid_url", "Enter a tournament URL.", 400)
    if len(url) > MAX_URL_LEN:
        raise RunStartError("invalid_url", "That tournament URL is too long.", 400)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise RunStartError(
            "invalid_url", "Enter a full http(s) tournament URL.", 400
        )
    try:
        ipaddress.ip_address(host)
        raise RunStartError("invalid_url", "IP-address URLs are not allowed.", 400)
    except ValueError:
        pass  # a hostname, not an IP literal — good
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        raise RunStartError("invalid_url", "That host is not allowed.", 400)
    if allowed_hosts and not any(
        host == h or host.endswith("." + h) for h in allowed_hosts
    ):
        raise RunStartError(
            "invalid_url", "That URL's host isn't allowed for this scraper.", 400
        )
    # Resolve the host and reject anything that maps to a private/loopback/
    # link-local/reserved address. This also defeats numeric-host obfuscation
    # (decimal/octal/hex IPs) that the IP-literal check above can't see.
    try:
        _ssrf.assert_resolves_public(host)
    except _ssrf.UnsafeUrlError:
        raise RunStartError("invalid_url", "That host is not allowed.", 400)
    return url


def _normalize_feed_api_key(raw, default):
    """A feed API key: trimmed and length-capped; blank falls back to ``default``."""
    key = (raw or "").strip()
    if not key:
        return default
    if len(key) > MAX_API_KEY_LEN:
        raise RunStartError("invalid_api_key", "That API key is too long.", 400)
    return key


def _normalize_feed_gender(raw, *, default="both"):
    """A feed gender selector: ``boys`` / ``girls`` / ``both`` (blank -> default)."""
    gender = (raw or "").strip().lower()
    if not gender:
        return default
    if gender not in FEED_GENDERS:
        raise RunStartError(
            "invalid_gender", "Gender must be boys, girls, or both.", 400
        )
    return gender


def _parse_time_of_day(raw, fallback):
    """Parse an ``HH:MM`` (or ``HH:MM:SS``) string into a ``time``.

    Returns ``fallback`` on anything unparseable so a malformed submission never
    wipes the schedule's saved time-of-day.
    """
    raw = (raw or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return fallback


def _normalize_rank_type(raw, *, default="both"):
    """A Singles / Doubles / Both selector (blank -> default)."""
    rt = (raw or "").strip().lower()
    if not rt:
        return default
    if rt not in ("singles", "doubles", "both"):
        raise RunStartError(
            "invalid_rank_type", "Rank type must be singles, doubles, or both.", 400
        )
    return rt


def _parse_biweekly_days(raw, *, default=BIWEEKLY_DEFAULT_DAYS):
    """The rolling-window size in days (1..MAX_RANGE_DAYS; blank -> default)."""
    raw = (raw or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise RunStartError(
            "invalid_window",
            "The rolling-window size must be a whole number of days.",
            400,
        )
    if n < 1 or n > MAX_RANGE_DAYS:
        raise RunStartError(
            "invalid_window",
            f"The rolling-window size must be between 1 and {MAX_RANGE_DAYS} days.",
            400,
        )
    return n


def _year_month_extras(spec, get):
    """Collect the (inert) State / Country fields for year+month scrapers.

    These are carried on ``Run.params`` for display/parity with the supplied spec
    but do not change what the scraper fetches.
    """
    extra = {}
    if spec.wants_state:
        extra["state"] = (get("state") or "").strip()[:MAX_TEXT_FIELD_LEN]
    if spec.wants_country:
        extra["country"] = (get("country") or "").strip()[:MAX_TEXT_FIELD_LEN]
    return extra


def validate_run_params(spec, data, *, webhook=False):
    """Validate the start inputs for ``spec`` from a dict-like ``data``.

    Returns a :class:`RunInputs`. Shared by the browser start form (``data`` =
    ``request.POST``) and the trigger webhook (``data`` from JSON/POST). When
    ``webhook`` is set, missing inputs fall back to sensible scheduled defaults
    (current year/month, or a trailing window) instead of raising.
    """
    kind = spec.input_kind
    get = data.get

    if kind == registry.INPUT_NONE:
        # This scraper takes no inputs — it always fetches the current data
        # (e.g. padelfip world rankings). The runner derives any date itself.
        return RunInputs(
            params={},
            date_from=None,
            date_to=None,
            tournament="rankings",
        )

    if kind == registry.INPUT_YEAR_MONTH:
        year = _parse_year(get("year"), default_current=webhook)
        month = _parse_month(get("month"), default_all=webhook)
        extra = _year_month_extras(spec, get)
        if month:
            return RunInputs(
                params={"year": year, "month": month, **extra},
                date_from=date(year, month, 1),
                date_to=_month_end(year, month),
                tournament=f"{year}-{month:02d}",
            )
        return RunInputs(
            params={"year": year, "month": 0, **extra},
            date_from=date(year, 1, 1),
            date_to=date(year, 12, 31),
            tournament=f"{year} · all months",
        )

    if kind == registry.INPUT_RANK_SNAPSHOT:
        if webhook and not (get("snapshot_date") or "").strip():
            snap = timezone.localdate()
        else:
            snap = _parse_iso_date(get("snapshot_date"), "snapshot")
        params = {"single_date": snap.isoformat()}
        label = f"ranking @ {snap.isoformat()}"
        if spec.rank_type:
            rt = _normalize_rank_type(get("rank_type"))
            params["rank_type"] = rt
            if rt != "both":
                label = f"{label} · {rt}"
        return RunInputs(
            params=params,
            date_from=snap,
            date_to=snap,
            tournament=label,
        )

    if kind == registry.INPUT_KEY_BATCH:
        # Queue-driven scrapers (south_africa). Either run the WHOLE pending
        # SAKey queue in one run (the webhook / scheduler always does this) or
        # process an explicit paste of 32-hex tournament keys. Keys are
        # extracted, lower-cased and de-duplicated; the runner skips (and logs)
        # any key already marked done so it isn't re-scraped.
        run_all = webhook or (get("run_all") in ("on", "true", "1", True))
        if run_all:
            return RunInputs(
                params={"run_all": True, "keys": []},
                date_from=None,
                date_to=None,
                tournament="pending queue",
            )
        raw = (get("keys") or "")[:MAX_KEY_BATCH_PASTE]
        keys = list(
            dict.fromkeys(m.lower() for m in re.findall(r"[0-9a-fA-F]{32}", raw))
        )
        if not keys:
            raise RunStartError(
                "invalid_keys",
                "Paste at least one 32-character tournament key, or tick "
                "\u201crun the pending queue\u201d to process queued keys.",
                400,
            )
        keys = keys[: registry.KEY_BATCH_MAX_KEYS]
        return RunInputs(
            params={"run_all": False, "keys": keys},
            date_from=None,
            date_to=None,
            tournament=f"{len(keys)} key(s)",
        )

    if kind in (registry.INPUT_DATE_RANGE, registry.INPUT_DATE_RANGE_OR_URL):
        url_raw = (get("tournament_url") or "").strip()
        if kind == registry.INPUT_DATE_RANGE_OR_URL and url_raw:
            url = _validate_tournament_url(url_raw, spec.allowed_hosts)
            return RunInputs(
                params={"tournament_url": url},
                date_from=None,
                date_to=None,
                tournament=url,
            )
        if kind == registry.INPUT_DATE_RANGE_OR_URL and spec.url_required:
            raise RunStartError(
                "invalid_url",
                "This scraper needs a tournament / box-score / Google-Sheet "
                "URL — enter one to start the run.",
                400,
            )
        blank_dates = not (get("date_from") or "").strip() and not (
            get("date_to") or ""
        ).strip()
        bw_on = spec.bi_weekly and (
            get("bi_weekly_on") in ("on", "true", "1", True)
            or (webhook and blank_dates)
        )
        bw_days = None
        if bw_on:
            # Rolling window: a trailing N-day span ending today, recomputed on
            # every run so scheduled runs always grab the most recent N days.
            bw_days = _parse_biweekly_days(get("bi_weekly_days"))
            end = timezone.localdate()
            start = end - timedelta(days=bw_days)
        elif webhook and blank_dates:
            end = timezone.localdate()
            start = end - timedelta(days=DEFAULT_RANGE_DAYS)
        else:
            start = _parse_iso_date(get("date_from"), "start")
            end = _parse_iso_date(get("date_to"), "end")
        if start > end:
            raise RunStartError(
                "invalid_date",
                "The start date must be on or before the end date.",
                400,
            )
        if (end - start).days > MAX_RANGE_DAYS:
            raise RunStartError(
                "invalid_date",
                f"Keep the date range within {MAX_RANGE_DAYS} days.",
                400,
            )
        params = {"date_from": start.isoformat(), "date_to": end.isoformat()}
        label = f"{start.isoformat()} → {end.isoformat()}"
        if bw_on:
            params["bi_weekly"] = bw_days
            label = f"last {bw_days} days ({label})"
        if spec.rank_type:
            rt = _normalize_rank_type(get("rank_type"))
            params["rank_type"] = rt
            if rt != "both":
                label = f"{label} · {rt}"
        if spec.feed_api_key:
            params["api_key"] = _normalize_feed_api_key(
                get("api_key"), spec.feed_api_key_default
            )
        if spec.feed_gender:
            gender = _normalize_feed_gender(get("gender"))
            params["gender"] = gender
            if gender != "both":
                label = f"{label} · {gender}"
        return RunInputs(
            params=params,
            date_from=start,
            date_to=end,
            tournament=label,
        )

    # Default / INPUT_YEAR.
    year = _parse_year(get("year"), default_current=webhook)
    return RunInputs(
        params={"year": year},
        date_from=date(year, 1, 1),
        date_to=date(year, 12, 31),
        tournament=ALL_TOURNAMENTS,
    )


def _create_guarded_run(scraper, *, inputs, launched_by):
    """Apply every run-start guard and create the RUNNING row (does NOT launch).

    The single choke-point for *all* run creation — the real-time web form and the
    schedule webhook (via :func:`_start_scraper_run`) and the ``scrape_now`` CLI —
    so maintenance, stale-run reaping, the single-in-flight-run rule, and the
    browser-exclusivity rule are enforced identically everywhere. ``inputs`` is a
    validated :class:`RunInputs`. Raises RunStartError on any guard failure;
    returns the created :class:`Run`.
    """
    if scraper.is_maintenance:
        raise RunStartError(
            "maintenance", "This source is in maintenance — runs are blocked.", 503
        )
    # Sweep dead workers across ALL sources (not just this one): a crashed
    # browser run elsewhere would otherwise hold the exclusivity lock below.
    _reap_stale_runs()

    starting_spec = registry.spec_for(scraper.slug)
    starting_uses_browser = bool(starting_spec and starting_spec.uses_browser)

    try:
        with transaction.atomic():
            # Serialize run-start decisions so the in-flight / exclusivity checks
            # below can't race when several triggers fire at once (e.g. multiple
            # scheduled webhooks at the same minute). The advisory lock auto-
            # releases when this transaction commits — by which point the new
            # RUNNING row is visible to the next contender.
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", [RUN_START_LOCK_KEY])

            if scraper.runs.filter(status=Run.Status.RUNNING).exists():
                raise RunStartError(
                    "already_running",
                    "A run is already in progress for this source.",
                    409,
                )

            # Browser-based runs (the itftennis family) each drive a pool of
            # headless-Chrome instances, so they need the host to themselves: a
            # browser run can't start while ANY other run is live, and no run can
            # start while a browser run is live. Lightweight (curl) sources may
            # still run concurrently with one another.
            others = (
                Run.objects.filter(status=Run.Status.RUNNING)
                .exclude(scraper=scraper)
                .select_related("scraper")
            )
            blocker = None
            if starting_uses_browser:
                blocker = others.first()
            else:
                for other in others:
                    other_spec = registry.spec_for(other.scraper.slug)
                    if other_spec and other_spec.uses_browser:
                        blocker = other
                        break
            if blocker is not None:
                if starting_uses_browser:
                    msg = (
                        f"Can't start a browser-based run while “{blocker.scraper.name}” "
                        "is running — browser sources need the server to themselves. "
                        "Wait for it to finish or stop it first."
                    )
                else:
                    msg = (
                        f"A browser-based run (“{blocker.scraper.name}”) is in progress "
                        "and is using the server's resources. Wait for it to finish or "
                        "stop it first."
                    )
                raise RunStartError("busy", msg, 409)

            return Run.objects.create(
                scraper=scraper,
                launched_by=launched_by,
                tournament=(inputs.tournament or ALL_TOURNAMENTS)[:120],
                date_from=inputs.date_from,
                date_to=inputs.date_to,
                params=inputs.params,
                status=Run.Status.RUNNING,
                started_at=timezone.now(),
            )
    except IntegrityError:
        # Lost the race to the partial-unique constraint: another run is live.
        raise RunStartError(
            "already_running", "A run is already in progress for this source.", 409
        )


def _start_scraper_run(scraper, *, inputs, launched_by):
    """Guard + create the run (:func:`_create_guarded_run`) then launch the worker.

    Shared by the real-time browser form and the GitHub-Actions trigger webhook so
    both honour maintenance, stale-run reaping, the single-in-flight-run rule, the
    browser-exclusivity rule, and launch-failure handling identically. ``inputs``
    is a validated :class:`RunInputs`. Raises RunStartError on any guard failure.
    """
    run = _create_guarded_run(scraper, inputs=inputs, launched_by=launched_by)
    try:
        _launch_run(run)
    except Exception:  # noqa: BLE001
        run.status = Run.Status.FAILED
        run.finished_at = timezone.now()
        run.log_text = "Failed to launch the scraper process.\n"
        run.save(update_fields=["status", "finished_at", "log_text"])
        raise RunStartError(
            "launch_failed",
            "Could not start the run. Please try again.",
            503,
            run=run,
        )
    return run


@login_required
@require_http_methods(["POST"])
def scraper_run_view(request, slug):
    s = get_object_or_404(Scraper, slug=slug)
    back = f"{reverse('scraper_detail', args=[slug])}?tab=real-time"
    try:
        inputs = validate_run_params(registry.spec_for(slug), request.POST)
        run = _enqueue_run(s, inputs=inputs, launched_by=request.user)
    except RunStartError as exc:
        messages.error(request, exc.message)
        return redirect(back)

    _dispatch_next()
    run.refresh_from_db()
    if run.status == Run.Status.RUNNING:
        messages.success(
            request, f"Run #{run.short_id} started — streaming the live log below."
        )
    else:
        messages.success(
            request,
            f"Run #{run.short_id} queued — it will start automatically when "
            "capacity frees up. Track it on the Batch jobs tab.",
        )
    return redirect(back)


def _extract_bearer_token(request):
    """Read the trigger token from the Authorization: Bearer <token> header only."""
    header = request.META.get("HTTP_AUTHORIZATION", "") or ""
    if header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


def _request_params(request):
    """Return a dict-like of start params from a JSON body or a form-encoded POST.

    The webhook accepts either ``application/json`` or form-encoded params; both
    are normalized to string values so :func:`validate_run_params` can treat them
    uniformly. A malformed or non-object JSON body yields an empty mapping (which
    then falls back to the per-kind scheduled defaults).
    """
    if "application/json" in (request.content_type or ""):
        try:
            data = json.loads((request.body or b"").decode("utf-8") or "{}")
        except (ValueError, TypeError, UnicodeDecodeError):
            return {}
        if isinstance(data, dict):
            return {k: ("" if v is None else str(v)) for k, v in data.items()}
        return {}
    return request.POST


@csrf_exempt
@require_http_methods(["POST"])
def scraper_trigger_view(request, slug):
    """Token-authenticated webhook to launch a run (e.g. from a GitHub Actions cron).

    Auth is a per-scraper bearer token (``Authorization: Bearer <token>``) compared
    in constant time. It is deliberately not login/CSRF protected because the caller
    is an external machine, not a cookie-authenticated browser. The token is never
    logged or echoed back. The same guards as the browser form apply (maintenance,
    single-in-flight run). Documented and managed from the scraper's Schedule tab.
    """
    s = get_object_or_404(Scraper, slug=slug)
    token = _extract_bearer_token(request)
    if (
        not s.trigger_token
        or not token
        or not hmac.compare_digest(token, s.trigger_token)
    ):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)

    try:
        inputs = validate_run_params(
            registry.spec_for(slug), _request_params(request), webhook=True
        )
        run = _enqueue_run(s, inputs=inputs, launched_by=None)
    except RunStartError as exc:
        return JsonResponse(
            {"ok": False, "error": exc.code, "detail": exc.message}, status=exc.status
        )

    _dispatch_next()
    run.refresh_from_db()
    return JsonResponse(
        {
            "ok": True,
            "run_id": run.short_id,
            "run_uuid": str(run.uuid),
            "status": run.status,
            "params": run.params,
            "events_url": request.build_absolute_uri(
                reverse("run_events", args=[s.slug, run.uuid])
            ),
        },
        status=201,
    )


def _trigger_example_json(
    input_kind, defaults, url_required=False, feed_api_key=False, feed_gender=False
):
    """A copy-ready JSON body for the manual ``curl`` example on the Schedule tab."""
    if input_kind == registry.INPUT_YEAR_MONTH:
        return '{"year":"%s","month":"%s"}' % (defaults["year"], defaults["month"])
    if input_kind in (registry.INPUT_DATE_RANGE, registry.INPUT_DATE_RANGE_OR_URL):
        if url_required:
            return '{"tournament_url":"%s"}' % defaults["tournament_url"]
        parts = [
            '"date_from":"%s"' % defaults["date_from"],
            '"date_to":"%s"' % defaults["date_to"],
        ]
        if feed_api_key:
            parts.append('"api_key":"%s"' % defaults["api_key"])
        if feed_gender:
            parts.append('"gender":"%s"' % defaults["gender"])
        return "{%s}" % ",".join(parts)
    if input_kind == registry.INPUT_RANK_SNAPSHOT:
        return '{"snapshot_date":"%s"}' % defaults["snapshot_date"]
    if input_kind == registry.INPUT_NONE:
        return "{}"
    return '{"year":"%s"}' % defaults["year"]


def _github_workflow_yaml(
    *,
    code,
    trigger_url,
    secret_name,
    input_kind,
    defaults,
    url_required=False,
    feed_api_key=False,
    feed_gender=False,
    bi_weekly=False,
):
    """Render the copy-ready GitHub Actions workflow shown on the Schedule tab.

    The ``workflow_dispatch`` inputs, ``env`` block and ``curl`` payload are
    tailored to the scraper's ``input_kind`` (year / year+month / date range).
    Built in Python (not the template) so the GitHub ``${{ ... }}`` expressions
    and JSON braces don't collide with Django's template syntax.
    """
    if input_kind == registry.INPUT_YEAR_MONTH:
        dy, dm = defaults["year"], defaults["month"]
        inputs = (
            f"    inputs:\n"
            f"      year:\n"
            f'        description: "Season year to scrape ({YEAR_MIN}-{YEAR_MAX})"\n'
            f"        required: false\n"
            f'        default: "{dy}"\n'
            f"      month:\n"
            f'        description: "Month 1-12 (0 = whole year)"\n'
            f"        required: false\n"
            f'        default: "{dm}"\n'
        )
        env = (
            f"          YEAR: ${{{{ github.event.inputs.year || '{dy}' }}}}\n"
            f"          MONTH: ${{{{ github.event.inputs.month || '{dm}' }}}}\n"
        )
        data = '{\\"year\\":\\"$YEAR\\",\\"month\\":\\"$MONTH\\"}'
    elif input_kind in (registry.INPUT_DATE_RANGE, registry.INPUT_DATE_RANGE_OR_URL):
        if url_required:
            tu = defaults["tournament_url"]
            inputs = (
                f"    inputs:\n"
                f"      tournament_url:\n"
                f'        description: "Tournament / box-score / Google-Sheet URL"\n'
                f"        required: true\n"
                f'        default: "{tu}"\n'
            )
            env = (
                f"          TOURNAMENT_URL: "
                f"${{{{ github.event.inputs.tournament_url || '{tu}' }}}}\n"
            )
            data = '{\\"tournament_url\\":\\"$TOURNAMENT_URL\\"}'
        else:
            # Leave the date inputs blank by default so a *scheduled* run posts an
            # empty window — the server then computes a fresh trailing window
            # (ending today) on every run, instead of freezing the dates that were
            # baked in when this YAML was generated. Manual dispatch can still type
            # exact dates (fill both together). bi_weekly scrapers default to a
            # shorter rolling window.
            win = BIWEEKLY_DEFAULT_DAYS if bi_weekly else DEFAULT_RANGE_DAYS
            inputs = (
                f"    inputs:\n"
                f"      date_from:\n"
                f'        description: "Start date (YYYY-MM-DD; blank = last {win} days)"\n'
                f"        required: false\n"
                f'        default: ""\n'
                f"      date_to:\n"
                f'        description: "End date (YYYY-MM-DD; blank = today)"\n'
                f"        required: false\n"
                f'        default: ""\n'
            )
            env = (
                f"          DATE_FROM: ${{{{ github.event.inputs.date_from || '' }}}}\n"
                f"          DATE_TO: ${{{{ github.event.inputs.date_to || '' }}}}\n"
            )
            data_parts = [
                '\\"date_from\\":\\"$DATE_FROM\\"',
                '\\"date_to\\":\\"$DATE_TO\\"',
            ]
            if feed_api_key:
                ak = defaults["api_key"]
                inputs += (
                    f"      api_key:\n"
                    f'        description: "Feed API key"\n'
                    f"        required: false\n"
                    f'        default: "{ak}"\n'
                )
                env += (
                    f"          API_KEY: "
                    f"${{{{ github.event.inputs.api_key || '{ak}' }}}}\n"
                )
                data_parts.append('\\"api_key\\":\\"$API_KEY\\"')
            if feed_gender:
                gd = defaults["gender"]
                inputs += (
                    f"      gender:\n"
                    f'        description: "boys, girls, or both"\n'
                    f"        required: false\n"
                    f'        default: "{gd}"\n'
                )
                env += (
                    f"          GENDER: "
                    f"${{{{ github.event.inputs.gender || '{gd}' }}}}\n"
                )
                data_parts.append('\\"gender\\":\\"$GENDER\\"')
            data = "{%s}" % ",".join(data_parts)
    elif input_kind == registry.INPUT_RANK_SNAPSHOT:
        ds = defaults["snapshot_date"]
        inputs = (
            f"    inputs:\n"
            f"      snapshot_date:\n"
            f'        description: "Ranking snapshot date (YYYY-MM-DD)"\n'
            f"        required: false\n"
            f'        default: "{ds}"\n'
        )
        env = (
            f"          SNAPSHOT_DATE: "
            f"${{{{ github.event.inputs.snapshot_date || '{ds}' }}}}\n"
        )
        data = '{\\"snapshot_date\\":\\"$SNAPSHOT_DATE\\"}'
    elif input_kind == registry.INPUT_NONE:
        # No inputs — a manual dispatch posts an empty JSON body.
        inputs = ""
        env = ""
        data = "{}"
    else:  # INPUT_YEAR
        dy = defaults["year"]
        inputs = (
            f"    inputs:\n"
            f"      year:\n"
            f'        description: "Season year to scrape ({YEAR_MIN}-{YEAR_MAX})"\n'
            f"        required: false\n"
            f'        default: "{dy}"\n'
        )
        env = f"          YEAR: ${{{{ github.event.inputs.year || '{dy}' }}}}\n"
        data = '{\\"year\\":\\"$YEAR\\"}'

    header = (
        f"name: MatchMiner — {code} scheduled scrape\n"
        f"\n"
        f"on:\n"
        f"  schedule:\n"
        f"    # 06:00 UTC daily. Edit this cron to change the cadence.\n"
        f'    - cron: "0 6 * * *"\n'
        f"  workflow_dispatch:\n"
    )
    body = (
        f"\n"
        f"jobs:\n"
        f"  trigger:\n"
        f"    runs-on: ubuntu-latest\n"
        f"    steps:\n"
        f"      - name: Start the {code} scrape\n"
        f"        env:\n"
        f'          TRIGGER_URL: "{trigger_url}"\n'
        f"          TRIGGER_TOKEN: ${{{{ secrets.{secret_name} }}}}\n"
        f"{env}"
        f"        run: |\n"
        f'          curl -fsS -X POST "$TRIGGER_URL" \\\n'
        f'            -H "Authorization: Bearer $TRIGGER_TOKEN" \\\n'
        f'            -H "Content-Type: application/json" \\\n'
        f'            --data "{data}"\n'
    )
    return header + inputs + body


def _terminate_run_worker(run, settle=0.2):
    """Force-kill a run's worker process group, then best-effort reap the zombie.

    Returns True when the worker was killed or was already gone, False only when a
    live process could not be signalled (e.g. EPERM / no PID). ``ProcessLookupError``
    counts as success: the worker is already dead. ``settle`` is how long to wait
    after the SIGKILL before reaping — the process needs a beat to die before
    ``waitpid`` can collect it; pass 0 from hot paths that must not block.
    """
    pid = run.pid
    if not pid:
        return False
    killed = True
    if IS_WINDOWS:
        # Windows has no os.killpg / SIGKILL: TerminateProcess the worker (and any
        # children) via taskkill. Exit code 128 == "process not found" == already
        # gone, which counts as success.
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            if result.returncode not in (0, 128):
                killed = False
        except (OSError, subprocess.TimeoutExpired):
            # This runs inside the Stop request, so never block on a wedged
            # taskkill; leave the run for natural finish / stale reaping.
            killed = False
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone — treat as killed
        except PermissionError:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                killed = False
        except OSError:
            killed = False
    if settle:
        time.sleep(settle)
    # Best-effort reap so we don't leave a zombie when this process is the worker's
    # parent (dev runserver / same gunicorn worker). ChildProcessError means it
    # isn't our child — its owner reaps it on its next subprocess launch. Windows
    # has no waitpid for non-children and taskkill already reaped.
    if not IS_WINDOWS:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
    return killed


@login_required
@require_http_methods(["POST"])
def stop_run_view(request, slug, run_uuid):
    """Force-stop an in-flight run: kill its worker PID and mark it STOPPED.

    Shared by the Real-time stop button and the Batch jobs table's inline Stop
    control; the latter posts ``return_tab=batch`` so we redirect back there.
    """
    run = _get_run(slug, run_uuid)
    return_tab = "batch" if request.POST.get("return_tab") == "batch" else "real-time"
    back = f"{reverse('scraper_detail', args=[slug])}?tab={return_tab}"

    if run.status != Run.Status.RUNNING:
        messages.info(request, "That run is no longer in progress.")
        return redirect(back)

    # SIGKILL + reap the worker before we touch the row, so its own final save
    # can't race past our STOPPED write.
    killed = _terminate_run_worker(run)
    run.refresh_from_db()

    if run.status != Run.Status.RUNNING:
        # The worker finalised itself in the meantime (rare race) — keep its result.
        messages.info(request, f"Run #{run.short_id} had already finished.")
        return redirect(back)

    if not killed:
        # Couldn't signal a live worker (no PID / EPERM). Don't fake a STOPPED state
        # — the worker is still going and would overwrite us. Let it finish or the
        # stale-run reaper clean it up.
        messages.error(
            request,
            "Couldn't stop the run — the worker didn't respond. It will be "
            "cleaned up automatically if it gets stuck.",
        )
        return redirect(back)

    run.status = Run.Status.STOPPED
    run.finished_at = timezone.now()
    if run.started_at:
        run.duration_ms = int(
            (run.finished_at - run.started_at).total_seconds() * 1000
        )
    lines = list(run.log_lines.order_by("seq").values_list("text", flat=True))
    lines.append("[stopped] Run force-stopped by user — worker process killed.")
    run.log_text = "\n".join(lines) + "\n"
    run.save(update_fields=["status", "finished_at", "duration_ms", "log_text"])
    # Stopping freed capacity (a thread slot, or the browser-exclusivity lock) —
    # promote whatever the queue can now start.
    _dispatch_next()
    messages.success(request, f"Run #{run.short_id} stopped.")
    return redirect(back)


def _get_run(slug, run_uuid):
    return get_object_or_404(Run, uuid=run_uuid, scraper__slug=slug)


def _run_lines(run):
    """Log lines for the viewer: the materialised snapshot, else the live rows."""
    if run.log_text:
        return run.log_text.splitlines()
    return list(run.log_lines.order_by("seq").values_list("text", flat=True))


def _run_log_text(run):
    if run.log_text:
        return run.log_text
    joined = "\n".join(run.log_lines.order_by("seq").values_list("text", flat=True))
    return joined + "\n" if joined else ""


@login_required
def run_events_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    # If the worker died, the poller would otherwise stream forever — reap here
    # so the live console terminates without needing a page reload.
    if run.status == Run.Status.RUNNING:
        _reap_stale_runs(run.scraper)
        run.refresh_from_db()
    elif run.status == Run.Status.QUEUED:
        # Pump the queue while a waiting run is being watched, so it promotes
        # itself the moment capacity frees up — no page reload needed.
        _dispatch_next(blocking=False)
        run.refresh_from_db()
    try:
        after = int(request.GET.get("after", "0"))
    except (TypeError, ValueError):
        after = 0
    lines = list(
        run.log_lines.filter(seq__gt=after)
        .order_by("seq")
        .values("seq", "level", "text")
    )
    return JsonResponse(
        {
            "status": run.status,
            "status_display": run.get_status_display(),
            "queued": run.status == Run.Status.QUEUED,
            "done": run.status not in (Run.Status.RUNNING, Run.Status.QUEUED),
            "lines": lines,
            "row_count": run.row_count,
            "progress_done": run.progress_done,
            "progress_total": run.progress_total,
            "progress_percent": run.progress_percent,
            "eta_label": run.eta_label,
            "size_label": run.size_label,
            "duration_label": run.duration_label,
            "has_csv": run.has_csv,
            "has_requests": run.has_requests,
            "has_errors": run.has_errors,
        }
    )


@login_required
@require_http_methods(["GET"])
def queue_events_view(request, slug):
    """JSON poll for the Batch-jobs tab.

    Pumps the dispatcher (so queued jobs promote themselves while the tab is
    open), then reports:

    - ``running_short_id`` / ``running_uuid`` — this scraper's live job, for the
      header badge,
    - ``queued_uuids`` — the ordered list of this scraper's still-queued jobs, so
      the page can detect a structural change (a new job, a promotion, a cancel)
      and reload to re-render the table,
    - ``jobs`` — a per-uuid state map for the rows currently shown on the page
      (uuids passed via ``?ids=…``), so progress bars / statuses / log links
      update live without a reload.
    """
    s = get_object_or_404(Scraper, slug=slug)
    _dispatch_next(blocking=False)

    running = (
        s.runs.filter(status=Run.Status.RUNNING).order_by("-started_at").first()
    )
    queued_uuids = list(
        s.runs.filter(status=Run.Status.QUEUED)
        .order_by("created_at")
        .values_list("uuid", flat=True)
    )

    ids = []
    raw = (request.GET.get("ids") or "").strip()
    if raw:
        for tok in raw.split(",")[:JOBS_PER_PAGE]:
            try:
                ids.append(uuid.UUID(tok.strip()))
            except (ValueError, AttributeError):
                continue
    jobs = {}
    if ids:
        for r in s.runs.filter(uuid__in=ids):
            jobs[str(r.uuid)] = {
                "status": r.status,
                "status_display": r.get_status_display(),
                "state": _job_state(r),
                "row_count": r.row_count,
                "progress_done": r.progress_done,
                "progress_total": r.progress_total,
                "progress_percent": r.progress_percent,
                "duration_label": r.duration_label,
                "size_label": r.size_label,
                "has_csv": r.has_csv,
                "has_requests": r.has_requests,
                "has_errors": r.has_errors,
                "can_cancel": r.status == Run.Status.QUEUED,
                "done": r.status not in (Run.Status.RUNNING, Run.Status.QUEUED),
            }

    return JsonResponse(
        {
            "running_short_id": running.short_id if running else None,
            "running_uuid": str(running.uuid) if running else None,
            "queued_uuids": [str(u) for u in queued_uuids],
            "jobs": jobs,
        }
    )


@login_required
@require_http_methods(["POST"])
def run_cancel_view(request, slug, run_uuid):
    """Cancel a still-QUEUED job (Batch tab). Only waiting jobs are cancellable —
    a running job is ended via Stop run, a finished one is immutable."""
    run = _get_run(slug, run_uuid)
    back = f"{reverse('scraper_detail', args=[slug])}?tab=batch"
    # Atomic conditional cancel: the transition fires in a single UPDATE guarded
    # by ``status=QUEUED`` in the WHERE clause, so it can never stomp a job the
    # dispatcher promoted to RUNNING in the same instant (which would orphan a
    # live worker as "stopped"). If the dispatcher won the race, 0 rows match and
    # we report the job as no-longer-cancellable.
    cancelled = Run.objects.filter(
        uuid=run.uuid, status=Run.Status.QUEUED
    ).update(
        status=Run.Status.STOPPED,
        finished_at=timezone.now(),
        log_text="Job cancelled while queued — it never started.\n",
    )
    if not cancelled:
        messages.error(
            request,
            "That job is no longer queued — only waiting jobs can be cancelled.",
        )
        return redirect(back)
    messages.success(request, f"Queued job #{run.short_id} cancelled.")
    return redirect(back)


@login_required
@require_http_methods(["POST"])
def scraper_queue_view(request, slug):
    """Batch-tab submit: validate inputs, enqueue a job, then pump the dispatcher
    (it starts immediately when capacity allows, otherwise waits in the queue)."""
    s = get_object_or_404(Scraper, slug=slug)
    back = f"{reverse('scraper_detail', args=[slug])}?tab=batch"
    try:
        inputs = validate_run_params(registry.spec_for(slug), request.POST)
        run = _enqueue_run(s, inputs=inputs, launched_by=request.user)
    except RunStartError as exc:
        messages.error(request, exc.message)
        return redirect(back)

    _dispatch_next()
    run.refresh_from_db()
    if run.status == Run.Status.RUNNING:
        messages.success(request, f"Job #{run.short_id} started immediately.")
    else:
        messages.success(
            request,
            f"Job #{run.short_id} queued — it will start automatically when "
            "capacity frees up.",
        )
    return redirect(back)


@login_required
def run_log_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    lines = _run_lines(run)
    paginator = Paginator(lines, LOG_LINES_PER_PAGE)
    page_obj = paginator.get_page(request.GET.get("page"))
    ctx = _app_ctx(
        "scrapers",
        s=run.scraper,
        run=run,
        page_obj=page_obj,
        line_total=len(lines),
    )
    return render(request, "run_log.html", ctx)


def _download_filename(slug, run, kind):
    """Standard download name shared by every scraper:

    ``<scraper slug>__<UTC start timestamp>_<kind>.csv`` where ``kind`` is one of
    ``log`` / ``item`` / ``request`` / ``error``. Keeping one convention across
    all scrapers makes the files predictable and chronologically sortable.
    """
    ts = run.started_at
    if timezone.is_naive(ts):
        stamp = ts.strftime("%Y%m%d-%H%M%S")
    else:
        stamp = ts.astimezone(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{slug}__{stamp}_{kind}.csv"


# Large runs (e.g. the queue-driven south_africa job — 230k rows / 100 MB+)
# produce CSV/log payloads too big for a plain ``HttpResponse``: that buffers
# the whole body in memory twice (str + encoded bytes) and the WSGI server
# can't start sending until it's fully built, so over a remote/networked DB the
# download reliably times out. Streaming the column in fixed-size chunks starts
# the transfer immediately (keeping the WSGI channel active under its inactivity
# timeout) and avoids the double buffering. The column is still fetched up front
# inside the view (so the DB read is covered by DBReconnectMiddleware's retry);
# the generator then only hands an already in-memory string out in pieces.
_DOWNLOAD_CHUNK = 256 * 1024  # 256 KB text slices


def _iter_text_chunks(text, size=_DOWNLOAD_CHUNK):
    if not text:
        return
    for i in range(0, len(text), size):
        # Slicing a str is codepoint-safe, so each slice encodes cleanly.
        yield text[i : i + size].encode("utf-8")


def _run_for_download(slug, run_uuid, *fields):
    """Fetch a run loading only ``started_at`` (for the filename) plus the
    requested payload column(s) — never all four giant TextFields at once."""
    return get_object_or_404(
        Run.objects.only("started_at", *fields),
        uuid=run_uuid,
        scraper__slug=slug,
    )


def _stream_text_download(slug, run, body, kind, content_type):
    resp = StreamingHttpResponse(
        _iter_text_chunks(body), content_type=content_type
    )
    resp["Content-Disposition"] = (
        f'attachment; filename="{_download_filename(slug, run, kind)}"'
    )
    return resp


@login_required
def run_log_download_view(request, slug, run_uuid):
    run = _run_for_download(slug, run_uuid, "log_text")
    return _stream_text_download(
        slug, run, _run_log_text(run), "log", "text/plain; charset=utf-8"
    )


@login_required
def run_csv_download_view(request, slug, run_uuid):
    run = _run_for_download(slug, run_uuid, "csv_data")
    return _stream_text_download(
        slug, run, run.csv_data or "", "item", "text/csv; charset=utf-8"
    )


@login_required
def run_requests_download_view(request, slug, run_uuid):
    run = _run_for_download(slug, run_uuid, "requests_csv")
    return _stream_text_download(
        slug, run, run.requests_csv or "", "request", "text/csv; charset=utf-8"
    )


@login_required
def run_errors_download_view(request, slug, run_uuid):
    run = _run_for_download(slug, run_uuid, "errors_csv")
    return _stream_text_download(
        slug, run, run.errors_csv or "", "error", "text/csv; charset=utf-8"
    )


@login_required
def college_matches_export_view(request, slug):
    """Download the match database as CSV — all rows, or only those whose match
    date falls within an optional ``from``/``to`` window (both inclusive).

    Only available for scrapers whose runner persists matches (the
    ``has_match_store`` flag). Streams matching :class:`CollegeMatch.data` records
    in insertion order through :func:`college_store.to_csv`. ``date_norm`` holds
    the normalized ISO (``YYYY-MM-DD``) match date, so a lexicographic range
    filter on it is also chronological; a blank/invalid bound is simply ignored
    (open-ended on that side).
    """
    get_object_or_404(Scraper, slug=slug)
    if not registry.spec_for(slug).has_match_store:
        raise Http404("This scraper has no match database.")

    def _bound(key):
        raw = (request.GET.get(key) or "").strip()
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None

    date_from = _bound("from")
    date_to = _bound("to")

    qs = CollegeMatch.objects.all()
    if date_from:
        qs = qs.filter(date_norm__gte=date_from.isoformat())
    if date_to:
        qs = qs.filter(date_norm__lte=date_to.isoformat())
    rows = list(qs.order_by("created_at").values_list("data", flat=True))

    if date_from or date_to:
        lo = date_from.isoformat() if date_from else "start"
        hi = date_to.isoformat() if date_to else "end"
        filename = f"{slug}_matches_{lo}_to_{hi}.csv"
    else:
        filename = f"{slug}_all_matches.csv"

    resp = StreamingHttpResponse(
        _iter_text_chunks(college_store.to_csv(rows)),
        content_type="text/csv; charset=utf-8",
    )
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@login_required
@require_http_methods(["POST"])
def run_delete_view(request, slug, run_uuid):
    """Delete a single run from Calls history (its log lines + CSVs go with it)."""
    run = _get_run(slug, run_uuid)
    back = f"{reverse('scraper_detail', args=[slug])}?tab=calls"
    page = (request.POST.get("page") or "").strip()
    if page.isdigit():
        back = f"{back}&page={page}"

    if run.is_running:
        messages.error(request, "Can't delete a run while it's still in progress.")
        return redirect(back)

    short = run.short_id
    run.delete()
    messages.success(request, f"Run #{short} and its files were deleted.")
    return redirect(back)


@login_required
@require_http_methods(["POST"])
def runs_bulk_delete_view(request, slug):
    """Bulk-delete the selected runs (their log lines + CSVs go with them)."""
    s = get_object_or_404(Scraper, slug=slug)
    back = f"{reverse('scraper_detail', args=[slug])}?tab=calls"
    page = (request.POST.get("page") or "").strip()
    if page.isdigit():
        back = f"{back}&page={page}"

    valid = []
    for raw in request.POST.getlist("run_uuids"):
        try:
            valid.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError, AttributeError):
            continue

    if not valid:
        messages.error(request, "Select at least one run to delete.")
        return redirect(back)

    qs = Run.objects.filter(scraper=s, uuid__in=valid).exclude(
        status=Run.Status.RUNNING
    )
    deleted = qs.count()
    qs.delete()
    if deleted:
        messages.success(
            request,
            f"Deleted {deleted} run{'' if deleted == 1 else 's'} and their files.",
        )
    else:
        messages.error(
            request, "Nothing was deleted — the selected runs may be in progress."
        )
    return redirect(back)


def _placeholder(active_nav, kicker, title, sub, empty_title, empty_sub, superuser_only=False):
    @login_required
    def view(request):
        if superuser_only and not request.user.is_superuser:
            messages.error(request, "Only administrators can access that page.")
            return redirect("overview")
        return render(
            request,
            "_placeholder.html",
            _app_ctx(
                active_nav,
                page_kicker=kicker,
                page_title=title,
                page_sub=sub,
                empty_title=empty_title,
                empty_sub=empty_sub,
            ),
        )

    return view


apis_view = _placeholder(
    "apis",
    "Workspace",
    "APIs",
    "Public endpoints that expose your mined tennis data to downstream apps.",
    "No APIs published",
    "Published API endpoints will be listed here.",
)
apis_logs_view = _placeholder(
    "apis_logs",
    "Workspace",
    "PDFs & logs",
    "Generated exports and run logs collected across all sources.",
    "Nothing logged yet",
    "Run logs and exported files will collect here.",
)
requirements_view = _placeholder(
    "requirements",
    "Portals",
    "Requirements",
    "Field coverage and data requirements tracked for each source.",
    "No requirements yet",
    "Source requirements will be tracked here.",
)
companies_view = _placeholder(
    "companies",
    "Portals",
    "Companies",
    "Organizations and partners connected to your workspace.",
    "No companies yet",
    "Connected companies will appear here.",
)
@login_required
@require_http_methods(["GET", "POST"])
def settings_view(request):
    User = get_user_model()
    if not request.user.is_superuser:
        messages.error(request, "Only administrators can access that page.")
        return redirect("overview")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "change_password":
            user_id = (request.POST.get("user_id") or "").strip()
            password = request.POST.get("password") or ""
            confirm = request.POST.get("password_confirm") or ""
            if not user_id.isdigit():
                messages.error(request, "Invalid user.")
                return redirect("settings")
            target = User.objects.filter(pk=int(user_id)).first()
            if target is None:
                messages.error(request, "That user no longer exists.")
                return redirect("settings")
            if not password:
                messages.error(request, "Enter a new password.")
                return redirect("settings")
            if password != confirm:
                messages.error(request, "The passwords don't match.")
                return redirect("settings")
            try:
                validate_password(password, target)
            except ValidationError as exc:
                messages.error(request, " ".join(exc.messages))
                return redirect("settings")
            target.set_password(password)
            target.save()
            if target.pk == request.user.pk:
                update_session_auth_hash(request, target)
            messages.success(request, f"Password updated for “{target.username}”.")
            return redirect("settings")

        if action == "add_proxy":
            name = (request.POST.get("name") or "").strip()
            kind = request.POST.get("kind", Proxy.Kind.RESIDENTIAL)
            address = (request.POST.get("address") or "").strip()
            if not name:
                messages.error(request, "Give the proxy a name.")
            elif kind not in Proxy.Kind.values:
                messages.error(request, "Pick a valid proxy type.")
            else:
                Proxy.objects.create(name=name, kind=kind, address=address)
                messages.success(request, f"Added proxy “{name}”.")
            return redirect("settings")

        if action == "delete_proxy":
            proxy_id = (request.POST.get("proxy_id") or "").strip()
            if proxy_id.isdigit():
                Proxy.objects.filter(pk=int(proxy_id)).delete()
                messages.success(request, "Proxy removed.")
            return redirect("settings")

        if action == "save_anthropic_key":
            # The key is a secret: it is stored as-is but never logged or echoed
            # back to the page (only a masked form is ever rendered).
            key = (request.POST.get("anthropic_api_key") or "").strip()
            if not key:
                messages.error(
                    request,
                    "Enter an Anthropic API key, or use “Clear stored key” to remove it.",
                )
                return redirect("settings")
            cfg = GeneralConfig.get_solo()
            cfg.anthropic_api_key = key
            cfg.save()
            messages.success(request, "Anthropic API key saved.")
            return redirect("settings")

        if action == "clear_anthropic_key":
            cfg = GeneralConfig.get_solo()
            cfg.anthropic_api_key = ""
            cfg.save()
            messages.success(
                request,
                "Anthropic API key cleared — the AI scrapers fall back to the "
                "server environment key if one is set.",
            )
            return redirect("settings")

        messages.error(request, "Unknown action.")
        return redirect("settings")

    users = User.objects.order_by("-is_active", "-is_superuser", "username")
    proxies = Proxy.objects.annotate(used_by=Count("scrapers")).order_by("name")
    by_kind = {k.value: 0 for k in Proxy.Kind}
    for p in proxies:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
    cfg = GeneralConfig.get_solo()
    env_keys = [k for k in (getattr(settings, "CLAUDE_KEYS", []) or []) if k]
    ctx = _app_ctx(
        "settings",
        users_list=users,
        total_users=users.count(),
        proxies=proxies,
        proxy_kinds=Proxy.Kind.choices,
        total_proxies=len(proxies),
        by_kind=by_kind,
        anthropic_masked=cfg.masked_anthropic_key,
        anthropic_configured=bool(cfg.masked_anthropic_key),
        anthropic_env_present=bool(env_keys),
    )
    return render(request, "settings.html", ctx)


def _active_superuser_count(exclude_pk=None):
    User = get_user_model()
    qs = User.objects.filter(is_active=True, is_superuser=True)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.count()


@login_required
@require_http_methods(["GET", "POST"])
def users_view(request):
    User = get_user_model()
    if not request.user.is_superuser:
        messages.error(request, "Only administrators can manage users.")
        return redirect("overview")

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "add":
            username = (request.POST.get("username") or "").strip()
            email = (request.POST.get("email") or "").strip()
            password = request.POST.get("password") or ""
            is_super = (request.POST.get("role") or "member") == "admin"
            if not username:
                messages.error(request, "Username is required.")
            elif User.objects.filter(username__iexact=username).exists():
                messages.error(request, f"A user named “{username}” already exists.")
            else:
                candidate = User(
                    username=username,
                    email=email,
                    is_superuser=is_super,
                    is_staff=is_super,
                    is_active=True,
                )
                try:
                    validate_password(password, candidate)
                except ValidationError as exc:
                    messages.error(request, " ".join(exc.messages))
                else:
                    candidate.set_password(password)
                    candidate.save()
                    messages.success(request, f"Added user “{username}”.")
            return redirect("users")

        user_id = (request.POST.get("user_id") or "").strip()
        if not user_id.isdigit():
            messages.error(request, "Invalid user.")
            return redirect("users")
        target = User.objects.filter(pk=int(user_id)).first()
        if target is None:
            messages.error(request, "That user no longer exists.")
            return redirect("users")
        is_self = target.pk == request.user.pk

        if action == "edit":
            email = (request.POST.get("email") or "").strip()
            password = request.POST.get("password") or ""
            new_super = (request.POST.get("role") or "member") == "admin"
            losing_admin = target.is_superuser and not new_super
            if losing_admin and is_self:
                messages.error(request, "You can't remove your own admin role.")
                return redirect("users")
            if (
                losing_admin
                and target.is_active
                and _active_superuser_count(exclude_pk=target.pk) == 0
            ):
                messages.error(request, "At least one active admin must remain.")
                return redirect("users")
            if password:
                try:
                    validate_password(password, target)
                except ValidationError as exc:
                    messages.error(request, " ".join(exc.messages))
                    return redirect("users")
                target.set_password(password)
            target.email = email
            target.is_superuser = new_super
            target.is_staff = new_super
            target.save()
            messages.success(request, f"Updated “{target.username}”.")
            return redirect("users")

        if action in ("activate", "deactivate"):
            if action == "deactivate":
                if is_self:
                    messages.error(request, "You can't deactivate your own account.")
                    return redirect("users")
                if (
                    target.is_superuser
                    and _active_superuser_count(exclude_pk=target.pk) == 0
                ):
                    messages.error(request, "At least one active admin must remain.")
                    return redirect("users")
                target.is_active = False
                target.save(update_fields=["is_active"])
                messages.success(request, f"Deactivated “{target.username}”.")
            else:
                target.is_active = True
                target.save(update_fields=["is_active"])
                messages.success(request, f"Reactivated “{target.username}”.")
            return redirect("users")

        if action == "delete":
            if is_self:
                messages.error(request, "You can't delete your own account.")
                return redirect("users")
            if (
                target.is_superuser
                and target.is_active
                and _active_superuser_count(exclude_pk=target.pk) == 0
            ):
                messages.error(request, "At least one active admin must remain.")
                return redirect("users")
            name = target.username
            target.delete()
            messages.success(request, f"Deleted “{name}”.")
            return redirect("users")

        messages.error(request, "Unknown action.")
        return redirect("users")

    users = User.objects.order_by("-is_active", "-is_superuser", "username")
    total = users.count()
    admins = sum(1 for u in users if u.is_superuser)
    active = sum(1 for u in users if u.is_active)
    ctx = _app_ctx(
        "users",
        users_list=users,
        total_users=total,
        admin_count=admins,
        active_count=active,
    )
    return render(request, "users.html", ctx)


@login_required
@require_http_methods(["GET", "POST"])
def proxies_view(request):
    """The standalone Proxies page was merged into the Settings page. Keep this
    route as a permanent redirect so old links/bookmarks still resolve."""
    return redirect("settings")


@require_http_methods(["POST"])
def logout_view(request):
    logout(request)
    return redirect("login")
