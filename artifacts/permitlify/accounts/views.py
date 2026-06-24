import hmac
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import date, timezone as dt_timezone

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Max
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Proxy, Run, Scraper
from .runs import ALL_TOURNAMENTS

STALE_RUNNING_AFTER = timezone.timedelta(minutes=20)
YEAR_MIN = 2000
YEAR_MAX = 2030

TAB_LABELS = {
    "real-time": "Real-time test",
    "calls": "Calls history",
    "schedule": "Schedule",
    "settings": "Settings",
    "status": "Status",
}

CALLS_PER_PAGE = 12
LOG_LINES_PER_PAGE = 150


def _counts():
    return {
        "scrapers": Scraper.objects.count(),
        "proxies": Proxy.objects.count(),
        "apis": 6,
        "logs": Run.objects.count(),
        "users": get_user_model().objects.count(),
    }


def _app_ctx(active_nav, **extra):
    ctx = {"counts": _counts(), "active_nav": active_nav}
    ctx.update(extra)
    return ctx


def _scrapers_annotated():
    return Scraper.objects.annotate(
        run_count=Count("runs"),
        last_run_at=Max("runs__started_at"),
    )


def _launch_run(run):
    """Spawn the run as a detached ``manage.py run_scrape <uuid>`` subprocess.

    ``start_new_session`` makes the child its own process-group leader; we persist
    its PID on the Run so the real-time Stop button can force-kill the whole group.
    """
    proc = subprocess.Popen(
        [sys.executable, "manage.py", "run_scrape", str(run.uuid)],
        cwd=str(settings.BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
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
    ctx = _app_ctx(
        "overview",
        active_scrapers=Scraper.objects.filter(mode=Scraper.Mode.PRODUCTION).count(),
        runs_today=Run.objects.filter(started_at__date=today).count(),
        maint_count=Scraper.objects.filter(mode=Scraper.Mode.MAINTENANCE).count(),
        recent=_scrapers_annotated().order_by("-last_run_at")[:4],
    )
    return render(request, "overview.html", ctx)


@login_required
def scrapers_view(request):
    scrapers = _scrapers_annotated().order_by("name")
    return render(request, "scrapers.html", _app_ctx("scrapers", scrapers=scrapers))


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

        # Settings tab: save the per-scraper proxy selection.
        if request.POST.get("form") == "settings":
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

            s.save(update_fields=["proxy", "threads", "updated_at"])
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

    ctx = _app_ctx("scrapers", s=s, tab=tab, tab_label=TAB_LABELS[tab])

    if tab == "real-time":
        _reap_stale_runs(s)
        ctx["active_run"] = (
            s.runs.filter(status=Run.Status.RUNNING).order_by("-started_at").first()
        )
        current_year = timezone.localdate().year
        ctx["years"] = list(range(YEAR_MAX, YEAR_MIN - 1, -1))
        ctx["default_year"] = min(max(current_year, YEAR_MIN), YEAR_MAX)
    elif tab == "calls":
        paginator = Paginator(s.runs.all(), CALLS_PER_PAGE)
        ctx["page_obj"] = paginator.get_page(request.GET.get("page"))
        ctx["run_total"] = paginator.count
    elif tab == "schedule":
        trigger_url = request.build_absolute_uri(
            reverse("scraper_trigger", args=[s.slug])
        )
        default_year = min(max(timezone.localdate().year, YEAR_MIN), YEAR_MAX)
        secret_name = (
            "MATCHMINER_"
            + "".join(c if c.isalnum() else "_" for c in s.code.upper())
            + "_TRIGGER_TOKEN"
        )
        ctx["trigger_url"] = trigger_url
        ctx["default_year"] = default_year
        ctx["secret_name"] = secret_name
        ctx["workflow_filename"] = f"{s.slug}-schedule.yml"
        ctx["workflow_yaml"] = _github_workflow_yaml(
            code=s.code,
            trigger_url=trigger_url,
            secret_name=secret_name,
            default_year=default_year,
        )
    elif tab == "settings":
        ctx["proxies"] = Proxy.objects.filter(is_active=True).order_by("name")
        ctx["thread_min"] = Scraper.THREADS_MIN
        ctx["thread_max"] = Scraper.THREADS_MAX

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


def _start_scraper_run(scraper, *, year, launched_by):
    """Apply the run guards and launch the worker, returning the new Run.

    Shared by the real-time browser form and the GitHub-Actions trigger webhook so
    both honour maintenance, stale-run reaping, the single-in-flight-run rule, and
    launch-failure handling identically. Raises RunStartError on any guard failure.
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
            tournament=ALL_TOURNAMENTS,
            date_from=date(year, 1, 1),
            date_to=date(year, 12, 31),
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
        year = _parse_year(request.POST.get("year"))
        run = _start_scraper_run(s, year=year, launched_by=request.user)
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


def _trigger_year_param(request):
    """Read the optional ``year`` from a JSON body or a form-encoded POST."""
    if "application/json" in (request.content_type or ""):
        try:
            data = json.loads((request.body or b"").decode("utf-8") or "{}")
        except (ValueError, TypeError, UnicodeDecodeError):
            return ""
        if isinstance(data, dict) and data.get("year") is not None:
            return str(data.get("year")).strip()
        return ""
    return (request.POST.get("year") or "").strip()


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
        year = _parse_year(_trigger_year_param(request), default_current=True)
        run = _start_scraper_run(s, year=year, launched_by=None)
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
            "year": year,
            "events_url": request.build_absolute_uri(
                reverse("run_events", args=[s.slug, run.uuid])
            ),
        },
        status=201,
    )


def _github_workflow_yaml(*, code, trigger_url, secret_name, default_year):
    """Render the copy-ready GitHub Actions workflow shown on the Schedule tab.

    Built in Python (not the template) so the GitHub ``${{ ... }}`` expressions and
    JSON braces don't collide with Django's template syntax.
    """
    return (
        f"name: MatchMiner — {code} scheduled scrape\n"
        f"\n"
        f"on:\n"
        f"  schedule:\n"
        f"    # 06:00 UTC daily. Edit this cron to change the cadence.\n"
        f'    - cron: "0 6 * * *"\n'
        f"  workflow_dispatch:\n"
        f"    inputs:\n"
        f"      year:\n"
        f'        description: "Season year to scrape (2000-2030)"\n'
        f"        required: false\n"
        f'        default: "{default_year}"\n'
        f"\n"
        f"jobs:\n"
        f"  trigger:\n"
        f"    runs-on: ubuntu-latest\n"
        f"    steps:\n"
        f"      - name: Start the {code} scrape\n"
        f"        env:\n"
        f'          TRIGGER_URL: "{trigger_url}"\n'
        f"          TRIGGER_TOKEN: ${{{{ secrets.{secret_name} }}}}\n"
        f"          YEAR: ${{{{ github.event.inputs.year || '{default_year}' }}}}\n"
        f"        run: |\n"
        f'          curl -fsS -X POST "$TRIGGER_URL" \\\n'
        f'            -H "Authorization: Bearer $TRIGGER_TOKEN" \\\n'
        f'            -H "Content-Type: application/json" \\\n'
        f'            --data "{{\\"year\\":\\"$YEAR\\"}}"\n'
    )


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
    # isn't our child — its owner reaps it on its next subprocess launch.
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


def _placeholder(active_nav, kicker, title, sub, empty_title, empty_sub):
    @login_required
    def view(request):
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
)
users_view = _placeholder(
    "users",
    "System",
    "Users",
    "Team members with access to this workspace.",
    "Just you for now",
    "Invite teammates to collaborate in MatchMiner.",
)


@login_required
@require_http_methods(["GET", "POST"])
def proxies_view(request):
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
