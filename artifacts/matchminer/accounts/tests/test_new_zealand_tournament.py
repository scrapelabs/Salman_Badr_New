from datetime import date, time, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from parsel import Selector

from accounts.live_scrapers import _ts_tournament, new_zealand_tournament, registry
from accounts.models import NewZealandMember, Run, Scraper


class NewZealandTournamentDashboardTests(TestCase):
    def setUp(self):
        self.scraper = Scraper.objects.get(slug="new_zealand_tournament")
        self.user = get_user_model().objects.create_superuser(
            username="new-zealand-operator",
            password="test-password",
        )
        self.client.force_login(self.user)

    def test_seeded_scraper_is_visible(self):
        self.assertEqual(self.scraper.code, "NZ_T")
        self.assertEqual(self.scraper.name, "New Zealand Tournament")
        self.assertEqual(self.scraper.tour, "TNZ")
        self.assertEqual(self.scraper.domain, "tnz.tournamentsoftware.com")
        self.assertTrue(self.scraper.trigger_token)

        schedule = self.scraper.schedule
        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.frequency, "weekly")
        self.assertEqual(schedule.time_of_day, time(6, 0))
        self.assertEqual(schedule.weekday, 2)
        self.assertEqual(schedule.timezone, "UTC")
        self.assertEqual(schedule.next_run_at.weekday(), 2)
        self.assertEqual(schedule.next_run_at.hour, 6)
        self.assertEqual(schedule.next_run_at.minute, 0)
        self.assertEqual(schedule.next_run_at.utcoffset(), timedelta(0))

        response = self.client.get(reverse("scrapers"))
        self.assertContains(response, 'data-slug="new_zealand_tournament"')
        self.assertContains(response, "New Zealand Tournament")

    def test_detail_uses_standard_tabs_and_fourteen_day_form(self):
        response = self.client.get(
            reverse("scraper_detail", args=[self.scraper.slug])
        )

        today = timezone.localdate()
        self.assertContains(response, 'name="tournament_url"')
        self.assertContains(
            response,
            f'name="date_from" type="date" value="{today - timedelta(days=14)}"',
        )
        self.assertContains(
            response,
            f'name="date_to" type="date" value="{today}"',
        )
        for label in (
            "Batch jobs",
            "Real-time",
            "Calls history",
            "QA",
            "Status",
            "Settings",
            "Schedule",
        ):
            self.assertContains(response, label)

        spec = registry.get_spec(self.scraper.slug)
        self.assertEqual(spec.input_kind, registry.INPUT_DATE_RANGE_OR_URL)
        self.assertEqual(spec.allowed_hosts, ("tnz.tournamentsoftware.com",))
        self.assertEqual(spec.default_range_days, 14)
        self.assertIs(spec.load_runner(), new_zealand_tournament.run)


class FakeSelectorClient:
    def __init__(self, pages):
        self.pages = pages

    def get_selector(self, url):
        html = self.pages.get(url)
        return Selector(text=html) if html is not None else None


class NewZealandMemberRegistryTests(SimpleTestCase):
    def test_member_csv_preserves_ids_and_merges_valid_duplicate_fields(self):
        text = (
            "\ufeffSurname,Name,National ID,Birth Day,Birth Month,Birth Year,Gender\n"
            '"O\'Connor, Jr.",Jos\u00e9,000123,7,2,2010,\n'
            "Duplicate,Record,000123,bad,13,unknown,O\n"
            "Invalid,Date,BAD-DATE,31,2,2011,F\n"
            "Decimal,Date,DECIMAL,1.0,12.0,2009.0,X\n"
            "Future,Date,FUTURE,1,1,9999,M\n"
            "Malformed,Too,Few,1,1\n"
            "Malformed,Too,Many,1,1,2000,M,extra\n"
            "Broken,Identifier,BAD\ufffdID,1,1,2000,M\n"
            "Missing,Identifier,,1,1,2000,M\n"
        )

        players = new_zealand_tournament._parse_member_csv(text)

        self.assertEqual(
            players["000123"], {"dob": "02/07/2010", "gender": "O"}
        )
        self.assertEqual(players["BAD-DATE"], {"dob": "", "gender": "F"})
        self.assertEqual(players["DECIMAL"], {"dob": "12/01/2009", "gender": ""})
        self.assertEqual(players["FUTURE"], {"dob": "", "gender": "M"})
        self.assertNotIn("BAD\ufffdID", players)
        self.assertNotIn("", players)

    def test_conflicting_duplicate_values_are_not_used(self):
        text = (
            "Surname,Name,National ID,Birth Day,Birth Month,Birth Year,Gender\n"
            "First,Player,DUPLICATE,1,2,2010,M\n"
            "Second,Player,DUPLICATE,3,4,2011,F\n"
            "Third,Player,DUPLICATE,1,2,2010,M\n"
        )

        players = new_zealand_tournament._parse_member_csv(text)

        self.assertEqual(players["DUPLICATE"], {"dob": "", "gender": ""})

    def test_member_csv_requires_the_expected_schema(self):
        with self.assertRaisesRegex(
            new_zealand_tournament.MemberRegistryError, "Birth Year, Gender"
        ):
            new_zealand_tournament._parse_member_csv(
                "Surname,Name,National ID,Birth Day,Birth Month\n"
            )


