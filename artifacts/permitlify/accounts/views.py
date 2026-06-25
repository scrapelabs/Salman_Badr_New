import hmac
import ipaddress
import json
import os
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
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Exists, Max, OuterRef, Subquery
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .live_scrapers import _ssrf, registry
from .models import Proxy, Run, Scraper, Ticket
from .system_stats import collect_system_stats, gauge_card
from .runs import ALL_TOURNAMENTS

STALE_RUNNING_AFTER = timezone.timedelta(minutes=20)
YEAR_MIN = 2000
YEAR_MAX = 2030
IS_WINDOWS = os.name == "nt"

# Date-range run inputs (date_range / date_range_or_url scrapers).
DEFAULT_RANGE_DAYS = 30   # webhook window when a scheduled call omits dates
MAX_RANGE_DAYS = 400      # reject absurd windows
MAX_URL_LEN = 2048
MONTHS = [
    (1, "January"), (2, "February"), (3, "March"), (4, "April"),
    (5, "May"), (6, "June"), (7, "July"), (8, "August"),
    (9, "September"), (10, "October"), (11, "November"), (12, "December"),
]

TAB_LABELS = {
    "real-time": "Real-time test",
    "calls": "Calls history",
    "schedule": "Schedule",
    "settings": "Settings",
    "status": "Status",
}

CALLS_PER_PAGE = 12
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
    if run.status == Run.Status.FAILED:
        return "failed"
    if run.status == Run.Status.STOPPED:
        return "stopped"
    return "healthy"  # success / partial


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
        Run.objects.exclude(status=Run.Status.RUNNING)
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
        "detail_url": reverse("scraper_detail", args=[run.scraper.slug]),
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


def _reap_stale_runs(scraper):
    """Fail runs stuck in RUNNING past the max duration (e.g. the worker died).

    Also force-kills any surviving worker process group so a stuck worker can't
    keep running — or resurface its status — after we release the RUNNING lock.
    """
    cutoff = timezone.now() - STALE_RUNNING_AFTER
    for run in scraper.runs.filter(status=Run.Status.RUNNING, started_at__lt=cutoff):
        _terminate_run_worker(run, settle=0)
        run.status = Run.Status.FAILED
        run.finished_at = timezone.now()
        run.duration_ms = run.duration_ms or int(
            STALE_RUNNING_AFTER.total_seconds() * 1000
        )
        if not run.log_text:
            lines = list(run.log_lines.order_by("seq").values_list("text", flat=True))
            lines.append(
                "[reaper] Run exceeded the maximum duration and was marked failed."
            )
            run.log_text = "\n".join(lines) + "\n"
        run.save(update_fields=["status", "finished_at", "duration_ms", "log_text"])


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
        runs_today=Run.objects.filter(started_at__date=today).count(),
        maint_count=Scraper.objects.filter(mode=Scraper.Mode.MAINTENANCE).count(),
        monitor=_monitor_cards(collect_system_stats()),
        threads_running=threads_running,
        running_scrapers=running_scrapers,
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
                "runs_today": Run.objects.filter(started_at__date=today).count(),
                "maint_count": Scraper.objects.filter(
                    mode=Scraper.Mode.MAINTENANCE
                ).count(),
                "recent_runs": [_run_brief(r) for r in _recent_runs()],
            },
            "scrapers": scr_map,
        }
    )


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

            update_fields = ["proxy", "threads", "updated_at"]
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

            s.save(update_fields=update_fields)
            messages.success(request, "Scraper settings saved.")
            return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=settings")

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

    tab = request.GET.get("tab", "real-time")
    if tab not in TAB_LABELS:
        tab = "real-time"
    # The Settings (routing & performance) tab is admin-only.
    if tab == "settings" and not request.user.is_superuser:
        return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=real-time")

    ctx = _app_ctx("scrapers", s=s, tab=tab, tab_label=TAB_LABELS[tab])

    if tab == "real-time":
        _reap_stale_runs(s)
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
        current_year = timezone.localdate().year
        today = timezone.localdate()
        ctx["input_kind"] = spec.input_kind
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
    elif tab == "calls":
        paginator = Paginator(s.runs.all(), CALLS_PER_PAGE)
        ctx["page_obj"] = paginator.get_page(request.GET.get("page"))
        ctx["run_total"] = paginator.count
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
            spec.input_kind, sched_defaults, url_required=spec.url_required
        )
        ctx["workflow_filename"] = f"{s.slug}-schedule.yml"
        ctx["workflow_yaml"] = _github_workflow_yaml(
            code=s.code,
            trigger_url=trigger_url,
            secret_name=secret_name,
            input_kind=spec.input_kind,
            defaults=sched_defaults,
            url_required=spec.url_required,
        )
    elif tab == "settings":
        ctx["proxies"] = Proxy.objects.filter(is_active=True).order_by("name")
        ctx["thread_min"] = Scraper.THREADS_MIN
        ctx["thread_max"] = Scraper.THREADS_MAX
        ctx["needs_claude"] = registry.spec_for(slug).needs_claude
        ctx["needs_login"] = registry.spec_for(slug).needs_login
        ctx["login_label"] = registry.spec_for(slug).login_label

    return render(request, "scraper_detail.html", ctx)


