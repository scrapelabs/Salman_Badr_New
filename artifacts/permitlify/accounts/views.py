from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Max
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_http_methods

from .models import Run, Scraper
from .runs import ALL_TOURNAMENTS, create_run

TAB_LABELS = {
    "real-time": "Real-time test",
    "code": "Code samples",
    "calls": "Calls history",
    "settings": "Settings",
    "enhancements": "Enhancements",
    "status": "Status",
}

CALLS_PER_PAGE = 12
LOG_LINES_PER_PAGE = 150


def _counts():
    return {
        "scrapers": Scraper.objects.count(),
        "schedule": 12,
        "proxies": 48,
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

    # POST = save Production/Maintenance status from the Status tab.
    if request.method == "POST":
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
        today = timezone.localdate()
        ctx["tournaments"] = [ALL_TOURNAMENTS] + list(s.tournaments or [])
        ctx["default_to"] = today.isoformat()
        ctx["default_from"] = (today - timezone.timedelta(days=7)).isoformat()
    elif tab == "calls":
        paginator = Paginator(s.runs.all(), CALLS_PER_PAGE)
        ctx["page_obj"] = paginator.get_page(request.GET.get("page"))
        ctx["run_total"] = paginator.count

    return render(request, "scraper_detail.html", ctx)


@login_required
@require_http_methods(["POST"])
def scraper_run_view(request, slug):
    s = get_object_or_404(Scraper, slug=slug)
    back = f"{reverse('scraper_detail', args=[slug])}?tab=real-time"

    if s.is_maintenance:
        messages.error(
            request, "This source is in maintenance — real-time runs are blocked."
        )
        return redirect(back)

    tournament = request.POST.get("tournament", ALL_TOURNAMENTS).strip()
    valid = {ALL_TOURNAMENTS} | set(s.tournaments or [])
    if tournament not in valid:
        messages.error(request, "Unknown tournament for this source.")
        return redirect(back)

    raw_from = request.POST.get("date_from", "").strip()
    raw_to = request.POST.get("date_to", "").strip()
    date_from = parse_date(raw_from) if raw_from else None
    date_to = parse_date(raw_to) if raw_to else None

    if bool(raw_from) != bool(raw_to):
        messages.error(request, "Provide both a start and end date, or leave both empty.")
        return redirect(back)
    if (raw_from and not date_from) or (raw_to and not date_to):
        messages.error(request, "Enter valid dates (YYYY-MM-DD).")
        return redirect(back)
    if date_from and date_to and date_from > date_to:
        messages.error(request, "The start date must be on or before the end date.")
        return redirect(back)

    run = create_run(
        s,
        tournament=tournament,
        date_from=date_from,
        date_to=date_to,
        user=request.user,
        allow_failure=False,
    )
    messages.success(
        request,
        f"Run #{run.short_id} {run.get_status_display().lower()} — "
        f"{run.row_count} rows ({run.size_label}).",
    )
    return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=calls")


def _get_run(slug, run_uuid):
    return get_object_or_404(Run, uuid=run_uuid, scraper__slug=slug)


@login_required
def run_log_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    lines = run.log_text.splitlines()
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


@login_required
def run_log_download_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    resp = HttpResponse(run.log_text, content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{slug}-{run.short_id}.log"'
    return resp


@login_required
def run_csv_download_view(request, slug, run_uuid):
    run = _get_run(slug, run_uuid)
    body = run.csv_data or ""
    resp = HttpResponse(body, content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{slug}-{run.short_id}.csv"'
    return resp


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


schedule_view = _placeholder(
    "schedule",
    "Workspace",
    "Schedule",
    "Cron windows and run cadence for every scraper in the workspace.",
    "No schedules yet",
    "Scheduled runs will appear here once you set cadences for your scrapers.",
)
proxies_view = _placeholder(
    "proxies",
    "Workspace",
    "Proxies",
    "Rotating proxy pools and their health, used to route scraper traffic.",
    "No proxies configured",
    "Add a proxy pool to route scraper traffic through it.",
)
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


@require_http_methods(["POST"])
def logout_view(request):
    logout(request)
    return redirect("login")
