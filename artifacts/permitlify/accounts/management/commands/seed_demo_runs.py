"""Seed demo Run rows for each scraper.

Idempotent by default: scrapers that already have runs are skipped. Pass
``--reset`` to wipe and regenerate. Run after ``migrate`` to populate the
calls-history tab with sample data.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Run, Scraper
from accounts.runs import ALL_TOURNAMENTS, create_run


class Command(BaseCommand):
    help = "Create simulated demo runs for every scraper (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing runs first.")
        parser.add_argument("--per", type=int, default=16, help="Runs per scraper.")

    def handle(self, *args, **options):
        reset = options["reset"]
        per = max(1, options["per"])
        now = timezone.now()
        created_total = 0

        for scraper in Scraper.objects.all():
            if reset:
                scraper.runs.all().delete()
            elif scraper.runs.exists():
                self.stdout.write(f"  skip {scraper.code} (already has runs)")
                continue

            # Stable-ish per scraper so re-seeding looks consistent.
            rng = random.Random(f"{scraper.slug}:{per}")
            options_pool = [ALL_TOURNAMENTS] + list(scraper.tournaments or [])
            count = per if not scraper.is_maintenance else max(4, per // 2)

            for i in range(count):
                started = now - timedelta(
                    hours=i * rng.randint(5, 11), minutes=rng.randint(0, 59)
                )
                tournament = rng.choice(options_pool)
                span = rng.randint(1, 21)
                date_to = started.date()
                date_from = date_to - timedelta(days=span)
                # Maintenance sources have a higher recent failure rate.
                forced = None
                if scraper.is_maintenance and i < 3:
                    forced = Run.Status.FAILED
                create_run(
                    scraper,
                    tournament=tournament,
                    date_from=date_from,
                    date_to=date_to,
                    started_at=started,
                    status=forced,
                    rng=rng,
                )
                created_total += 1
            self.stdout.write(self.style.SUCCESS(f"  seeded {count} runs for {scraper.code}"))

        self.stdout.write(self.style.SUCCESS(f"Done. {created_total} runs created."))
