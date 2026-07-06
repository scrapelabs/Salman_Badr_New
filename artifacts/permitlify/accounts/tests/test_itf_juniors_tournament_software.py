from datetime import date
from unittest.mock import patch

from django.test import TestCase

from accounts.live_scrapers import itf_juniors_tournament_software as itf_juniors
from accounts.models import Run, Scraper


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        pass


class _InlineExecutor:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def map(self, func, items):
        return [func(item) for item in items]


class ITFJuniorsRunTests(TestCase):
    def setUp(self):
        self.scraper, _created = Scraper.objects.get_or_create(
            slug="itf_juniors_tournament_software",
            defaults={
                "code": "ITFJ",
                "name": "ITF Juniors TournamentSoftware",
                "tour": "ITF Juniors",
                "domain": "itfjuniors.tournamentsoftware.com",
            },
        )

    def test_clean_empty_date_discovery_succeeds(self):
        run = Run.objects.create(
            scraper=self.scraper,
            date_from=date(2026, 6, 18),
            date_to=date(2026, 7, 2),
        )
        logs = []

        with patch(
            "accounts.live_scrapers.itf_juniors_tournament_software.build_proxies",
            return_value=None,
        ), patch(
            "accounts.live_scrapers.itf_juniors_tournament_software.ScraperClient",
            _FakeClient,
        ), patch(
            "accounts.live_scrapers.itf_juniors_tournament_software._warm_up",
            lambda client: None,
        ), patch(
            "accounts.live_scrapers.itf_juniors_tournament_software._discover_range",
            return_value=[],
        ):
            items_csv, _requests_csv, errors_csv, row_count, status = itf_juniors.run(
                run, lambda level, msg: logs.append(msg)
            )

        self.assertEqual(row_count, 0)
        self.assertEqual(status, Run.Status.SUCCESS)
        self.assertEqual(items_csv, "")
        self.assertEqual(errors_csv, "")
        self.assertTrue(any("successful empty run" in msg for msg in logs))

    def test_discovered_tournament_with_no_rows_still_fails(self):
        run = Run.objects.create(
            scraper=self.scraper,
            date_from=date(2026, 4, 1),
            date_to=date(2026, 4, 30),
        )
        tournament = {
            "tournament_id": "abc",
            "tournament_name": "Empty Tournament",
            "tournament_url": "https://itfjuniors.tournamentsoftware.com/tournament/abc",
        }

        with patch(
            "accounts.live_scrapers.itf_juniors_tournament_software.build_proxies",
            return_value=None,
        ), patch(
            "accounts.live_scrapers.itf_juniors_tournament_software.ScraperClient",
            _FakeClient,
        ), patch(
            "accounts.live_scrapers.itf_juniors_tournament_software._warm_up",
            lambda client: None,
        ), patch(
            "accounts.live_scrapers.itf_juniors_tournament_software._discover_range",
            return_value=[tournament],
        ), patch(
            "accounts.live_scrapers.itf_juniors_tournament_software._scrape_tournament",
            return_value=[],
        ), patch(
            "accounts.live_scrapers.itf_juniors_tournament_software.ThreadPoolExecutor",
            _InlineExecutor,
        ):
            _items_csv, _requests_csv, _errors_csv, row_count, status = itf_juniors.run(
                run, lambda level, msg: None
            )

        self.assertEqual(row_count, 0)
        self.assertEqual(status, Run.Status.FAILED)
