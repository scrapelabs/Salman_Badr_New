"""Trim the catalogue down to Billie Jean King Cup only.

The other seeded sources had no live implementation. This deletes every
scraper except ``billiejeankingcup`` from existing databases (and, via the
``Run`` FK ``CASCADE``, any runs that belonged to them) so the app ships only
the scraper that actually works. New sources will be added back later.

Irreversible by design — the reverse is a no-op (the old rows can be
re-created by re-running the relevant seed once those scrapers are wired).
"""

from django.db import migrations

KEEP = "billiejeankingcup"


def forwards(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    Scraper.objects.exclude(slug=KEEP).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_proxy_scraper_proxy")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
