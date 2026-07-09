"""Seed the tournamentsoftware.com individual-tournament scrapers (idempotent).

Seven federations share one engine (:mod:`accounts.live_scrapers._ts_tournament`),
differing only by host and a few constant fields. Inline, self-contained data so
the migration's behaviour never drifts with app code. The trigger token is
generated here because historical models used in migrations do not run
:class:`accounts.models.Scraper`'s custom ``save()`` that normally auto-assigns
one — and the field is ``unique``.
"""

import secrets

from django.db import migrations

_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)


def _ts(slug, name, host, sanction):
    return {
        "slug": slug,
        "code": slug[:8].upper(),
        "name": name,
        "tour": "TS",
        "domain": host,
        "vendor_url": f"https://{host}",
        "description": (
            f"{sanction} individual tournament results from {host} "
            "(a tournamentsoftware.com site) \u2014 tournaments, draws and "
            "per-player match results scraped via pure HTTP. Input is either a "
            "single tournament URL or a date range to search the tournament "
            "calendar. Shares the engine with the other tournamentsoftware "
            "federations."
        ),
        "returns": "CSV",
        "tournaments": ["Tournament"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    }


SCRAPERS = [
    _ts("croatia_tournament", "Croatia Tournament", "hts.tournamentsoftware.com", "Croatian Tennis Association"),
    _ts("denmark_tournament", "Denmark Tournament", "dtf.tournamentsoftware.com", "Danish Tennis Federation"),
    _ts("sweden_tournament", "Sweden Tournament", "svtf.tournamentsoftware.com", "Swedish Tennis Association"),
    _ts("hong_kong_tournament", "Hong Kong Tournament", "hkta.tournamentsoftware.com", "Hong Kong Tennis Association"),
    _ts("finland_tournament", "Finland Tournament", "www.tennisassa.fi", "Tennis Finland"),
    _ts("ireland_tournament", "Ireland Tournament", "ti.tournamentsoftware.com", "Tennis Ireland"),
    _ts("luxembourg_tournament", "Luxembourg Tournament", "flt.tournamentsoftware.com", "Luxembourg Tennis Federation"),
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
    dependencies = [("accounts", "0016_seed_davis_cup")]
    operations = [migrations.RunPython(seed, unseed)]
