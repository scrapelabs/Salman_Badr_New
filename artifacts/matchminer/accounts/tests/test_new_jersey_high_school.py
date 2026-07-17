from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from accounts.live_scrapers import new_jersey_high_school
from accounts.models import Run, Scraper


SAMPLE_FEED = {
    "games": {
        "games": [
            {
                "gameReportId": 1121240,
                "sportGender": "Boys",
                "gameDate": "2026-05-27T16:00:00",
                "matches": [
                    {
                        "eventResultId": 98765,
                        "eventName": "1st Singles",
                        "score": "6-1, 6-2",
                        "winner1": {
                            "firstName": "Alex",
                            "lastName": "Winner",
                            "playerId": 111,
                            "schoolName": "North High",
                            "schoolCity": "Newark",
                            "schoolState": "NJ",
                        },
                        "loser1": {
                            "firstName": "Sam",
                            "lastName": "Loser",
                            "playerId": 222,
                            "schoolName": "South High",
                            "schoolCity": "Trenton",
                            "schoolState": "NJ",
                        },
                    }
                ],
            }
        ]
    }
}
EMPTY_FEED = {"games": {"games": []}}


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def get_json(self, url, *, params, **kwargs):
        self.requests.append((url, params, kwargs))
        return self.response


class NewJerseyHighSchoolTests(SimpleTestCase):
    def test_scrape_day_falls_back_to_direct_client_when_proxy_returns_none(self):
        proxied = FakeClient(None)
        direct = FakeClient(SAMPLE_FEED)
        logs = []

        rows = new_jersey_high_school._scrape_day(
            proxied,
            "KEY",
            ("boys",),
            date(2026, 5, 27),
            lambda level, message: logs.append((level, message)),
            fallback_client=direct,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["match_id"], "1121240-98765")
        self.assertEqual(rows[0]["winner_1_name"], "Winner, Alex")
        self.assertEqual(proxied.requests[0][1]["gamedate"], "05/27/2026")
        self.assertEqual(direct.requests[0][1]["gamedate"], "05/27/2026")
        self.assertTrue(any("direct retry" in message for _, message in logs))

    def test_scrape_day_uses_longer_feed_timeout(self):
        client = FakeClient(SAMPLE_FEED)

        rows = new_jersey_high_school._scrape_day(
            client,
            "KEY",
            ("boys",),
            date(2026, 5, 27),
            lambda _level, _message: None,
        )

        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(client.requests[0][2]["timeout"], 60)


class NewJerseyHighSchoolRunTests(TestCase):
    def test_clean_empty_feed_finishes_successfully(self):
        scraper, _created = Scraper.objects.update_or_create(
            slug="new_jersey_high_school",
            defaults={
                "code": "NJHS",
                "name": "NJHS",
                "tour": "High School",
                "domain": "njschoolsports.com",
                "threads": 1,
                "proxy": None,
            },
        )
        run = Run.objects.create(
            scraper=scraper,
            date_from=date(2026, 6, 10),
            date_to=date(2026, 6, 10),
            params={"gender": "both", "api_key": "KEY"},
        )
        logs = []

        class EmptyFeedClient:
            def __init__(self, *, log, tele, proxies):
                self.tele = tele

            def get_json(self, url, *, params, **kwargs):
                self.tele.record_request(
                    url=url,
                    method="GET",
                    status=200,
                    size=23,
                    duration_ms=1,
                )
                return EMPTY_FEED

            def close(self):
                pass

        with patch.object(new_jersey_high_school, "ScraperClient", EmptyFeedClient):
            items_csv, requests_csv, errors_csv, row_count, status = (
                new_jersey_high_school.run(
                    run, lambda level, message: logs.append((level, message))
                )
            )

        self.assertEqual(items_csv, "")
        self.assertIn("http_status", requests_csv)
        self.assertEqual(errors_csv, "")
        self.assertEqual(row_count, 0)
        self.assertEqual(status, Run.Status.SUCCESS)
