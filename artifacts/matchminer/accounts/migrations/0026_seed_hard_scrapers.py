"""Seed the four advanced ("hard") scrapers (idempotent).

Completes the source catalogue with the four sources that need extra infra or
credentials to run live: Tennis Australia (an Azure Blob match feed behind a
SAS URL), Polish Tennis (the portal.pzt.pl ASP.NET results portal), USTA League
Team Captains (a credentialed TennisLink walk), and the College Dual Match (AI)
scraper (Anthropic Claude box-score extraction). Inline, self-contained data so
the migration's behaviour never drifts with app code. The trigger token is
generated here because historical models used in migrations do not run
:class:`accounts.models.Scraper`'s custom ``save()`` that normally auto-assigns
one — and the field is ``unique``.
"""

import secrets

from django.db import migrations

_DEFAULT_MAINT = (
    "Auto-paused after 5 consecutive failures. An operator must re-enable this "
    "source once the upstream is healthy again."
)

SCRAPERS = [
    {
        "slug": "australia_tennis",
        "code": "AUS",
        "name": "Tennis Australia",
        "tour": "Australia",
        "domain": "blob.core.windows.net",
        "vendor_url": "https://www.tennis.com.au",
        "description": (
            "Tennis Australia match results from an Azure Blob container "
            "(read over the Blob REST API via a SAS URL). One run takes a date "
            "range, lists the daily match-JSON blobs in the window, downloads "
            "each, and emits one row per match. Requires "
            "AUSTRALIA_TENNIS_SAS_URL — without it the run fails honestly."
        ),
        "returns": "CSV",
        "tournaments": ["Tennis Australia"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "poland_results",
        "code": "POL",
        "name": "Poland Results",
        "tour": "Poland",
        "domain": "portal.pzt.pl",
        "vendor_url": "https://portal.pzt.pl",
        "description": (
            "Polish Tennis Association (PZT) results from the portal.pzt.pl "
            "ASP.NET portal. One run takes a date range or a single tournament "
            "URL, discovers tournaments inside the window across the category "
            "summary pages, walks each tournament's order-of-play, and parses "
            "the match tables into one row per played match. Fully "
            "deterministic (no AI)."
        ),
        "returns": "CSV",
        "tournaments": ["Polish Tennis (PZT)"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "usta_team_captains",
        "code": "USTA",
        "name": "USTA Team Captains",
        "tour": "USA Leagues",
        "domain": "tennislink.usta.com",
        "vendor_url": "https://tennislink.usta.com",
        "description": (
            "USTA League team captains from TennisLink (tennislink.usta.com). "
            "One run takes a championship year, logs in, enumerates every NTRP "
            "level and gender, walks each team's roster, and emits one row per "
            "team with its captain (name, city/state, NTRP). Requires "
            "USTA_USERNAME / USTA_PASSWORD — without them the run fails "
            "honestly. Returns a bespoke 15-column captains table."
        ),
        "returns": "CSV",
        "tournaments": ["USTA League Team Captains"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
    {
        "slug": "college_dual_match",
        "code": "CDM",
        "name": "College Dual Match (AI)",
        "tour": "College",
        "domain": "various (athletics sites)",
        "vendor_url": "",
        "description": (
            "College dual-match results extracted from box-score pages/PDFs "
            "with Anthropic Claude. One run takes a box-score, schedule, or "
            "Google-Sheet URL, fetches the recap(s), and asks Claude to "
            "extract the matches into a bespoke 23-column table. Requires "
            "CLAUDE_KEYS (or ANTHROPIC_API_KEY) — without them the run fails "
            "honestly. OPENAI_API_KEY is an optional date-recovery fallback."
        ),
        "returns": "CSV",
        "tournaments": ["College Dual Matches"],
        "mode": "production",
        "maintenance_message": _DEFAULT_MAINT,
    },
]


def seed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    for data in SCRAPERS:
        defaults = dict(data)
        defaults["trigger_token"] = secrets.token_urlsafe(32)
        Scraper.objects.get_or_create(slug=data["slug"], defaults=defaults)


def unseed(apps, schema_editor):
    Scraper = apps.get_model("accounts", "Scraper")
    Scraper.objects.filter(slug__in=[d["slug"] for d in SCRAPERS]).delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0025_seed_standalone_scrapers")]
    operations = [migrations.RunPython(seed, unseed)]
