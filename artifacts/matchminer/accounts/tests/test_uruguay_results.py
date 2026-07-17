from unittest.mock import patch

from django.test import TestCase
from parsel import Selector

from accounts.live_scrapers import uruguay_results
from accounts.models import Proxy, Run, Scraper


class UruguayResultsTests(TestCase):
    def test_unready_tournament_panel_logs_reason(self):
        logs = []
        html = """
        <html><body>
          <p>Aun no existen juegos calculados! Los cuadros aun no estan listos.</p>
        </body></html>
        """

        empty_reasons = []
        rows = uruguay_results._parse_category(
            client=None,
            tournament_url="https://uruguay.tenisintegrado.com/torneio_painel_info/index/882",
            sel=Selector(text=html),
            log=lambda level, msg: logs.append((level, msg)),
            empty_reasons=empty_reasons,
        )

        self.assertEqual(rows, [])
        self.assertEqual(empty_reasons, ["games are not calculated yet"])
        self.assertTrue(any("games are not calculated yet" in msg for _level, msg in logs))

    def test_unready_tournaments_are_healthy_empty_run(self):
        scraper = Scraper.objects.create(
            slug="uruguay_results_test",
            code="URG",
            name="Uruguay Results",
            tour="AUT",
            domain="uruguay.tenisintegrado.com",
            threads=1,
        )
        run = Run.objects.create(
            scraper=scraper,
            params={"year": 2026, "month": 7},
        )
        logs = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def close(self):
                pass

        def fake_scrape(_client, _tournament_url, log=None, empty_reasons=None):
            if empty_reasons is not None:
                empty_reasons.append("games are not calculated yet")
            return []

        with patch.object(uruguay_results, "build_proxies", return_value={}), patch.object(
            uruguay_results, "ScraperClient", FakeClient
        ), patch.object(
            uruguay_results,
            "_discover_tournaments",
            return_value=[
                "https://uruguay.tenisintegrado.com/torneio_painel_info/index/882"
            ],
        ), patch.object(uruguay_results, "_scrape_tournament", side_effect=fake_scrape):
            items_csv, _requests_csv, errors_csv, row_count, status = uruguay_results.run(
                run, lambda level, msg: logs.append((level, msg))
            )

        self.assertEqual(status, Run.Status.SUCCESS)
        self.assertEqual(row_count, 0)
        self.assertEqual(items_csv, "")
        self.assertEqual(errors_csv, "")
        self.assertTrue(any("healthy empty run" in msg for _level, msg in logs))

    def test_configured_proxy_is_bypassed_for_stable_direct_route(self):
        proxy = Proxy.objects.create(
            name="Blocked rotating proxy",
            kind=Proxy.Kind.DATACENTER,
            address="http://proxy.example",
        )
        scraper = Scraper.objects.create(
            slug="uruguay_results_proxy_fallback_test",
            code="URGF",
            name="Uruguay Results Proxy Fallback",
            tour="AUT",
            domain="uruguay.tenisintegrado.com",
            threads=1,
            proxy=proxy,
        )
        run = Run.objects.create(
            scraper=scraper,
            params={"year": 2026, "month": 7},
        )
        logs = []

        def fake_discover(_client, _year, _month, _log):
            return [
                "https://uruguay.tenisintegrado.com/torneio_painel_info/index/882"
            ]

        def fake_scrape(_client, _tournament_url, log=None, empty_reasons=None):
            empty_reasons.append("games are not calculated yet")
            return []

        with patch.object(
            uruguay_results, "build_proxies"
        ) as build_proxies, patch.object(
            uruguay_results, "ScraperClient"
        ) as client_cls, patch.object(
            uruguay_results, "_discover_tournaments", side_effect=fake_discover
        ), patch.object(
            uruguay_results, "_scrape_tournament", side_effect=fake_scrape
        ):
            client_cls.return_value.__enter__.return_value = client_cls.return_value
            _items_csv, _requests_csv, errors_csv, row_count, status = (
                uruguay_results.run(
                    run, lambda level, msg: logs.append((level, msg))
                )
            )

        build_proxies.assert_not_called()
        self.assertEqual(status, Run.Status.SUCCESS)
        self.assertEqual(row_count, 0)
        self.assertEqual(errors_csv, "")
        self.assertEqual(
            [call.kwargs["proxies"] for call in client_cls.call_args_list],
            [None, None],
        )
        self.assertTrue(
            any("using direct connection" in msg for _level, msg in logs)
        )
