"""Seed the Davis Cup (ITF men's team competition) scraper (idempotent).

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
        "slug": "davis_cup",
        "code": "DC",
        "name": "Davis Cup",
        "tour": "ITF",
        "domain": "daviscup.com",
        "vendor_url": "https://www.daviscup.com",
        "description": (
            "Davis Cup (ITF men's team competition) results from the public ITF "
            "/ Stadion data API \u2014 ties and per-match results scraped via "
            "curl_cffi. Input is a single season year. Shares the engine with "
            "the Billie Jean King Cup; needs a residential proxy because the "
            "upstream sits behind CloudFront (blocks data-center IPs)."
        ),
        "returns": "CSV",
        "tournaments": ["Davis Cup"],
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
    dependencies = [("accounts", "0015_seed_croatia_league")]
    operations = [migrations.RunPython(seed, unseed)]
