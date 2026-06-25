"""Seed the dynamic-country tournamentsoftware.com scrapers (idempotent).

GLTA, Tennis Europe, COSAT and ITF Juniors each aggregate tournaments from many
countries on a single tournamentsoftware.com host, so they share the same engine
(:mod:`accounts.live_scrapers._ts_tournament`) in its ``dynamic_country`` mode —
country is read per-tournament and per-player rather than being a federation
constant. Inline, self-contained data so the migration's behaviour never drifts
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


def _ts(slug, name, host, org):
    return {
        "slug": slug,
        "code": slug[:8].upper(),
        "name": name,
        "tour": "TS",
        "domain": host,
        "vendor_url": f"https://{host}",
        "description": (
            f"{org} individual tournament results from {host} "
            "(a tournamentsoftware.com site) \u2014 tournaments, draws and "
            "per-player match results scraped via pure HTTP. This is a "
            "multi-country source: each tournament's country and each player's "
            "nationality are read from the page rather than fixed. Input is "
            "either a single tournament URL or a date range to search the "
            "tournament calendar. Shares the engine with the other "
            "tournamentsoftware federations."
        ),
        "returns": "CSV",
        "tournaments": ["Tournament"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    }


SCRAPERS = [
    _ts("glta_tournament", "GLTA Tournament", "glta.tournamentsoftware.com", "GLTA"),
    _ts("tennis_europe", "Tennis Europe", "te.tournamentsoftware.com", "Tennis Europe"),
    _ts("cosat_tournament", "COSAT Tournament", "cosat.tournamentsoftware.com", "COSAT"),
    _ts("itf_juniors_tournament_software", "ITF Juniors", "itfjuniors.tournamentsoftware.com", "ITF Juniors"),
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
    dependencies = [("accounts", "0017_seed_ts_tournaments")]
    operations = [migrations.RunPython(seed, unseed)]
