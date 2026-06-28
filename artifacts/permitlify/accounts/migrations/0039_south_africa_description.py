"""Re-point the south_africa Scraper description after the Key queue tab retired.

The seeded blurb told users to "Manage the queue and paste extra keys from the
Lab's Key queue / Real-time tabs". The Key queue tab has been removed from the UI,
so re-point the copy at the Real-time tab (which still launches and monitors the
queue-driven run). Idempotent — re-running just re-sets the same text, and only the
``description`` field is touched.
"""

from django.db import migrations

OLD = (
    "Tennis South Africa results from the SportyHQ public results API. "
    "Queue-driven: works through a list of SportyHQ tournament keys, each "
    "unlocking one tournament's full result set. Manage the queue and paste "
    "extra keys from the Lab's Key queue / Real-time tabs."
)
NEW = (
    "Tennis South Africa results from the SportyHQ public results API. "
    "Queue-driven: works through a list of SportyHQ tournament keys, each "
    "unlocking one tournament's full result set. Paste extra keys or run the "
    "whole pending queue from the Lab's Real-time tab."
)


def forwards(apps, schema_editor):
    # Only rewrite the original seeded blurb — never clobber a description an
    # admin may have customized on a self-hosted DB.
    Scraper = apps.get_model("accounts", "Scraper")
    Scraper.objects.filter(slug="south_africa", description=OLD).update(description=NEW)


def backwards(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    Scraper.objects.filter(slug="south_africa", description=NEW).update(description=OLD)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0038_seed_south_africa")]
    operations = [migrations.RunPython(forwards, backwards)]
