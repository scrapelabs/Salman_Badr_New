"""Seed the Ioncourt (college dual-match) scraper (idempotent).

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
        "slug": "ioncourt",
        "code": "IC",
        "name": "Ioncourt",
        "tour": "ITA",
        "domain": "ioncourt.com",
        "vendor_url": "https://ioncourt.com",
        "description": (
            "College dual-match results from the Ioncourt JSON API "
            "(api.ioncourt.com). Logs in, paginates completed college ties, then "
            "pulls each tie's teams and individual matches. Input is a date "
            "range. Needs IONCOURT_PHONE / IONCOURT_PASSWORD to be set."
        ),
        "returns": "CSV",
        "tournaments": ["Ioncourt"],
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
    dependencies = [("accounts", "0021_seed_uruguay_results")]
    operations = [migrations.RunPython(seed, unseed)]
