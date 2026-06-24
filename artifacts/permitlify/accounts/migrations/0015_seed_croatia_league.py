"""Seed the Croatia League (Croatian Tennis Association) scraper (idempotent).

Inline, self-contained data so the migration's behaviour never drifts with app
code. The trigger token is generated here because historical models used in
migrations do not run :class:`accounts.models.Scraper`'s custom ``save()`` that
normally auto-assigns one — and the field is ``unique``.
"""

import secrets

from django.db import migrations

_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)

SCRAPERS = [
    {
        "slug": "croatia_league",
        "code": "CRO",
        "name": "Croatia League (HTS)",
        "tour": "HTS",
        "domain": "hts.tournamentsoftware.com",
        "vendor_url": "https://hts.tournamentsoftware.com",
        "description": (
            "Croatian Tennis Association league results \u2014 leagues, draws and "
            "per-team match results scraped via pure HTTP. Input is either a single "
            "tournament URL or a date range to search the league calendar."
        ),
        "returns": "CSV",
        "tournaments": ["League"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
]


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    for data in SCRAPERS:
        defaults = dict(data)
        defaults["trigger_token"] = secrets.token_urlsafe(32)
        Scraper.objects.get_or_create(slug=data["slug"], defaults=defaults)


def unseed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    Scraper.objects.filter(slug__in=[d["slug"] for d in SCRAPERS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0014_seed_brazil_results")]
    operations = [migrations.RunPython(seed, unseed)]
