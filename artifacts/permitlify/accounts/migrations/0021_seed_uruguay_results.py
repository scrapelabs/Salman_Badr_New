"""Seed the Uruguay Results (AUT / tenisintegrado) scraper (idempotent).

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
        "slug": "uruguay_results",
        "code": "UR",
        "name": "Uruguay Results",
        "tour": "AUT",
        "domain": "uruguay.tenisintegrado.com",
        "vendor_url": "https://uruguay.tenisintegrado.com",
        "description": (
            "Uruguay (AUT) tournament results from the Uruguayan tenisintegrado "
            "platform \u2014 walks the Menores and Profesional sections, each "
            "month's tournaments, and every bracket panel via curl_cffi. Input "
            "is a season year + month (0 = whole year). Shares its match-block "
            "parser shape with Brazil Results."
        ),
        "returns": "CSV",
        "tournaments": ["Uruguay Results"],
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
    dependencies = [("accounts", "0020_seed_itftennis")]
    operations = [migrations.RunPython(seed, unseed)]
