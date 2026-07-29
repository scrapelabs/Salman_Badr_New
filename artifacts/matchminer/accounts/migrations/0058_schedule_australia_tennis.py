from datetime import datetime, time, timedelta, timezone

from django.db import migrations


SCRAPER_SLUG = "australia_tennis"


def _next_6am_utc():
    now = datetime.now(timezone.utc)
    candidate = datetime.combine(now.date(), time(6, 0), tzinfo=timezone.utc)
    return candidate if candidate > now else candidate + timedelta(days=1)


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    ScraperSchedule = apps.get_model("accounts", "ScraperSchedule")
    db_alias = schema_editor.connection.alias
    scraper = Scraper.objects.using(db_alias).get(slug=SCRAPER_SLUG)
    ScraperSchedule.objects.using(db_alias).update_or_create(
        scraper=scraper,
        defaults={
            "enabled": True,
            "frequency": "daily",
            "time_of_day": time(6, 0),
            "timezone": "UTC",
            "anchor_date": None,
            "next_run_at": _next_6am_utc(),
        },
    )


def unseed(apps, schema_editor):
    ScraperSchedule = apps.get_model("accounts", "ScraperSchedule")
    db_alias = schema_editor.connection.alias
    ScraperSchedule.objects.using(db_alias).filter(
        scraper__slug=SCRAPER_SLUG
    ).update(enabled=False, next_run_at=None)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0057_newzealandmember")]
    operations = [migrations.RunPython(seed, unseed)]
