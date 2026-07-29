import secrets

from django.db import migrations


_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)

SCRAPER = {
    "slug": "belgium_results_2",
    "code": "BE2",
    "name": "Belgium Results 2",
    "tour": "TPPWB",
    "domain": "tennis.tppwb.be",
    "vendor_url": "https://tennis.tppwb.be/MyAFT/Competitions/Tournaments",
    "description": (
        "Tennis Wallonie-Bruxelles results from tennis.tppwb.be. A run accepts "
        "a date range, discovers overlapping tournaments and all published "
        "category draws, then emits completed singles and doubles matches with "
        "official tournament metadata and federation player IDs."
    ),
    "returns": "CSV",
    "tournaments": ["Tennis Wallonie-Bruxelles"],
    "mode": "production",
    "maintenance_message": _DEFAULT_MAINT,
    "threads": 5,
    "max_tries": 4,
}


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    defaults = dict(SCRAPER)
    defaults["trigger_token"] = secrets.token_urlsafe(32)
    Scraper.objects.get_or_create(slug=SCRAPER["slug"], defaults=defaults)


def unseed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    Scraper.objects.filter(slug=SCRAPER["slug"]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0053_force_scraper_schedules_utc")]
    operations = [migrations.RunPython(seed, unseed)]
