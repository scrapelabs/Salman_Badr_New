from datetime import timezone as dt_timezone

from django.db import migrations, models


def normalize_schedules_to_utc(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    ScraperSchedule = apps.get_model("accounts", "ScraperSchedule")
    db_alias = schema_editor.connection.alias

    schedules = ScraperSchedule.objects.using(db_alias).exclude(timezone="UTC")
    for schedule in schedules.iterator():
        update_fields = ["timezone"]
        schedule.timezone = "UTC"

        # Preserve an enabled schedule's already-announced next instant, then use
        # that instant's UTC wall-clock fields for every subsequent recurrence.
        if schedule.next_run_at is not None:
            due_utc = schedule.next_run_at.astimezone(dt_timezone.utc)
            schedule.time_of_day = due_utc.time().replace(tzinfo=None)
            update_fields.append("time_of_day")

            if schedule.frequency in ("weekly", "biweekly"):
                schedule.weekday = due_utc.weekday()
                update_fields.append("weekday")
            if schedule.frequency == "monthly":
                schedule.day_of_month = due_utc.day
                update_fields.append("day_of_month")
            if schedule.frequency == "biweekly":
                schedule.anchor_date = due_utc.date()
                update_fields.append("anchor_date")

        schedule.save(using=db_alias, update_fields=update_fields)

    sportradar = Scraper.objects.using(db_alias).filter(slug="sportradar").first()
    if sportradar and "at 2:00 AM America/New_York" in sportradar.description:
        sportradar.description = sportradar.description.replace(
            "at 2:00 AM America/New_York",
            "at its configured UTC time",
        )
        sportradar.save(using=db_alias, update_fields=["description"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0052_seed_louisiana_high_school")]

    operations = [
        migrations.RunPython(normalize_schedules_to_utc, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="scraperschedule",
            name="timezone",
            field=models.CharField(
                choices=[("UTC", "UTC")],
                default="UTC",
                max_length=64,
            ),
        ),
    ]
