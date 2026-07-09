from unittest.mock import patch

from django.test import TestCase

from accounts.live_scrapers import south_africa
from accounts.models import Run, SAKey, Scraper


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_json(self, url):
        return {"status": "success", "num_of_results": 0, "data": []}


class SouthAfricaRunTests(TestCase):
    def setUp(self):
        self.scraper, _created = Scraper.objects.get_or_create(
            slug="south_africa",
            defaults={
                "code": "ZA",
                "name": "South Africa Results",
                "tour": "TSA",
                "domain": "sportyhq.com",
            },
        )

    def test_explicit_done_key_is_reprocessed_not_skipped(self):
        key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        SAKey.objects.create(
            scraper=self.scraper,
            tournament_key=key,
            status=SAKey.Status.DONE,
            num_results=101,
        )
        run = Run.objects.create(
            scraper=self.scraper,
            params={"run_all": False, "keys": [key]},
        )
        logs = []

        keys = south_africa._resolve_keys(run, self.scraper, lambda level, msg: logs.append(msg))

        self.assertEqual(keys, [key])
        self.assertTrue(any("re-scraping" in msg for msg in logs))

    def test_score_gets_trailing_semicolon(self):
        row = south_africa._row_for(
            {
                "result_id": "123",
                "discipline": "Singles",
                "match_date": "2026-07-01",
                "game_scores_winner_first": "6-4, 6-2",
                "winner": 1,
                "tournament": {"name": "SA Open", "draw": {"name": "Boys"}},
                "user_1": {"first_name": "Winner", "last_name": "Player"},
                "user_2": {"first_name": "Loser", "last_name": "Player"},
            },
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

        self.assertEqual(row[7], "6-4, 6-2;")

    def test_existing_score_semicolon_is_not_duplicated(self):
        row = south_africa._row_for(
            {
                "result_id": "123",
                "game_scores_winner_first": "6-4, 6-2;",
                "winner": 1,
                "tournament": {},
                "user_1": {},
                "user_2": {},
            },
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )

        self.assertEqual(row[7], "6-4, 6-2;")

    @patch("accounts.live_scrapers.south_africa.build_proxies", return_value=None)
    @patch("accounts.live_scrapers.south_africa.ScraperClient", _FakeClient)
    def test_success_payload_with_no_rows_fails_the_run(self, _build_proxies):
        key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        SAKey.objects.create(scraper=self.scraper, tournament_key=key)
        run = Run.objects.create(
            scraper=self.scraper,
            params={"run_all": False, "keys": [key]},
        )

        _items_csv, _requests_csv, errors_csv, row_count, status = south_africa.run(
            run, lambda level, msg: None
        )

        self.assertEqual(row_count, 0)
        self.assertEqual(status, Run.Status.FAILED)
        self.assertIn("API returned success but no result rows", errors_csv)
        self.assertEqual(
            SAKey.objects.get(tournament_key=key).status,
            SAKey.Status.FAILED,
        )
