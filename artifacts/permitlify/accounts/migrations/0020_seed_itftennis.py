"""Seed the four itftennis.com circuit scrapers (idempotent).

ITF Juniors / Masters / Men's / Women's all share one engine
(:mod:`accounts.live_scrapers._itftennis`), differing only by circuit code and a
few constant labels. Inline, self-contained data so the migration's behaviour
never drifts with app code. The trigger token is generated here because
historical models used in migrations do not run :class:`accounts.models.Scraper`'s
custom ``save()`` that normally auto-assigns one — and the field is ``unique``.
"""

import secrets

from django.db import migrations

_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)


def _circuit(slug, code, name, tour, body):
    return {
        "slug": slug,
        "code": code,
        "name": name,
        "tour": tour,
        "domain": "www.itftennis.com",
        "vendor_url": "https://www.itftennis.com",
        "description": (
            f"{body} \u2014 tournaments, draws and match results scraped from the "
            "itftennis.com calendar API via pure HTTP. Input is either a single "
            "tournament URL or a date range to page the circuit calendar. Needs a "
            "residential proxy (the host sits behind Imperva/Incapsula)."
        ),
        "returns": "CSV",
        "tournaments": ["Tournament"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    }


SCRAPERS = [
    _circuit(
        "itftennis_juniors", "ITFJ", "ITF Juniors", "ITF",
        "ITF Junior circuit results",
    ),
    _circuit(
        "itftennis_masters", "ITFM", "ITF Masters (Seniors)", "ITF",
        "ITF Seniors / Masters circuit results",
    ),
    _circuit(
        "itftennis_mens", "ITFP", "ITF Men's Pro Circuit", "ITF",
        "ITF Men's World Tennis Tour results",
    ),
    _circuit(
        "itftennis_womens", "ITFW", "ITF Women's Pro Circuit", "ITF",
        "ITF Women's World Tennis Tour results",
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
    dependencies = [("accounts", "0019_seed_ts_leagues")]
    operations = [migrations.RunPython(seed, unseed)]