class RunStartError(Exception):
    """A run could not be started; carries a machine code, message, and HTTP status.

    Lets the browser form (which renders a flash message) and the trigger webhook
    (which returns JSON + status) share one validation/launch path.
    """

    def __init__(self, code, message, status):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


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


def validate_run_params(spec, data, *, webhook=False):
    """Validate the start inputs for ``spec`` from a dict-like ``data``.

    Returns a :class:`RunInputs`. Shared by the browser start form (``data`` =
    ``request.POST``) and the trigger webhook (``data`` from JSON/POST). When
    ``webhook`` is set, missing inputs fall back to sensible scheduled defaults
    (current year/month, or a trailing window) instead of raising.
    """
    kind = spec.input_kind
    get = data.get

    if kind == registry.INPUT_YEAR_MONTH:
        year = _parse_year(get("year"), default_current=webhook)
        month = _parse_month(get("month"), default_all=webhook)
        if month:
            return RunInputs(
                params={"year": year, "month": month},
                date_from=date(year, month, 1),
                date_to=_month_end(year, month),
                tournament=f"{year}-{month:02d}",
            )
        return RunInputs(
            params={"year": year, "month": 0},
            date_from=date(year, 1, 1),
            date_to=date(year, 12, 31),
            tournament=f"{year} · all months",
        )

    if kind == registry.INPUT_RANK_SNAPSHOT:
        if webhook and not (get("snapshot_date") or "").strip():
            snap = timezone.localdate()
        else:
            snap = _parse_iso_date(get("snapshot_date"), "snapshot")
        return RunInputs(
            params={"single_date": snap.isoformat()},
            date_from=snap,
            date_to=snap,
            tournament=f"ranking @ {snap.isoformat()}",
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
        if (
            webhook
            and not (get("date_from") or "").strip()
            and not (get("date_to") or "").strip()
        ):
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
        return RunInputs(
            params={"date_from": start.isoformat(), "date_to": end.isoformat()},
            date_from=start,
            date_to=end,
            tournament=f"{start.isoformat()} → {end.isoformat()}",
        )

    # Default / INPUT_YEAR.
    year = _parse_year(get("year"), default_current=webhook)
    return RunInputs(
        params={"year": year},
        date_from=date(year, 1, 1),
        date_to=date(year, 12, 31),
        tournament=ALL_TOURNAMENTS,
    )


def _start_scraper_run(scraper, *, inputs, launched_by):
    """Apply the run guards and launch the worker, returning the new Run.

    Shared by the real-time browser form and the GitHub-Actions trigger webhook so
    both honour maintenance, stale-run reaping, the single-in-flight-run rule, and
    launch-failure handling identically. ``inputs`` is a validated
    :class:`RunInputs`. Raises RunStartError on any guard failure.
    """
    if scraper.is_maintenance:
        raise RunStartError(
            "maintenance", "This source is in maintenance — runs are blocked.", 503
        )
    _reap_stale_runs(scraper)
    if scraper.runs.filter(status=Run.Status.RUNNING).exists():
        raise RunStartError(
            "already_running", "A run is already in progress for this source.", 409
        )
    try:
        run = Run.objects.create(
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
    try:
        _launch_run(run)
    except Exception:  # noqa: BLE001
        run.status = Run.Status.FAILED
        run.finished_at = timezone.now()
        run.log_text = "Failed to launch the scraper process.\n"
        run.save(update_fields=["status", "finished_at", "log_text"])
        raise RunStartError(
            "launch_failed", "Could not start the run. Please try again.", 503
        )
    return run


@login_required
@require_http_methods(["POST"])
def scraper_run_view(request, slug):
    s = get_object_or_404(Scraper, slug=slug)
    back = f"{reverse('scraper_detail', args=[slug])}?tab=real-time"
    try:
        inputs = validate_run_params(registry.spec_for(slug), request.POST)
        run = _start_scraper_run(s, inputs=inputs, launched_by=request.user)
    except RunStartError as exc:
        messages.error(request, exc.message)
        return redirect(back)

    messages.success(
        request, f"Run #{run.short_id} started — streaming the live log below."
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
        run = _start_scraper_run(s, inputs=inputs, launched_by=None)
    except RunStartError as exc:
        return JsonResponse(
            {"ok": False, "error": exc.code, "detail": exc.message}, status=exc.status
        )

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


def _trigger_example_json(input_kind, defaults, url_required=False):
    """A copy-ready JSON body for the manual ``curl`` example on the Schedule tab."""
    if input_kind == registry.INPUT_YEAR_MONTH:
        return '{"year":"%s","month":"%s"}' % (defaults["year"], defaults["month"])
    if input_kind in (registry.INPUT_DATE_RANGE, registry.INPUT_DATE_RANGE_OR_URL):
        if url_required:
            return '{"tournament_url":"%s"}' % defaults["tournament_url"]
        return '{"date_from":"%s","date_to":"%s"}' % (
            defaults["date_from"],
            defaults["date_to"],
        )
    if input_kind == registry.INPUT_RANK_SNAPSHOT:
        return '{"snapshot_date":"%s"}' % defaults["snapshot_date"]
    return '{"year":"%s"}' % defaults["year"]


def _github_workflow_yaml(
    *, code, trigger_url, secret_name, input_kind, defaults, url_required=False
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
            df, dt = defaults["date_from"], defaults["date_to"]
            inputs = (
                f"    inputs:\n"
                f"      date_from:\n"
                f'        description: "Start date (YYYY-MM-DD)"\n'
                f"        required: false\n"
                f'        default: "{df}"\n'
                f"      date_to:\n"
                f'        description: "End date (YYYY-MM-DD)"\n'
                f"        required: false\n"
                f'        default: "{dt}"\n'
            )
            env = (
                f"          DATE_FROM: ${{{{ github.event.inputs.date_from || '{df}' }}}}\n"
                f"          DATE_TO: ${{{{ github.event.inputs.date_to || '{dt}' }}}}\n"
            )
            data = '{\\"date_from\\":\\"$DATE_FROM\\",\\"date_to\\":\\"$DATE_TO\\"}'
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
    """Force-stop an in-flight run: kill its worker PID and mark it STOPPED."""
    run = _get_run(slug, run_uuid)
    back = f"{reverse('scraper_detail', args=[slug])}?tab=real-time"

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
            "done": run.status != Run.Status.RUNNING,
            "lines": lines,
            "row_count": run.row_count,
            "progress_done": run.progress_done,
            "progress_total": run.progress_total,
            "progress_percent": run.progress_percent,
            "size_label": run.size_label,
            "duration_label": run.duration_label,
            "has_csv": run.has_csv,
            "has_requests": run.has_requests,
            "has_errors": run.has_errors,
        }
    )


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


@login_required
def run_log_download_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    resp = HttpResponse(_run_log_text(run), content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = (
        f'attachment; filename="{_download_filename(slug, run, "log")}"'
    )
    return resp


@login_required
def run_csv_download_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    body = run.csv_data or ""
    resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = (
        f'attachment; filename="{_download_filename(slug, run, "item")}"'
    )
    return resp


@login_required
def run_requests_download_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    body = run.requests_csv or ""
    resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = (
        f'attachment; filename="{_download_filename(slug, run, "request")}"'
    )
    return resp


@login_required
def run_errors_download_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    body = run.errors_csv or ""
    resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = (
        f'attachment; filename="{_download_filename(slug, run, "error")}"'
    )
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
settings_view = _placeholder(
    "settings",
    "System",
    "Settings",
    "Workspace configuration, API keys and preferences.",
    "Settings coming soon",
    "Workspace settings will live here.",
    superuser_only=True,
)
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
    if not request.user.is_superuser:
        messages.error(request, "Only administrators can manage proxies.")
        return redirect("overview")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "delete":
            proxy_id = (request.POST.get("proxy_id") or "").strip()
            if proxy_id.isdigit():
                Proxy.objects.filter(pk=int(proxy_id)).delete()
                messages.success(request, "Proxy removed.")
            return redirect("proxies")

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
        return redirect("proxies")

    proxies = Proxy.objects.annotate(used_by=Count("scrapers")).order_by("name")
    by_kind = {k.value: 0 for k in Proxy.Kind}
    for p in proxies:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
    ctx = _app_ctx(
        "proxies",
        proxies=proxies,
        proxy_kinds=Proxy.Kind.choices,
        total_proxies=len(proxies),
        by_kind=by_kind,
    )
    return render(request, "proxies.html", ctx)


@require_http_methods(["POST"])
def logout_view(request):
    logout(request)
    return redirect("login")
