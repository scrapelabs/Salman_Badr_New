"""Atomically replace the Tennis New Zealand member enrichment registry."""

from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.live_scrapers.new_zealand_tournament import (
    MemberRegistryError,
    _decode_member_csv,
    _parse_member_csv,
)
from accounts.models import NewZealandMember, Scraper


class Command(BaseCommand):
    help = "Replace the New Zealand member registry from a TNZ member CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_path", help="Path to the TNZ member CSV export.")
        parser.add_argument(
            "--allow-shrink",
            action="store_true",
            help="Allow the registry to shrink by more than 10%%.",
        )

    def handle(self, *args, **options):
        path = Path(options["csv_path"]).expanduser()
        if not path.is_file():
            raise CommandError(f"No such CSV file: {path}")
        try:
            players = _parse_member_csv(_decode_member_csv(path.read_bytes()))
        except OSError:
            raise CommandError(f"Could not read CSV file: {path}") from None
        except MemberRegistryError as exc:
            raise CommandError(str(exc)) from None
        if not players:
            raise CommandError("The member CSV contains no valid National ID values")

        records = []
        for national_id, details in sorted(players.items()):
            dob = (
                datetime.strptime(details["dob"], "%m/%d/%Y").date()
                if details["dob"]
                else None
            )
            records.append(
                NewZealandMember(
                    national_id=national_id,
                    dob=dob,
                    gender=details["gender"],
                )
            )

        existing_count = NewZealandMember.objects.count()
        if (
            existing_count
            and len(records) * 10 < existing_count * 9
            and not options["allow_shrink"]
        ):
            raise CommandError(
                f"Import would shrink the registry from {existing_count} to "
                f"{len(records)} records. Re-run with --allow-shrink only if "
                "that drop is expected."
            )

        with transaction.atomic():
            # Serialize imports so concurrent commands still have replace, not
            # interleaved delete/insert, semantics.
            Scraper.objects.select_for_update().get(slug="new_zealand_tournament")
            NewZealandMember.objects.all().delete()
            NewZealandMember.objects.bulk_create(records, batch_size=1000)

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(records)} New Zealand member record(s) from {path.name}."
            )
        )
