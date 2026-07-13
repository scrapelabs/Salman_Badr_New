from datetime import date, timedelta

from django.test import SimpleTestCase
from django.utils import timezone

from accounts.live_scrapers import registry, sportradar
from accounts.views import validate_run_params


class FakeTelemetry:
    def __init__(self):
        self.errors = []

    def record_error(self, message, *, level="ERROR", exc=None):
        self.errors.append((level, message, exc))


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.requests = []
        self.tele = FakeTelemetry()

    def get(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self.responses.pop(0)


def singles_summary(category_id="sr:category:3"):
    return {
        "sport_event": {
            "id": "sr:sport_event:1",
            "start_time": "2026-05-17T00:15:00+00:00",
            "sport_event_context": {
                "category": {"id": category_id, "name": "ATP"},
                "competition": {
                    "id": "sr:competition:1",
                    "name": "UTR PTT Newport Beach Men 11",
                    "gender": "men",
                    "type": "singles",
                },
                "groups": [
                    {"name": "2026 Newport Beach Men 11, 13-16 Playoff"},
                ],
                "round": {"name": "final"},
                "season": {
                    "start_date": "2026-05-11",
                    "end_date": "2026-05-17",
                },
            },
            "venue": {
                "city_name": "Newport Beach",
                "country_code": "USA",
                "country_name": "United States",
            },
            "competitors": [
                {
                    "id": "sr:competitor:111",
                    "name": "Fardanesh, Max",
                    "qualifier": "home",
                    "country_code": "USA",
                },
                {
                    "id": "sr:competitor:905015",
                    "name": "Alvarez, Marco",
                    "qualifier": "away",
                    "country_code": "USA",
                },
            ],
        },
        "sport_event_status": {
            "winner_id": "sr:competitor:905015",
            "match_status": "ended",
            "status": "closed",
            "period_scores": [
                {"number": 1, "home_score": 1, "away_score": 6},
                {"number": 2, "home_score": 2, "away_score": 6},
            ],
        },
    }


class SportRadarTests(SimpleTestCase):
    def test_scheduled_defaults_are_yesterday_to_today(self):
        inputs = validate_run_params(registry.SPECS["sportradar"], {}, webhook=True)
        today = timezone.localdate()

        self.assertEqual(inputs.date_from, today - timedelta(days=1))
        self.assertEqual(inputs.date_to, today)
        self.assertEqual(inputs.params["date_from"], (today - timedelta(days=1)).isoformat())
        self.assertEqual(inputs.params["date_to"], today.isoformat())

    def test_daily_summaries_use_header_api_key_and_paginate(self):
        first_page = [{"n": i} for i in range(sportradar.PAGE_LIMIT)]
        second_page = [{"n": "last"}]
        client = FakeClient(
            [
                FakeResponse({"summaries": first_page}),
                FakeResponse({"summaries": second_page}),
            ]
        )

        rows = sportradar._fetch_daily_summaries(client, "SECRET", date(2026, 5, 17))

        self.assertEqual(len(rows), sportradar.PAGE_LIMIT + 1)
        self.assertEqual(client.requests[0]["params"], {"start": "0", "limit": "200"})
        self.assertEqual(client.requests[1]["params"], {"start": "200", "limit": "200"})
        self.assertEqual(client.requests[0]["headers"]["x-api-key"], "SECRET")
        self.assertNotIn("SECRET", client.requests[0]["url"])

    def test_singles_row_maps_winner_score_and_profile_dobs(self):
        client = FakeClient(
            [
                FakeResponse({"info": {"date_of_birth": "2000-05-01"}}),
                FakeResponse({"info": {"date_of_birth": "2001-06-02"}}),
            ]
        )

        row = sportradar._row_from_summary(
            singles_summary(), client=client, api_key="SECRET", dob_cache={}
        )

        self.assertEqual(row["match_id"], "")
        self.assertEqual(row["id_type"], "SportRadar")
        self.assertEqual(row["draw_name"], "2026 Newport Beach Men 11, 13-16 Playoff")
        self.assertEqual(row["draw_team_type"], "Singles")
        self.assertEqual(row["tournament_name"], "UTR PTT Newport Beach Men 11")
        self.assertEqual(row["date"], "2026-05-17T00:15:00+00:00")
        self.assertEqual(row["score"], "6-1, 6-2;")
        self.assertEqual(row["winner_1_name"], "Alvarez, Marco")
        self.assertEqual(row["winner_1_gender"], "M")
        self.assertEqual(row["winner_1_dob"], "05/01/2000")
        self.assertEqual(row["winner_1_third_party_id"], "sr:competitor:905015")
        self.assertEqual(row["loser_1_name"], "Fardanesh, Max")
        self.assertEqual(row["loser_1_gender"], "M")
        self.assertEqual(row["loser_1_dob"], "06/02/2001")
        self.assertEqual(row["loser_1_third_party_id"], "sr:competitor:111")
        self.assertEqual(row["outcome"], "Completed")
        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["tournament_city"], "Newport Beach")
        self.assertEqual(row["tournament_country_code"], "USA")
        self.assertEqual(row["tournament_country"], "United States")
        self.assertEqual(row["tournament_start_date"], "05/11/2026")
        self.assertEqual(row["tournament_end_date"], "05/17/2026")

    def test_enriched_row_uses_parent_tournament_and_child_draw_suffix(self):
        summary = singles_summary()
        home = summary["sport_event"]["competitors"][0]
        home.pop("country_code", None)
        home["country"] = " Neutral "
        competition_metadata = {
            "parent": {
                "id": "sr:competition:parent",
                "name": "UTR PTT Newport Beach",
            },
            "child": {
                "id": "sr:competition:1",
                "name": "UTR PTT Newport Beach Men Singles",
                "parent_id": "sr:competition:parent",
                "type": "singles",
                "gender": "men",
            },
        }

        row = sportradar._row_from_summary(
            summary,
            competition_metadata=competition_metadata,
        )

        self.assertEqual(row["tournament_name"], "UTR PTT Newport Beach")
        self.assertEqual(row["draw_name"], "Men Singles")
        self.assertEqual(row["draw_team_type"], "Singles")
        self.assertEqual(row["draw_gender"], "Male")
        self.assertEqual(row["loser_1_country"], "")

    def test_country_value_skips_neutral_before_real_fallback(self):
        self.assertEqual(
            sportradar._country_value(" Neutral ", "", "RUS"),
            "RUS",
        )
        self.assertEqual(
            sportradar._country_value("neutral", None, "  "),
            "",
        )

    def test_enriched_row_uses_daily_child_and_keeps_empty_suffix_fallback(self):
        summary = singles_summary()
        competition = summary["sport_event"]["sport_event_context"]["competition"]
        competition["name"] = "UTR PTT Newport Beach Men Singles"
        metadata = {
            "parent": {
                "id": "sr:competition:parent",
                "name": "UTR PTT Newport Beach",
            },
            "child": {},
        }

        row = sportradar._row_from_summary(
            summary,
            competition_metadata=metadata,
        )

        self.assertEqual(row["tournament_name"], "UTR PTT Newport Beach")
        self.assertEqual(row["draw_name"], "Men Singles")

        competition["name"] = "UTR PTT Newport Beach"
        row = sportradar._row_from_summary(
            summary,
            competition_metadata=metadata,
        )

        self.assertEqual(row["draw_name"], "2026 Newport Beach Men 11, 13-16 Playoff")

    def test_disallowed_category_is_skipped(self):
        row = sportradar._row_from_summary(singles_summary("sr:category:999"))

        self.assertIsNone(row)

    def test_doubles_row_uses_embedded_players(self):
        summary = singles_summary()
        ctx = summary["sport_event"]["sport_event_context"]
        ctx["competition"]["gender"] = "women"
        ctx["competition"]["type"] = "doubles"
        summary["sport_event"]["competitors"] = [
            {
                "id": "sr:competitor:team1",
                "name": "Winner A / Winner B",
                "qualifier": "home",
                "players": [
                    {
                        "id": "sr:competitor:w1",
                        "name": "Winner, One",
                        "country_code": "USA",
                        "gender": "female",
                        "date_of_birth": "1999-01-01",
                    },
                    {
                        "id": "sr:competitor:w2",
                        "name": "Winner, Two",
                        "country_code": "CAN",
                        "gender": "female",
                        "date_of_birth": "1998-02-02",
                    },
                ],
            },
            {
                "id": "sr:competitor:team2",
                "name": "Loser A / Loser B",
                "qualifier": "away",
                "players": [
                    {"id": "sr:competitor:l1", "name": "Loser, One", "country_code": "GBR"},
                    {"id": "sr:competitor:l2", "name": "Loser, Two", "country_code": "AUS"},
                ],
            },
        ]
        summary["sport_event_status"]["winner_id"] = "sr:competitor:team1"
        summary["sport_event_status"]["period_scores"] = [
            {"number": 1, "home_score": 7, "away_score": 5},
        ]

        competition_metadata = {
            "parent": {
                "id": "sr:competition:parent",
                "name": "UTR PTT Newport Beach",
            },
            "child": {
                "id": "sr:competition:1",
                "name": "UTR PTT Newport Beach Women Doubles",
                "parent_id": "sr:competition:parent",
                "type": "doubles",
                "gender": "women",
            },
        }

        row = sportradar._row_from_summary(
            summary,
            competition_metadata=competition_metadata,
        )

        self.assertEqual(row["tournament_name"], "UTR PTT Newport Beach")
        self.assertEqual(row["draw_name"], "Women Doubles")
        self.assertEqual(row["draw_team_type"], "Doubles")
        self.assertEqual(row["draw_gender"], "Female")
        self.assertEqual(row["score"], "7-5;")
        self.assertEqual(row["winner_1_name"], "Winner, One")
        self.assertEqual(row["winner_2_name"], "Winner, Two")
        self.assertEqual(row["winner_2_third_party_id"], "sr:competitor:w2")
        self.assertEqual(row["winner_2_country"], "CAN")
        self.assertEqual(row["winner_2_dob"], "02/02/1998")
        self.assertEqual(row["loser_1_name"], "Loser, One")
        self.assertEqual(row["loser_2_name"], "Loser, Two")
