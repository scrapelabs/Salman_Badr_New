import csv
import io
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from accounts.live_scrapers import _rankings, wtatennis


class _WtaClient:
    def __init__(self, **kwargs):
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_json(self, _url):
        self.calls += 1
        if self.calls > 1:
            return []
        return [
            {
                "player": {
                    "id": "wta-123",
                    "firstName": "Test",
                    "lastName": "Player",
                    "dateOfBirth": "2000-01-02",
                    "countryCode": "BEL",
                },
                "points": 100,
                "ranking": 10,
                "rankedAt": "2026-07-20T00:00:00Z",
            }
        ]


class RankingsHeaderTests(SimpleTestCase):
    def test_default_header_remains_player_id_for_other_ranking_scrapers(self):
        output = _rankings.RankingsCsv()
        output.add({"player_id": "padel-123"})

        rows = list(csv.reader(io.StringIO(output.value())))
        self.assertEqual(rows[0][2], "Player Id")
        self.assertEqual(rows[1][2], "padel-123")

    def test_wta_header_uses_id_without_changing_player_value(self):
        run_obj = SimpleNamespace(
            scraper=SimpleNamespace(proxy=None),
            params={"single_date": "2026-07-20", "rank_type": "singles"},
            date_from=date(2026, 7, 20),
        )
        with mock.patch.object(
            wtatennis, "build_proxies", return_value=None
        ), mock.patch.object(
            wtatennis, "ScraperClient", _WtaClient
        ), mock.patch.object(
            wtatennis._rankings,
            "resolve_rank_types",
            return_value=("singles",),
        ):
            items_csv, _requests, _errors, count, status = wtatennis.run(
                run_obj,
                lambda *_args: None,
            )

        rows = list(csv.reader(io.StringIO(items_csv)))
        self.assertEqual(count, 1)
        self.assertEqual(status, wtatennis.Run.Status.SUCCESS)
        self.assertEqual(rows[0][2], "Id")
        self.assertEqual(rows[1][2], "wta-123")
