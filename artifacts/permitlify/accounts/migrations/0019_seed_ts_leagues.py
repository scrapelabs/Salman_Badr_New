"""Seed the four additional tournamentsoftware league scrapers (idempotent).

Denmark / Sweden / Hong Kong / Finland leagues share croatia_league's engine
(:mod:`accounts.live_scrapers._ts_league`), differing only by host and country
constants. Inline, self-contained data so the migration's behaviour never drifts
with app code. The trigger token is generated here because historical models used
in migrations do not run :class:`accounts.models.Scraper`'s custom ``save()`` that
normally auto-assigns one — and the field is ``unique``.
"""

import secrets

from django.db import migrations

_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)


def _league(slug, code, name, tour, domain, body):
    return {
        "slug": slug,
        "code": code,
        "name": name,
        "tour": tour,
        "domain": domain,
        "vendor_url": f"https://{domain}",
        "description": (
            f"{body} league results \u2014 leagues, draws and per-team match "
            "results scraped via pure HTTP. Input is either a single league URL "
            "or a date range to search the league calendar."
        ),
        "returns": "CSV",
        "tournaments": ["League"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    }


SCRAPERS = [
    _league(
        "denmark_league", "DEN", "Denmark League (DTF)", "DTF",
        "dtf.tournamentsoftware.com", "Danish Tennis Federation",
    ),
    _league(
        "sweden_league", "SWE", "Sweden League (SvTF)", "SVTF",
        "svtf.tournamentsoftware.com", "Tennis Sweden",
    ),
    _league(
        "hong_kong_league", "HKG", "Hong Kong League (HKTA)", "HKTA",
        "hkta.tournamentsoftware.com", "Hong Kong Tennis Association",
    ),
    _league(
        "finland_league", "FIN", "Finland League (Tennisässä)", "TENNISASSA",
        "www.tennisassa.fi", "Tennis Finland",
    ),
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
    dependencies = [("accounts", "0018_seed_dynamic_ts_tournaments")]
    operations = [migrations.RunPython(seed, unseed)]
