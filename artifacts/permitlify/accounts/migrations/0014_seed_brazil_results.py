"""Seed the Brazil Results (CBT) scraper (idempotent).

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
        "slug": "brazil_results",
        "code": "BRA",
        "name": "Brazil Results (CBT)",
        "tour": "CBT",
        "domain": "tenisintegrado.com.br",
        "vendor_url": "https://www.tenisintegrado.com.br",
        "description": (
            "Confedera\u00e7\u00e3o Brasileira de T\u00eanis national results \u2014 "
            "tournaments, draws and per-round match results scraped via pure HTTP. "
            "Input is a season year and month (month 0 = the whole year)."
        ),
        "returns": "CSV",
        "tournaments": ["Profissional", "Juvenil", "Inf-Juv"],
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
    dependencies = [("accounts", "0013_run_params")]
    operations = [migrations.RunPython(seed, unseed)]
