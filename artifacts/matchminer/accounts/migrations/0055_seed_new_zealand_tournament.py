import secrets

from django.db import migrations


_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)

SCRAPER = {
    "slug": "new_zealand_tournament",
    "code": "NZ_T",
    "name": "New Zealand Tournament",
    "tour": "TNZ",
    "domain": "tnz.tournamentsoftware.com",
    "vendor_url": "https://tnz.tournamentsoftware.com/find",
    "description": (
        "Tennis New Zealand individual tournament results from "
        "tnz.tournamentsoftware.com. Runs accept a date range or one tournament "
        "URL; scheduled runs use a 14-day lookback ending on run day."
    ),
    "returns": "CSV",
    "tournaments": ["Tennis New Zealand"],
    "mode": "production",
    "maintenance_message": _DEFAULT_MAINT,
    "threads": 5,
    "max_tries": 4,
}


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    db_alias = schema_editor.connection.alias
    defaults = dict(SCRAPER)
    defaults["trigger_token"] = secrets.token_urlsafe(32)
    Scraper.objects.using(db_alias).get_or_create(
        slug=SCRAPER["slug"], defaults=defaults
    )


def unseed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    db_alias = schema_editor.connection.alias
    Scraper.objects.using(db_alias).filter(slug=SCRAPER["slug"]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0054_seed_belgium_results_2")]
    operations = [migrations.RunPython(seed, unseed)]
