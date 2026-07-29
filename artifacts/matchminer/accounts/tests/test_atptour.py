import csv
from datetime import date
import io
from types import SimpleNamespace
import threading
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from parsel import Selector

from accounts.live_scrapers import _browser, atptour, registry


RANKINGS_HTML = """
<html>
  <head><title>ATP Rankings</title></head>
  <body>
    <table class="mega-table desktop-table">
      <tr>
        <td class="rank">1</td>
        <td class="player"><ul class="player-stats"><li class="name">
          <a href="/en/players/alpha/a001/overview">Alpha</a>
        </li></ul></td>
        <td class="points">1,000</td>
      </tr>
      <tr>
        <td class="rank">2</td>
        <td class="player"><ul class="player-stats"><li class="name">
          <a href="/en/players/bravo/b002/overview">Bravo</a>
        </li></ul></td>
        <td class="points">900</td>
      </tr>
    </table>
  </body>
</html>
"""


class ForbiddenCurlClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("ATP must not construct the curl HTTP client")


class FakeBrowserClient:
    instances = []
    failed_hero_ids = set()
    first_discovery_navigation_fails = False
    all_discovery_navigation_fails = False

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.failed_hero_ids = set()
        cls.first_discovery_navigation_fails = False
        cls.all_discovery_navigation_fails = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.api_tries = 2
        self.owner_thread = None
        self.selector_calls = []
        self.json_calls = []
        self.json_call_kwargs = []
        self.relaunch_count = 0
        self.closed = False
        self.is_discovery = kwargs.get("announce", True)
        type(self).instances.append(self)

    def __enter__(self):
        self.owner_thread = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self.close()

    def _assert_owner(self):
        assert self.owner_thread == threading.get_ident()

    def get_selector(self, url, **kwargs):
        self._assert_owner()
        self.selector_calls.append(url)
        if self.is_discovery:
            if type(self).all_discovery_navigation_fails:
                return None
            if (
                type(self).first_discovery_navigation_fails
                and len(self.selector_calls) == 1
            ):
                return None
        return Selector(text=RANKINGS_HTML)

    def get_json(self, url, **kwargs):
        self._assert_owner()
        self.json_calls.append(url)
        self.json_call_kwargs.append(kwargs)
        player_id = url.rstrip("/").split("/")[-1]
        if player_id in type(self).failed_hero_ids:
            return None
        return {
            "FirstName": player_id.upper(),
            "LastName": "Player",
            "NatlId": "USA",
            "BirthDate": "2000-01-02T00:00:00",
        }

    def relaunch(self):
        self._assert_owner()
        self.relaunch_count += 1
        return self

    def close(self):
        if self.owner_thread is not None:
            self._assert_owner()
        self.closed = True


