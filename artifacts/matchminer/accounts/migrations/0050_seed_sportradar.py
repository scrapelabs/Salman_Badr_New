"""Seed the SportRadar Tennis daily summaries scraper and schedule."""

import secrets
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import migrations
from django.utils import timezone

SCRAPER = {
    "slug": "sportradar",
    "code": "SRAD",
    "name": "SportRadar Tennis",
    "tour": "SportRadar",
    "domain": "api.sportradar.com",
    "vendor_url": "https://developer.sportradar.com/tennis/reference/daily-summaries",
    "description": (
        "SportRadar Tennis daily summaries. Scheduled runs pull yesterday and "
        "today at 2:00 AM America/New_York, filter to the configured ATP/WTA/ITF/"
        "Cup category IDs, and emit one row per scored match. Requires a "
        "SportRadar API key in this scraper's Settings tab or SPORTRADAR_API_KEY."
    ),
    "returns": "CSV",
    "tournaments": ["SportRadar Tennis Daily Summaries"],
    "mode": "production",
}


def _next_2am_new_york():
    tz = ZoneInfo("America/New_York")
    now_local = timezone.now().astimezone(tz)
    candidate = datetime.combine(now_local.date(), time(2, 0), tzinfo=tz)
    if candidate <= now_local:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(ZoneInfo("UTC"))


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    ScraperSchedule = apps.get_model("accounts", "ScraperSchedule")

    defaults = dict(SCRAPER)
    defaults["trigger_token"] = secrets.token_urlsafe(32)
    scraper, _ = Scraper.objects.get_or_create(slug=SCRAPER["slug"], defaults=defaults)
    ScraperSchedule.objects.update_or_create(
        scraper=scraper,
        defaults={
            "enabled": True,
            "frequency": "daily",
            "time_of_day": time(2, 0),
            "timezone": "America/New_York",
            "next_run_at": _next_2am_new_york(),
        },
    )


def unseed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    Scraper.objects.filter(slug=SCRAPER["slug"]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0049_overview_hot_path_indexes")]
    operations = [migrations.RunPython(seed, unseed)]
