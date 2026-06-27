"""Seed the South Africa (Tennis South Africa / SportyHQ) scraper + key queue.

Idempotent. Creates the lone ``south_africa`` Scraper row (with a unique trigger
token, generated here because historical models don't run the model's custom
``save()``) and bulk-seeds its :class:`accounts.models.SAKey` work list from the
vendored ``tournament_keys.txt`` (one 32-hex SportyHQ tournament key per line).
Re-running skips keys that already exist, so it's safe to apply repeatedly.
"""

import secrets
from pathlib import Path

from django.db import migrations

SCRAPER = {
    "slug": "south_africa",
    "code": "ZA",
    "name": "South Africa Results",
    "tour": "TSA",
    "domain": "sportyhq.com",
    "vendor_url": "https://tsa.sportyhq.com",
    "description": (
        "Tennis South Africa results from the SportyHQ public results API. "
        "Queue-driven: works through a list of SportyHQ tournament keys, each "
        "unlocking one tournament's full result set. Manage the queue and paste "
        "extra keys from the Lab's Key queue / Real-time tabs."
    ),
    "returns": "CSV",
    "tournaments": ["Tennis South Africa"],
    "mode": "production",
}

KEYS_FILE = (
    Path(__file__).resolve().parent.parent
    / "live_scrapers"
    / "south_africa_assets"
    / "tournament_keys.txt"
)


def _read_keys():
    keys = []
    seen = set()
    for line in KEYS_FILE.read_text(encoding="utf-8").splitlines():
        key = line.strip().lower()
        if len(key) == 32 and all(c in "0123456789abcdef" for c in key):
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    SAKey = apps.get_model("accounts", "SAKey")

    defaults = dict(SCRAPER)
    defaults["trigger_token"] = secrets.token_urlsafe(32)
    scraper, _ = Scraper.objects.get_or_create(
        slug=SCRAPER["slug"], defaults=defaults
    )

    existing = set(
        SAKey.objects.filter(tournament_key__in=_read_keys()).values_list(
            "tournament_key", flat=True
        )
    )
    rows = [
        SAKey(scraper=scraper, tournament_key=key, status="pending")
        for key in _read_keys()
        if key not in existing
    ]
    if rows:
        SAKey.objects.bulk_create(rows, batch_size=500)


def unseed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    SAKey = apps.get_model("accounts", "SAKey")
    SAKey.objects.filter(scraper__slug=SCRAPER["slug"]).delete()
    Scraper.objects.filter(slug=SCRAPER["slug"]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0037_sakey")]
    operations = [migrations.RunPython(seed, unseed)]
