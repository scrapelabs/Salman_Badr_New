"""Seed the remaining standalone scrapers (idempotent).

Adds the five sources that complete the source catalogue: the two US
high-school feed APIs (MaxPreps, New Jersey), the PrestoSports college
dual-match API, the FIP padel rankings, and the bespoke Estonia tournament
scraper. Inline, self-contained data so the migration's behaviour never drifts
with app code. The trigger token is generated here because historical models
used in migrations do not run :class:`accounts.models.Scraper`'s custom
``save()`` that normally auto-assigns one — and the field is ``unique``.
"""

import secrets

from django.db import migrations

_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)

SCRAPERS = [
    {
        "slug": "maxpreps",
        "code": "MAX",
        "name": "MaxPreps High School",
        "tour": "US High School",
        "domain": "www.maxpreps.com",
        "vendor_url": "https://www.maxpreps.com",
        "description": (
            "US high-school tennis results from the MaxPreps affiliate feed "
            "(www.maxpreps.com). One run takes a date range and pulls the "
            "singles and doubles XML feeds for every date in the window, "
            "emitting one row per match."
        ),
        "returns": "CSV",
        "tournaments": ["MaxPreps High School Tennis"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "new_jersey_high_school",
        "code": "NJHS",
        "name": "New Jersey High School",
        "tour": "US High School",
        "domain": "www.njschoolsports.com",
        "vendor_url": "https://www.njschoolsports.com",
        "description": (
            "New Jersey high-school tennis results from the NJSchoolSports UTR "
            "results feed (www.njschoolsports.com). One run takes a date range "
            "and pulls the boys' and girls' JSON feeds for every date in the "
            "window, emitting one row per match."
        ),
        "returns": "CSV",
        "tournaments": ["New Jersey High School Tennis"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "prestosports",
        "code": "PRES",
        "name": "PrestoSports",
        "tour": "College",
        "domain": "gameday-api.prestosports.com",
        "vendor_url": "https://www.prestosports.com",
        "description": (
            "College dual-match results from the PrestoSports GameDay API "
            "(gameday-api.prestosports.com). One run takes a date range, logs "
            "in, walks the men's and women's season events in the window, and "
            "parses each event's stats XML into one row per match. Requires "
            "PrestoSports login credentials — without them the run fails "
            "honestly."
        ),
        "returns": "CSV",
        "tournaments": ["College Dual Matches"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "padelfip",
        "code": "FIP",
        "name": "FIP Padel Rankings",
        "tour": "Padel (FIP)",
        "domain": "www.padelfip.com",
        "vendor_url": "https://www.padelfip.com/en/ranking/",
        "description": (
            "International Padel Federation (FIP) world ranking snapshots from "
            "www.padelfip.com. One run takes a single snapshot date and walks "
            "both the men's and women's ranking tables, emitting one row per "
            "ranked player (nationality, points, rank, rank date, rank type)."
        ),
        "returns": "CSV",
        "tournaments": ["FIP Padel Rankings"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "estonia_tournament",
        "code": "EST",
        "name": "Estonia Tournament",
        "tour": "Estonia",
        "domain": "etl.tournamentsoftware.com",
        "vendor_url": "https://www.tennis.ee",
        "description": (
            "Estonian national tennis results. Discovers tournaments from the "
            "tennis.ee calendar, then scrapes each tournament's player list, "
            "per-player matches and player profiles on "
            "etl.tournamentsoftware.com. Input is a date range or a single "
            "tournament URL, emitting one row per played match."
        ),
        "returns": "CSV",
        "tournaments": ["Estonian Tennis"],
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
    dependencies = [("accounts", "0024_seed_rankings_scrapers")]
    operations = [migrations.RunPython(seed, unseed)]