@override_settings(
    SCRAPER_BROWSER_HEADLESS=True,
    SCRAPER_BROWSER_CHANNEL="chrome",
)
class AtpTourBrowserTests(SimpleTestCase):
    def setUp(self):
        FakeBrowserClient.reset()
        self.proxy = SimpleNamespace(
            is_active=True,
            address="http://proxy.invalid:8080",
            name="test-datacenter",
            kind="datacenter",
            get_kind_display=lambda: "Datacenter",
        )
        self.run = SimpleNamespace(
            pk=123,
            scraper=SimpleNamespace(
                slug="atptour",
                worker_count=2,
                proxy=self.proxy,
            ),
            date_from=date(2026, 6, 15),
            date_to=date(2026, 6, 15),
            params={"rank_type": "singles"},
        )

    def _run(self):
        with (
            patch.object(atptour, "BrowserClient", FakeBrowserClient, create=True),
            patch.object(atptour, "ScraperClient", ForbiddenCurlClient, create=True),
            patch.object(atptour, "build_proxies", return_value=None, create=True),
            patch.object(atptour, "RANK_RANGES", [(0, 100)]),
            patch.object(
                atptour._rankings,
                "snapshot_dates",
                return_value=[date(2026, 6, 15)],
            ),
            patch.object(
                atptour._rankings,
                "resolve_rank_types",
                return_value=("singles",),
            ),
            patch.object(atptour, "_is_current_week", return_value=False),
            patch.object(atptour.Run.objects, "filter"),
        ):
            return atptour.run(self.run, lambda _level, _message: None)

    def test_registry_marks_atp_as_browser_exclusive(self):
        self.assertTrue(registry.get_spec("atptour").uses_browser)

    def test_runner_uses_assigned_proxy_with_independent_patchright_clients(self):
        items_csv, _requests_csv, _errors_csv, row_count, status = self._run()

        self.assertEqual(row_count, 2)
        self.assertEqual(status, atptour.Run.Status.SUCCESS)
        self.assertIn("a001", items_csv)
        self.assertIn("b002", items_csv)
        header = next(csv.reader(io.StringIO(items_csv)))
        self.assertEqual(header[2], "Id")
        self.assertEqual(len(FakeBrowserClient.instances), 3)
        self.assertEqual(
            [client.kwargs["announce"] for client in FakeBrowserClient.instances],
            [True, False, False],
        )
        for client in FakeBrowserClient.instances:
            self.assertIs(client.kwargs["proxy"], self.proxy)
            self.assertEqual(client.kwargs["allowed_hosts"], ("www.atptour.com",))
            self.assertTrue(client.kwargs["headless"])
            self.assertEqual(client.kwargs["channel"], "chrome")
            self.assertIsNone(client.kwargs["user_data_dir"])
            self.assertFalse(client.kwargs["rotate_proxy_session"])
            self.assertFalse(client.kwargs["manage_async_unsafe"])
            self.assertEqual(client.kwargs["api_tries"], 10)
            self.assertTrue(client.closed)

        workers = FakeBrowserClient.instances[1:]
        self.assertTrue(all(client.selector_calls for client in workers))
        self.assertEqual(sum(len(client.json_calls) for client in workers), 2)
        self.assertTrue(
            all(
                kwargs["tries"] == 10
                for client in workers
                for kwargs in client.json_call_kwargs
            )
        )

    def test_transient_ranking_challenge_relaunches_without_partial_status(self):
        FakeBrowserClient.first_discovery_navigation_fails = True

        _items_csv, _requests_csv, _errors_csv, row_count, status = self._run()

        discovery = FakeBrowserClient.instances[0]
        self.assertEqual(discovery.relaunch_count, 1)
        self.assertEqual(len(discovery.selector_calls), 2)
        self.assertEqual(row_count, 2)
        self.assertEqual(status, atptour.Run.Status.SUCCESS)

    def test_persistent_top_100_challenge_exhausts_budget_and_fails(self):
        FakeBrowserClient.all_discovery_navigation_fails = True

        items_csv, _requests_csv, _errors_csv, row_count, status = self._run()

        discovery = FakeBrowserClient.instances[0]
        self.assertEqual(discovery.relaunch_count, 1)
        self.assertEqual(len(discovery.selector_calls), 2)
        self.assertEqual(len(FakeBrowserClient.instances), 1)
        self.assertEqual(items_csv, "")
        self.assertEqual(row_count, 0)
        self.assertEqual(status, atptour.Run.Status.FAILED)

    def test_final_profile_failure_still_returns_success_with_output(self):
        FakeBrowserClient.failed_hero_ids = {"b002"}

        items_csv, _requests_csv, _errors_csv, row_count, status = self._run()

        self.assertIn("a001", items_csv)
        self.assertNotIn("b002", items_csv)
        self.assertEqual(row_count, 1)
        self.assertEqual(status, atptour.Run.Status.SUCCESS)


class BrowserJsonRetryTests(SimpleTestCase):
    def make_client(self, responses):
        client = object.__new__(_browser.BrowserClient)
        client.api_tries = 4
        client.log = Mock()
        client.tele = Mock()
        client._api = Mock(side_effect=responses)
        return client

    def test_explicit_json_retries_cover_http_and_invalid_json_failures(self):
        client = self.make_client(
            [
                _browser._ApiResponse(503, b"", ""),
                _browser._ApiResponse(200, b"not-json", "not-json"),
                _browser._ApiResponse(200, b'{"ok": true}', '{"ok": true}'),
            ]
        )

        with patch.object(_browser.time, "sleep") as sleep:
            result = client.get_json("https://www.atptour.com/api/player", tries=10)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client._api.call_count, 3)
        self.assertTrue(
            all(call.kwargs["tries"] == 1 for call in client._api.call_args_list)
        )
        self.assertEqual(sleep.call_count, 2)

    def test_explicit_json_retry_budget_is_exactly_ten(self):
        client = self.make_client(
            [_browser._ApiResponse(503, b"", "") for _ in range(10)]
        )

        with patch.object(_browser.time, "sleep"):
            result = client.get_json("https://www.atptour.com/api/player", tries=10)

        self.assertIsNone(result)
        self.assertEqual(client._api.call_count, 10)
        client.tele.record_error.assert_called_once()
