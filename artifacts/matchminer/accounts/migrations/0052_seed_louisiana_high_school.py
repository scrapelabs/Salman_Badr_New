import secrets

from django.db import migrations


_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)


SCRAPER = {
    "slug": "louisiana_high_school",
    "code": "LAHS",
    "name": "Louisiana High School",
    "tour": "US High School",
    "domain": "lhsaaonline.org",
    "vendor_url": "https://lhsaaonline.org",
    "description": (
        "Louisiana high-school tennis results from the LHSAA JSON feed "
        "(lhsaaonline.org). One run takes an update-date range and a boys/girls "
        "selector, then emits one row per match. The feed API key is stored in "
        "the scraper Settings tab."
    ),
    "returns": "CSV",
    "tournaments": ["LHSAA Tennis"],
    "mode": "production",
    "maintenance_message": _DEFAULT_MAINT,
    "secret_value": "7761eaf8-774a-40c3-950f-c2698a362e35",
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
    dependencies = [("accounts", "0051_scraperschedule_itf_lookback_days")]
    operations = [migrations.RunPython(seed, unseed)]
