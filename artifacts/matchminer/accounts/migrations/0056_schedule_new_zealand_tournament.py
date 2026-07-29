from datetime import datetime, time, timedelta, timezone

from django.db import migrations


SCRAPER_SLUG = "new_zealand_tournament"


def _next_wednesday_6am():
    now = datetime.now(timezone.utc)
    candidate = datetime.combine(
        now.date() + timedelta(days=(2 - now.weekday()) % 7),
        time(6, 0),
        tzinfo=timezone.utc,
    )
    return candidate if candidate > now else candidate + timedelta(days=7)


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    ScraperSchedule = apps.get_model("accounts", "ScraperSchedule")
    db_alias = schema_editor.connection.alias
    scraper = Scraper.objects.using(db_alias).get(slug=SCRAPER_SLUG)
    ScraperSchedule.objects.using(db_alias).update_or_create(
        scraper=scraper,
        defaults={
            "enabled": True,
            "frequency": "weekly",
            "time_of_day": time(6, 0),
            "weekday": 2,
            "timezone": "UTC",
            "anchor_date": None,
            "next_run_at": _next_wednesday_6am(),
        },
    )


def unseed(apps, schema_editor):
    ScraperSchedule = apps.get_model("accounts", "ScraperSchedule")
    db_alias = schema_editor.connection.alias
    ScraperSchedule.objects.using(db_alias).filter(
        scraper__slug=SCRAPER_SLUG
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0055_seed_new_zealand_tournament")]
    operations = [migrations.RunPython(seed, unseed)]
