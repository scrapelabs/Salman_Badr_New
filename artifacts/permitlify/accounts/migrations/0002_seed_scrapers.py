"""Seed the catalogue of 9 tennis scrapers (idempotent).

Inline, self-contained data so the migration's behaviour never drifts with app
code. Run definitions (logs/CSV) are seeded separately by the
``seed_demo_runs`` management command.
"""

from django.db import migrations

_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)

_SLAM_EVENTS = [
    "Men's Singles",
    "Women's Singles",
    "Men's Doubles",
    "Women's Doubles",
    "Mixed Doubles",
]

SCRAPERS = [
    {
        "slug": "billiejeankingcup",
        "code": "BJK",
        "name": "Billie Jean King Cup",
        "tour": "ITF",
        "domain": "billiejeankingcup.com",
        "vendor_url": "https://www.billiejeankingcup.com",
        "description": (
            "Women's national-team competition \u2014 ties, results and per-round "
            "draws scraped via pure HTTP. Session cookies are cached per spider "
            "run to skip the login step."
        ),
        "returns": "CSV",
        "tournaments": ["Finals", "Qualifiers", "Play-offs"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "daviscup",
        "code": "DC",
        "name": "Davis Cup",
        "tour": "ITF",
        "domain": "daviscup.com",
        "vendor_url": "https://www.daviscup.com",
        "description": (
            "Men's national-team competition \u2014 group ties, knockout draws and "
            "live tie scores mined per round."
        ),
        "returns": "CSV",
        "tournaments": ["Finals", "Qualifiers", "World Group I", "World Group II"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "atp-rankings",
        "code": "ATP",
        "name": "ATP Rankings",
        "tour": "ATP Tour",
        "domain": "atptour.com",
        "vendor_url": "https://www.atptour.com/en/rankings",
        "description": (
            "Weekly singles and doubles rankings with points, movement and "
            "tournaments-played fields."
        ),
        "returns": "JSON",
        "tournaments": ["Singles", "Doubles", "Race to Turin"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "wta-rankings",
        "code": "WTA",
        "name": "WTA Rankings",
        "tour": "WTA Tour",
        "domain": "wtatennis.com",
        "vendor_url": "https://www.wtatennis.com/rankings",
        "description": (
            "Weekly singles and doubles rankings, including race-to-finals "
            "standings and country breakdowns."
        ),
        "returns": "JSON",
        "tournaments": ["Singles", "Doubles", "Race to Riyadh"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "ausopen",
        "code": "AO",
        "name": "Australian Open",
        "tour": "Grand Slam",
        "domain": "ausopen.com",
        "vendor_url": "https://www.ausopen.com",
        "description": "Grand Slam draws, schedules and match statistics across all events.",
        "returns": "CSV",
        "tournaments": list(_SLAM_EVENTS),
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "rolandgarros",
        "code": "RG",
        "name": "Roland-Garros",
        "tour": "Grand Slam",
        "domain": "rolandgarros.com",
        "vendor_url": "https://www.rolandgarros.com",
        "description": (
            "Clay-court Grand Slam draws and results. Vendor markup changed and "
            "the parser is being updated."
        ),
        "returns": "CSV",
        "tournaments": list(_SLAM_EVENTS),
        "mode": "maintenance",
        "maintenance_message": (
            "Source layout changed on the vendor site. Parser is being rebuilt "
            "\u2014 re-enable once details.py is updated and verified."
        ),
    },
    {
        "slug": "wimbledon",
        "code": "WIM",
        "name": "Wimbledon",
        "tour": "Grand Slam",
        "domain": "wimbledon.com",
        "vendor_url": "https://www.wimbledon.com",
        "description": "Grass-court Grand Slam draws, order of play and completed match results.",
        "returns": "CSV",
        "tournaments": list(_SLAM_EVENTS),
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "usopen",
        "code": "USO",
        "name": "US Open",
        "tour": "Grand Slam",
        "domain": "usopen.org",
        "vendor_url": "https://www.usopen.org",
        "description": "Hard-court Grand Slam draws, schedules and detailed match statistics.",
        "returns": "CSV",
        "tournaments": list(_SLAM_EVENTS),
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "atp-live",
        "code": "LIVE",
        "name": "ATP Live Scores",
        "tour": "ATP Tour",
        "domain": "atptour.com",
        "vendor_url": "https://www.atptour.com/en/scores",
        "description": "Real-time live match scores polled at a high cadence during tournament play.",
        "returns": "JSON",
        "tournaments": ["Live Singles", "Live Doubles"],
        "mode": "maintenance",
        "maintenance_message": (
            "Upstream rate-limited the live feed. Paused to avoid bans \u2014 "
            "re-enable when the cooldown window clears."
        ),
    },
]


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    for data in SCRAPERS:
        Scraper.objects.get_or_create(slug=data["slug"], defaults=data)


def unseed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    Scraper.objects.filter(slug__in=[d["slug"] for d in SCRAPERS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0001_initial")]
    operations = [migrations.RunPython(seed, unseed)]
