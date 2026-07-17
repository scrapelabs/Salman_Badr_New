from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from accounts.live_scrapers import registry
from accounts.live_scrapers import louisiana_high_school


SINGLES_RECORD = {
    "GameUniqueID": "45a2fb7a-5df3-44df-8170-90639a9667be",
    "Date": "04/15/2026 03:10:00 PM",
    "Winner1Name": "Gallagher, Seth",
    "Winner1Gender": "M",
    "Winner1DOB": "",
    "Winner1City": "Lafayette",
    "Winner1State": "LA",
    "Winner1Country": "USA",
    "Winner2Name": "",
    "Winner2Gender": "M",
    "Winner2City": "Lafayette",
    "Winner2State": "LA",
    "Loser1Name": "Mccrory, Jackson",
    "Loser1Gender": "M",
    "Loser1DOB": "",
    "Loser1City": "Lafayette",
    "Loser1State": "LA",
    "Loser1Country": "USA",
    "Loser2Name": "",
    "Loser2Gender": "M",
    "Loser2City": "Lafayette",
    "Loser2State": "LA",
    "Score": "6-1,6-0",
    "DrawName": "Boys",
    "DrawTeamType": "Singles",
    "TournamentName": "LHSAA State Tennis Tournament",
    "TournamentStartDate": "",
    "TournamentEndDate": "",
    "TournamentCity": "",
    "TournamentState": "",
    "TournamentCountry": "",
    "LastUpdated": "5/18/2026 6:55:00 PM",
    "EventType": "Tournament",
    "Winner1PlayerID": "567790",
    "Loser1PlayerID": "533579",
    "VarsityOrJV": "Varsity",
}


