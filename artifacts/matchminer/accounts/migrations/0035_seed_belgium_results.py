"""Seed the Belgium (Tennis & Padel Vlaanderen) scraper (idempotent).

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
        "slug": "belgium_results",
        "code": "BE",
        "name": "Belgium Results",
        "tour": "TPV",
        "domain": "tennisenpadelvlaanderen.be",
        "vendor_url": "https://www.tennisenpadelvlaanderen.be",
        "description": (
            "Belgian (Flanders) tennis & padel results from "
            "tennisenpadelvlaanderen.be. Discovers tournaments in the run "
            "window via the public search, follows each draw's series pages, and "
            "scrapes the match game-tables. The site sits behind a Zenedge "
            "captcha, so live runs need the captcha model + TensorFlow present. "
            "Input is a date range or a single tournament URL."
        ),
        "returns": "CSV",
        "tournaments": ["Tennis Vlaanderen"],
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
    dependencies = [("accounts", "0034_scheduleevent")]
    operations = [migrations.RunPython(seed, unseed)]
