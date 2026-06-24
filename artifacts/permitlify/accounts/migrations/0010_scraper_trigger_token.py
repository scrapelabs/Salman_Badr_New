import secrets

from django.db import migrations, models


def backfill_tokens(apps, schema_editor):
    """Give every existing scraper a unique trigger token before the unique index."""
    Scraper = apps.get_model("accounts", "Scraper")
    for scraper in Scraper.objects.all():
        if not scraper.trigger_token:
            scraper.trigger_token = secrets.token_urlsafe(32)
            scraper.save(update_fields=["trigger_token"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_run_pid_alter_run_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="scraper",
            name="trigger_token",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.RunPython(backfill_tokens, noop),
        migrations.AlterField(
            model_name="scraper",
            name="trigger_token",
            field=models.CharField(blank=True, max_length=128, unique=True),
        ),
    ]