class LouisianaHighSchoolTests(SimpleTestCase):
    def test_record_maps_to_matchminer_columns(self):
        row = louisiana_high_school._row_from_record(SINGLES_RECORD)

        self.assertEqual(row["match_id"], "45a2fb7a-5df3-44df-8170-90639a9667be")
        self.assertEqual(row["ball_type"], "Yellow")
        self.assertEqual(row["id_type"], "Louisiana HS")
        self.assertEqual(row["draw_name"], "Boys")
        self.assertEqual(row["draw_team_type"], "Singles")
        self.assertEqual(row["tournament_name"], "LHSAA State Tennis Tournament")
        self.assertEqual(row["date"], "2026-04-15")
        self.assertEqual(row["score"], "6-1, 6-0;")
        self.assertEqual(row["winner_1_name"], "Gallagher, Seth")
        self.assertEqual(row["winner_1_third_party_id"], "567790")
        self.assertEqual(row["loser_1_third_party_id"], "533579")
        self.assertEqual(row["winner_2_name"], "")
        self.assertEqual(row["winner_2_gender"], "")
        self.assertEqual(row["loser_2_name"], "")
        self.assertEqual(row["loser_2_gender"], "")
        self.assertEqual(row["outcome"], "Completed")
        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["tournament_state"], "LA")
        self.assertEqual(row["tournament_country_code"], "USA")
        self.assertEqual(row["tournament_country"], "USA")
        self.assertEqual(row["tournament_import_source"], "LHSAA")
        self.assertEqual(row["tournament_sanction_body"], "LHSAA")
        self.assertEqual(row["tournament_event_type"], "Tournament")
        self.assertEqual(row["tournament_start_date"], "2026-04-15")
        self.assertEqual(row["tournament_end_date"], "2026-04-15")

    def test_updatedafter_uses_feed_month_day_year_format(self):
        self.assertEqual(louisiana_high_school._date_param(date(2026, 7, 5)), "7/5/2026")

    def test_run_fetches_selected_range_once_per_gender(self):
        class FakeClient:
            requests = []

            def __init__(self, *, log, tele, proxies):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def get_json(self, url, *, params, **kwargs):
                self.requests.append(
                    (params["gender"], params["updatedafter"])
                )
                return [SINGLES_RECORD]

        run = SimpleNamespace(
            pk=123,
            scraper=SimpleNamespace(secret_value="", proxy=None),
            date_from=date(2026, 4, 15),
            date_to=date(2026, 4, 28),
            params={"gender": "both"},
        )

        with (
            patch.object(louisiana_high_school, "ScraperClient", FakeClient),
            patch.object(louisiana_high_school, "build_proxies", return_value=None),
            patch.object(louisiana_high_school, "_api_key", return_value="test-key"),
            patch.object(louisiana_high_school.Run.objects, "filter") as run_filter,
        ):
            items_csv, _requests_csv, _errors_csv, row_count, status = (
                louisiana_high_school.run(run, lambda _level, _message: None)
            )

        self.assertEqual(
            FakeClient.requests,
            [
                ("boys", "4/15/2026"),
                ("girls", "4/15/2026"),
            ],
        )
        self.assertEqual(row_count, 1)
        self.assertEqual(status, louisiana_high_school.Run.Status.SUCCESS)
        self.assertEqual(items_csv.count(SINGLES_RECORD["GameUniqueID"]), 1)
        run_filter.return_value.update.assert_any_call(
            progress_total=2,
            progress_done=0,
        )
        progress_updates = [
            call
            for call in run_filter.return_value.update.call_args_list
            if set(call.kwargs) == {"progress_done"}
        ]
        self.assertEqual(len(progress_updates), 2)

    def test_run_treats_clean_empty_feed_as_successful_header_only_csv(self):
        class EmptyClient:
            def __init__(self, *, log, tele, proxies):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def get_json(self, url, *, params, **kwargs):
                return []

        run = SimpleNamespace(
            pk=123,
            scraper=SimpleNamespace(secret_value="", proxy=None),
            date_from=date(2026, 6, 1),
            date_to=date(2026, 7, 14),
            params={"gender": "both"},
        )

        with (
            patch.object(louisiana_high_school, "ScraperClient", EmptyClient),
            patch.object(louisiana_high_school, "build_proxies", return_value=None),
            patch.object(louisiana_high_school, "_api_key", return_value="test-key"),
            patch.object(louisiana_high_school.Run.objects, "filter"),
        ):
            items_csv, _requests_csv, errors_csv, row_count, status = (
                louisiana_high_school.run(run, lambda _level, _message: None)
            )

        self.assertEqual(row_count, 0)
        self.assertEqual(status, louisiana_high_school.Run.Status.SUCCESS)
        self.assertEqual(items_csv.splitlines(), [",".join(louisiana_high_school.HEADER)])
        self.assertEqual(errors_csv, "")

    def test_feed_enforces_inclusive_played_date_range(self):
        records = [
            {
                **SINGLES_RECORD,
                "GameUniqueID": "before",
                "Date": "04/14/2026 11:59:59 PM",
            },
            {
                **SINGLES_RECORD,
                "GameUniqueID": "start",
                "Date": "04/15/2026 03:10:00 PM",
            },
            {
                **SINGLES_RECORD,
                "GameUniqueID": "end",
                "Date": "2026-04-28",
            },
            {
                **SINGLES_RECORD,
                "GameUniqueID": "after",
                "Date": "04/29/2026 12:00:00 AM",
            },
        ]

        rows = louisiana_high_school._parse_feed(
            records,
            updated_from=date(2026, 4, 15),
            updated_to=date(2026, 4, 28),
        )

        self.assertEqual(
            [(row["match_id"], row["date"]) for row in rows],
            [("start", "2026-04-15"), ("end", "2026-04-28")],
        )

    def test_registry_surfaces_gender_and_settings_api_key(self):
        spec = registry.get_spec("louisiana_high_school")

        self.assertIsNotNone(spec)
        self.assertEqual(spec.input_kind, registry.INPUT_DATE_RANGE)
        self.assertTrue(spec.feed_gender)
        self.assertFalse(spec.feed_api_key)
        self.assertEqual(spec.secret_label, "LHSAA API key")
        self.assertEqual(spec.secret_env_var, "LHSAA_API_KEY")
        self.assertEqual(spec.default_range_days, 90)
