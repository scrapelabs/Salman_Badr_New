from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

DEFAULT_MAINT_MSG = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable "
    "this source once the upstream is healthy again."
)

# In-memory sample catalogue of scraper scripts (each one mirrors a real
# spider like `billiejeankingcup`). Mutated in place by the status form so the
# Production/Maintenance toggle behaves for the rest of the server process.
SCRAPERS = [
    {
        "slug": "billiejeankingcup",
        "code": "BJK",
        "name": "Billie Jean King Cup",
        "tour": "ITF",
        "domain": "billiejeankingcup.com",
        "vendor": "https://www.billiejeankingcup.com",
        "desc": "Women's national-team competition — ties, results and per-round "
        "draws scraped via pure HTTP. Session cookies are cached per spider run "
        "to skip the login step.",
        "mode": "production",
        "calls": 1240,
        "returns": "CSV",
        "last_run": "2h ago",
        "maintenance_message": DEFAULT_MAINT_MSG,
    },
    {
        "slug": "daviscup",
        "code": "DC",
        "name": "Davis Cup",
        "tour": "ITF",
        "domain": "daviscup.com",
        "vendor": "https://www.daviscup.com",
        "desc": "Men's national-team competition — group ties, knockout draws and "
        "live tie scores mined per round.",
        "mode": "production",
        "calls": 980,
        "returns": "CSV",
        "last_run": "3h ago",
        "maintenance_message": DEFAULT_MAINT_MSG,
    },
    {
        "slug": "atp-rankings",
        "code": "ATP",
        "name": "ATP Rankings",
        "tour": "ATP Tour",
        "domain": "atptour.com",
        "vendor": "https://www.atptour.com/en/rankings",
        "desc": "Weekly singles and doubles rankings with points, movement and "
        "tournaments-played fields.",
        "mode": "production",
        "calls": 4120,
        "returns": "JSON",
        "last_run": "12m ago",
        "maintenance_message": DEFAULT_MAINT_MSG,
    },
    {
        "slug": "wta-rankings",
        "code": "WTA",
        "name": "WTA Rankings",
        "tour": "WTA Tour",
        "domain": "wtatennis.com",
        "vendor": "https://www.wtatennis.com/rankings",
        "desc": "Weekly singles and doubles rankings, including race-to-finals "
        "standings and country breakdowns.",
        "mode": "production",
        "calls": 3870,
        "returns": "JSON",
        "last_run": "18m ago",
        "maintenance_message": DEFAULT_MAINT_MSG,
    },
    {
        "slug": "ausopen",
        "code": "AO",
        "name": "Australian Open",
        "tour": "Grand Slam",
        "domain": "ausopen.com",
        "vendor": "https://www.ausopen.com",
        "desc": "Grand Slam draws, schedules and match statistics across all "
        "events.",
        "mode": "production",
        "calls": 640,
        "returns": "CSV",
        "last_run": "1h ago",
        "maintenance_message": DEFAULT_MAINT_MSG,
    },
    {
        "slug": "rolandgarros",
        "code": "RG",
        "name": "Roland-Garros",
        "tour": "Grand Slam",
        "domain": "rolandgarros.com",
        "vendor": "https://www.rolandgarros.com",
        "desc": "Clay-court Grand Slam draws and results. Vendor markup changed "
        "and the parser is being updated.",
        "mode": "maintenance",
        "calls": 0,
        "returns": "CSV",
        "last_run": "5d ago",
        "maintenance_message": "Source layout changed on the vendor site. Parser "
        "is being rebuilt — re-enable once details.py is updated and verified.",
    },
    {
        "slug": "wimbledon",
        "code": "WIM",
        "name": "Wimbledon",
        "tour": "Grand Slam",
        "domain": "wimbledon.com",
        "vendor": "https://www.wimbledon.com",
        "desc": "Grass-court Grand Slam draws, order of play and completed match "
        "results.",
        "mode": "production",
        "calls": 510,
        "returns": "CSV",
        "last_run": "4h ago",
        "maintenance_message": DEFAULT_MAINT_MSG,
    },
    {
        "slug": "usopen",
        "code": "USO",
        "name": "US Open",
        "tour": "Grand Slam",
        "domain": "usopen.org",
        "vendor": "https://www.usopen.org",
        "desc": "Hard-court Grand Slam draws, schedules and detailed match "
        "statistics.",
        "mode": "production",
        "calls": 720,
        "returns": "CSV",
        "last_run": "2h ago",
        "maintenance_message": DEFAULT_MAINT_MSG,
    },
    {
        "slug": "atp-live",
        "code": "LIVE",
        "name": "ATP Live Scores",
        "tour": "ATP Tour",
        "domain": "atptour.com",
        "vendor": "https://www.atptour.com/en/scores",
        "desc": "Real-time live match scores polled at a high cadence during "
        "tournament play.",
        "mode": "maintenance",
        "calls": 30,
        "returns": "JSON",
        "last_run": "20m ago",
        "maintenance_message": "Upstream rate-limited the live feed. Paused to "
        "avoid bans — re-enable when the cooldown window clears.",
    },
]

TAB_LABELS = {
    "real-time": "Real-time test",
    "code": "Code samples",
    "calls": "Calls history",
    "settings": "Settings",
    "enhancements": "Enhancements",
    "status": "Status",
}


def _get_scraper(slug):
    for s in SCRAPERS:
        if s["slug"] == slug:
            return s
    return None


def _counts():
    return {
        "scrapers": len(SCRAPERS),
        "schedule": 12,
        "proxies": 48,
        "apis": 6,
        "logs": 9,
        "users": 4,
    }


def _app_ctx(active_nav, **extra):
    ctx = {"counts": _counts(), "active_nav": active_nav}
    ctx.update(extra)
    return ctx


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
    calls_today = sum(s["calls"] for s in SCRAPERS)
    maint_count = sum(1 for s in SCRAPERS if s["mode"] == "maintenance")
    ctx = _app_ctx(
        "overview",
        calls_today=f"{calls_today:,}",
        maint_count=maint_count,
        recent=SCRAPERS[:4],
    )
    return render(request, "overview.html", ctx)


@login_required
def scrapers_view(request):
    return render(request, "scrapers.html", _app_ctx("scrapers", scrapers=SCRAPERS))


@login_required
def scraper_detail_view(request, slug):
    s = _get_scraper(slug)
    if s is None:
        raise Http404("Scraper not found")

    if request.method == "POST":
        mode = request.POST.get("mode", s["mode"])
        if mode in ("production", "maintenance"):
            s["mode"] = mode
        s["maintenance_message"] = request.POST.get(
            "maintenance_message", s["maintenance_message"]
        )
        return redirect(f"{reverse('scraper_detail', args=[slug])}?tab=status")

    tab = request.GET.get("tab", "status")
    if tab not in TAB_LABELS:
        tab = "status"
    ctx = _app_ctx("scrapers", s=s, tab=tab, tab_label=TAB_LABELS[tab])
    return render(request, "scraper_detail.html", ctx)


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