class NewZealandMemberDatabaseTests(TestCase):
    CSV_HEADER = (
        "Surname,Name,National ID,Birth Day,Birth Month,Birth Year,Gender\n"
    )

    def _csv_file(self, directory, body):
        path = Path(directory) / "members.csv"
        path.write_text(self.CSV_HEADER + body, encoding="utf-8")
        return path

    def test_loader_reads_exact_id_registry_from_database(self):
        NewZealandMember.objects.create(
            national_id="00123", dob=date(2012, 3, 2), gender="O"
        )
        logs = []

        players = new_zealand_tournament.load_player_enrichment(
            lambda level, message: logs.append((level, message)), mock.Mock()
        )

        self.assertEqual(
            players, {"00123": {"dob": "03/02/2012", "gender": "O"}}
        )
        self.assertEqual(logs, [("INFO", "Player registry ready: 1 National ID(s)")])

    def test_loader_fails_clearly_when_registry_is_empty(self):
        with self.assertRaisesRegex(
            new_zealand_tournament.MemberRegistryError,
            "import_new_zealand_members",
        ):
            new_zealand_tournament.load_player_enrichment(mock.Mock(), mock.Mock())

    def test_import_command_atomically_replaces_registry(self):
        NewZealandMember.objects.create(
            national_id="STALE", dob=date(2000, 1, 1), gender="M"
        )
        with TemporaryDirectory() as directory:
            path = self._csv_file(
                directory,
                "Example,Player,00123,2,3,2012,M\n"
                "Other,Player,ABC-9,29,2,2012,O\n",
            )
            output = StringIO()
            call_command("import_new_zealand_members", str(path), stdout=output)

        self.assertFalse(NewZealandMember.objects.filter(national_id="STALE").exists())
        self.assertEqual(NewZealandMember.objects.count(), 2)
        member = NewZealandMember.objects.get(national_id="00123")
        self.assertEqual(member.dob, date(2012, 3, 2))
        self.assertEqual(member.gender, "M")
        self.assertIn("Imported 2 New Zealand member record(s)", output.getvalue())

    def test_invalid_import_preserves_existing_registry(self):
        NewZealandMember.objects.create(
            national_id="CURRENT", dob=date(2000, 1, 1), gender="F"
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("Surname,Name,National ID\nBad,File,123\n", encoding="utf-8")
            with self.assertRaises(CommandError):
                call_command("import_new_zealand_members", str(path))

        self.assertEqual(
            list(NewZealandMember.objects.values_list("national_id", flat=True)),
            ["CURRENT"],
        )

    def test_truncated_import_is_rejected_without_allow_shrink(self):
        NewZealandMember.objects.bulk_create(
            [
                NewZealandMember(national_id=f"CURRENT-{index}", gender="M")
                for index in range(10)
            ]
        )
        with TemporaryDirectory() as directory:
            path = self._csv_file(directory, "Only,Player,ONE,1,1,2000,F\n")
            with self.assertRaisesRegex(CommandError, "--allow-shrink"):
                call_command("import_new_zealand_members", str(path))

        self.assertEqual(NewZealandMember.objects.count(), 10)


class NewZealandTournamentRunnerTests(SimpleTestCase):
    def test_config_uses_tennis_new_zealand_metadata(self):
        cfg = new_zealand_tournament.CONFIG
        self.assertEqual(cfg.base, "https://tnz.tournamentsoftware.com")
        self.assertEqual(cfg.country, "New Zealand")
        self.assertEqual(cfg.country_code, "NZL")
        self.assertEqual(cfg.import_source, "Tennis New Zealand")
        self.assertEqual(cfg.sanction_body, "Tennis New Zealand")

    def test_exact_national_id_enrichment_overrides_player_dob_and_gender(self):
        cfg = new_zealand_tournament.CONFIG
        player_url = f"{cfg.base}/sport/player.aspx?id=TOURNAMENT&player=1"
        pages = {
            player_url: """
                <div class="page-subhead"><div class="media__content">
                  <h4 class="media__title">
                    <a href="/player-profile/PLAYER-GUID">Profile</a>
                    <span class="media__title-aside">(000123)</span>
                  </h4>
                </div></div>
            """,
        }

        row = _ts_tournament._build_row(
            FakeSelectorClient(pages),
            cfg,
            {
                "draw_name": "Open Singles",
                "tournament_name": "TNZ Open",
                "player_enrichment": {
                    "000123": {"dob": "02/07/2010", "gender": "O"}
                },
            },
            {
                "draw_team_type": "Singles",
                "outcome": "Completed",
                "score": "6-3, 6-4;",
                "winner_1": {
                    "name": "Jos\u00e9 Example",
                    "profile_url": player_url,
                },
                "loser_1": {"name": "Other Player", "profile_url": ""},
            },
        )

        self.assertEqual(row["winner_1_third_party_id"], "000123")
        self.assertEqual(row["winner_1_dob"], "02/07/2010")
        self.assertEqual(row["winner_1_gender"], "O")
        self.assertEqual(row["winner_1_country"], "NZL")
        self.assertEqual(row["id_type"], "New Zealand")
        self.assertEqual(row["tournament_import_source"], "Tennis New Zealand")
        self.assertEqual(row["tournament_sanction_body"], "Tennis New Zealand")

    def test_similar_but_non_exact_id_does_not_enrich_player(self):
        cfg = new_zealand_tournament.CONFIG
        player_url = f"{cfg.base}/sport/player.aspx?id=TOURNAMENT&player=1"
        pages = {
            player_url: """
                <div class="page-subhead"><div class="media__content">
                  <h4 class="media__title">
                    <a href="/player-profile/PLAYER-GUID">Profile</a>
                    <span class="media__title-aside">(000123)</span>
                  </h4>
                </div></div>
            """,
        }

        _name, third_party_id, dob, gender, _country = _ts_tournament._parse_player(
            FakeSelectorClient(pages),
            cfg,
            "Jos\u00e9 Example",
            player_url,
            player_enrichment={
                "123": {"dob": "02/07/2010", "gender": "M"},
                "Example, Jos\u00e9": {"dob": "02/07/2010", "gender": "M"},
            },
        )

        self.assertEqual(third_party_id, "000123")
        self.assertEqual(dob, "")
        self.assertEqual(gender, "")

    def test_registry_failure_stops_before_tournament_requests(self):
        def fail_registry(_log, _tele):
            raise new_zealand_tournament.MemberRegistryError("Registry is empty")

        run_obj = SimpleNamespace(
            pk=1,
            scraper=SimpleNamespace(worker_count=1),
            params={},
            date_from=date(2026, 7, 9),
            date_to=date(2026, 7, 23),
        )

        items, requests, errors, row_count, status = _ts_tournament.run(
            new_zealand_tournament.CONFIG,
            run_obj,
            lambda *_args: None,
            player_enrichment_loader=fail_registry,
        )

        self.assertEqual(items, "")
        self.assertEqual(requests, "")
        self.assertIn("Registry is empty", errors)
        self.assertEqual(row_count, 0)
        self.assertEqual(status, Run.Status.FAILED)

    def test_wrapper_passes_member_loader_to_shared_engine(self):
        run_obj = object()
        log = mock.Mock()
        expected = ("items", "requests", "errors", 1, Run.Status.SUCCESS)

        with mock.patch.object(
            _ts_tournament, "run", return_value=expected
        ) as shared_run:
            result = new_zealand_tournament.run(run_obj, log)

        self.assertEqual(result, expected)
        shared_run.assert_called_once_with(
            new_zealand_tournament.CONFIG,
            run_obj,
            log,
            player_enrichment_loader=new_zealand_tournament.load_player_enrichment,
        )
