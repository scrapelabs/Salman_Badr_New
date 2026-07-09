"""Seed the WTA + ATP ranking-snapshot scrapers (idempotent).

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
        "slug": "wtatennis",
        "code": "WTA",
        "name": "WTA Rankings",
        "tour": "WTA Tour",
        "domain": "api.wtatennis.com",
        "vendor_url": "https://www.wtatennis.com/rankings",
        "description": (
            "Women's singles and doubles ranking snapshots from the WTA JSON "
            "API (api.wtatennis.com). One run takes a single snapshot date and "
            "walks both ranking tables, emitting one row per ranked player "
            "(birthdate, nationality, points, rank, rank date, rank type)."
        ),
        "returns": "CSV",
        "tournaments": ["WTA Rankings"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "atptour",
        "code": "ATP",
        "name": "ATP Rankings",
        "tour": "ATP Tour",
        "domain": "www.atptour.com",
        "vendor_url": "https://www.atptour.com/en/rankings",
        "description": (
            "Men's singles and doubles ranking snapshots from atptour.com. One "
            "run takes a single snapshot date, discovers every ranked player "
            "from the rankings tables, then enriches each from their hero "
            "endpoint. atptour sits behind Cloudflare, so a residential proxy "
            "is required — without one the run fails honestly."
        ),
        "returns": "CSV",
        "tournaments": ["ATP Rankings"],
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
    dependencies = [("accounts", "0023_seed_czech_scraper")]
    operations = [migrations.RunPython(seed, unseed)]
