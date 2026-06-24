"""Seed the Billie Jean King Cup scraper (idempotent).

Inline, self-contained data so the migration's behaviour never drifts with app
code. Runs are not seeded — they are produced live by the real scraper in
``accounts.live_scrapers`` when a user starts a scrape. Other sources will be
added back later; for now the catalogue ships only the fully-working scraper.
"""

from django.db import migrations

_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)

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
