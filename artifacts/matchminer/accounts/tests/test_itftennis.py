import threading
from unittest import mock

from django.test import SimpleTestCase
from parsel import Selector

from accounts.live_scrapers import _http, _itftennis, itftennis_masters


MALFORMED_JSONLD_PAGE = """
<html>
  <head>
    <script type="application/ld+json">
      {
        "@context": "https://www.schema.org",
        "@type": "Event",
        "name": "W35 San Gregorio",
        "url": "/en/tournament/w35-san-gregorio/ita/2026/w-itf-ita-2026-021/",
        "description": "First paragraph.
Second paragraph.",
        "startDate": "6/15/2026 12:00:00 AM",
        "endDate": "6/21/2026 12:00:00 AM",
        "location": {
          "name": "San Gregorio",
          "address": {"addressCountry": "Italy"}
        }
      }
    </script>
  </head>
  <body>
    <h1 id="ga__tournament-name">W35 San Gregorio</h1>
  </body>
</html>
"""

TEAM_EVENT_PAGE = """
<html>
  <head>
    <script type="application/ld+json">
      {
        "@type": "Event",
        "startDate": "7/5/2026 12:00:00 AM",
        "endDate": "7/10/2026 12:00:00 AM",
        "location": {
          "name": "Rome",
          "address": {"addressCountry": "Italy"}
        }
      }
    </script>
  </head>
  <body>
    <h1 id="ga__tournament-name">2026 Masters World Team Championships</h1>
    <span id="ga__tournament-surface">Clay - Outdoor</span>
  </body>
</html>
"""

TEAM_FILTERS = {
    "tournamentId": 2100001160,
    "tourType": "T",
    "filters": [
        {
            "dataName": "ageCategoryCode",
            "valueCode": "V50",
            "valueDesc": "50+",
            "subFilter": [
                {
                    "dataName": "playerTypeCode",
                    "valueCode": "M",
                    "valueDesc": "Men's",
                    "subFilter": [
                        {
                            "dataName": "eventClassificationCode",
                            "valueCode": "M",
                            "valueDesc": "Main Draw",
                            "subFilter": [
                                {
                                    "dataName": "drawsheetStructureCode",
                                    "valueCode": "RR",
                                    "valueDesc": "Round Robin",
                                    "subFilter": None,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}

TEAM_TIE = {
    "tieId": 100319849,
    "side1NationCode": "ESP",
    "side1NationName": "Spain",
    "side2NationCode": "CZE",
    "side2NationName": "Czechia",
    "playStatusDesc": "Played and completed",
    "WinningSide": 1,
}

TEAM_DRAWSHEET = {
    "drawsheetStructure": "RR",
    "rrGroups": [
        {
            "groupName": "Group A",
            "nationTeams": [
                {
                    "nationCode": "ESP",
                    "ties": [
                        TEAM_TIE,
                        {
                            "tieId": 100319999,
                            "side1NationCode": "ESP",
                            "side2NationCode": "ITA",
                            "playStatusDesc": "Not started",
                        },
                    ],
                },
                {"nationCode": "CZE", "ties": [TEAM_TIE]},
            ],
        }
    ],
}

TEAM_MATCHES = [
    {
        "matchId": 1183602567,
        "ageCategory": "50+",
        "eventDesc": "Men's ",
        "roundGroupDesc": "Group A",
        "eventClassificationCode": "M",
        "teams": [
            {
                "players": [
                    {"givenName": "Jonathan", "familyName": "Garcia", "nationality": "ESP"}
                ],
                "scores": [{"score": 6}, {"score": 6}],
                "isWinner": True,
                "tieNationCode": "ESP",
            },
            {
                "players": [
                    {"givenName": "Vit", "familyName": "Subert", "nationality": "CZE"}
                ],
                "scores": [{"score": 0}, {"score": 0}],
                "isWinner": False,
                "tieNationCode": "CZE",
            },
        ],
        "playStatusDesc": "Played and completed",
    },
    {
        "matchId": 1183602569,
        "ageCategory": "50+",
        "eventDesc": "Men's ",
        "roundGroupDesc": "Group A",
        "eventClassificationCode": "M",
        "teams": [
            {
                "players": [
                    {"givenName": "Mario", "familyName": "Perea", "nationality": "ESP"},
                    {"givenName": "Manuel", "familyName": "Sala", "nationality": "ESP"},
                ],
                "scores": [{"score": 6}, {"score": 6}],
                "isWinner": True,
                "tieNationCode": "ESP",
            },
            {
                "players": [
                    {"givenName": "Antonin", "familyName": "Jerabek", "nationality": "CZE"},
                    {"givenName": "David", "familyName": "Zmrzly", "nationality": "CZE"},
                ],
                "scores": [{"score": 2}, {"score": 2}],
                "isWinner": False,
                "tieNationCode": "CZE",
            },
        ],
        "playStatusDesc": "Played and completed",
    },
]


class FakeBrowserClient:
    def get_selector(self, _url):
        return Selector(text=MALFORMED_JSONLD_PAGE)


class FakeTeamBrowserClient:
    def get_selector(self, _url):
        return Selector(text=TEAM_EVENT_PAGE)

    def get_json(self, url, params=None, headers=None):
        if "GetEventFilters" in url:
            return TEAM_FILTERS
        if "GetDrawsheet" in url:
            return TEAM_DRAWSHEET
        raise AssertionError(f"Browser should not fetch API URL: {url}")


class FakeTeamApiClient:
    def __init__(self):
        self.tie_requests = []
        self.order_requests = []

    def get_json(self, url, params=None, headers=None):
        del headers
        if "GetEventFilters" in url:
            return TEAM_FILTERS
        if "GetDrawsheet" in url:
            return TEAM_DRAWSHEET
        if "GetOrderOfPlayDays" in url:
            return [{"orderOfPlayDayId": 2595711}]
        if "GetOrderOfPlay" in url:
            self.order_requests.append(params)
            return [{"courtName": "Court 1", "matches": TEAM_MATCHES}]
        if "GetTieMatches" in url:
            self.tie_requests.append(params)
            return TEAM_MATCHES
        raise AssertionError(f"Unexpected API URL: {url}")


class BlockedDirectClient:
    get_calls = 0

    def __init__(self, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass

    def get_json(self, _url, **_kwargs):
        return None

    def get(self, _url, **_kwargs):
        type(self).get_calls += 1
        return None


class ClearedDirectBrowser:
    def __init__(self, **_kwargs):
        self.pages = []
        self.api_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        pass

    def get_selector(self, url):
        self.pages.append(url)
        return Selector(text=TEAM_EVENT_PAGE)

    def get_json(self, _url, **_kwargs):
        self.api_calls += 1
        return TEAM_MATCHES


class ItfTennisMetadataTests(SimpleTestCase):
    def test_http_200_incapsula_page_is_detected_as_a_challenge(self):
        response = type(
            "Response",
            (),
            {
                "status_code": 200,
                "text": "Request unsuccessful. Incapsula incident ID: 123",
            },
        )()
        client = _http.ScraperClient(log=lambda *_args: None, tele=None)

        self.assertTrue(client._is_challenge(response))

    def test_direct_api_client_falls_back_to_cleared_browser(self):
        browser = ClearedDirectBrowser()
        with mock.patch.object(_itftennis, "ScraperClient", BlockedDirectClient):
            with _itftennis._DirectApiClient(
                lambda *_args: None,
                None,
                "https://www.itftennis.com/team-event/",
                browser,
            ) as client:
                matches = client.get_json("https://www.itftennis.com/ties")

                self.assertEqual(len(matches), 2)
                self.assertEqual(browser.api_calls, 1)
                self.assertTrue(client.http_blocked)

    def test_direct_api_client_stops_optional_dob_calls_after_challenge(self):
        BlockedDirectClient.get_calls = 0
        browser = ClearedDirectBrowser()
        with mock.patch.object(_itftennis, "ScraperClient", BlockedDirectClient):
            with _itftennis._DirectApiClient(
                lambda *_args: None,
                None,
                "https://www.itftennis.com/team-event/",
                browser,
            ) as client:
                self.assertIsNone(client.get("https://www.itftennis.com/player/1"))
                self.assertIsNone(client.get("https://www.itftennis.com/player/2"))

                self.assertEqual(BlockedDirectClient.get_calls, 1)
                self.assertEqual(browser.api_calls, 0)
                self.assertTrue(client.optional_http_blocked)

    def test_jsonld_accepts_unescaped_newlines_in_source_description(self):
        data = _itftennis._jsonld(Selector(text=MALFORMED_JSONLD_PAGE))

        self.assertEqual(data.get("name"), "W35 San Gregorio")
        self.assertEqual(data.get("startDate"), "6/15/2026 12:00:00 AM")
        self.assertEqual(data.get("endDate"), "6/21/2026 12:00:00 AM")
        self.assertEqual(
            _itftennis._to_mdy(data["startDate"], "%m/%d/%Y %I:%M:%S %p"),
            "06/15/2026",
        )

    def test_single_url_discovery_uses_canonical_jsonld_tournament_id(self):
        supplied_url = (
            "https://www.itftennis.com/en/tournament/w35-san-gregorio/ita/"
            "2026/w-itf-ita-2026-021/draws-and-results/"
        )

        tournaments = _itftennis._discover_one(
            FakeBrowserClient(), supplied_url, lambda _level, _message: None
        )

        self.assertEqual(
            tournaments,
            [
                {
                    "tournament_id": "w-itf-ita-2026-021",
                    "tournament_url": (
                        "https://www.itftennis.com/en/tournament/"
                        "w35-san-gregorio/ita/2026/w-itf-ita-2026-021/"
                    ),
                }
            ],
        )

    def test_team_event_expands_each_tie_into_singles_and_doubles_rubbers(self):
        client = FakeTeamBrowserClient()
        api_client = FakeTeamApiClient()
        rows = []

        emitted = _itftennis._scrape_tournament(
            client,
            itftennis_masters.CONFIG,
            {
                "tournament_id": "s-gc1-ita-05a-2026",
                "tournament_url": "https://www.itftennis.com/team-event/",
            },
            lambda row: rows.append(row) or True,
            lambda _level, _message: None,
            {},
            threading.Lock(),
            api_client=api_client,
        )

        self.assertEqual(emitted, 2)
        self.assertEqual(
            api_client.tie_requests,
            [],
        )
        self.assertEqual(
            api_client.order_requests,
            [{"orderOfPlayDayId": "2595711"}],
        )
        self.assertEqual([row["match_id"] for row in rows], [1183602567, 1183602569])
        self.assertEqual([row["round"] for row in rows], ["Group A", "Group A"])
        self.assertEqual(
            [row["draw_team_type"] for row in rows], ["Singles", "Doubles"]
        )
        self.assertEqual(rows[0]["winner_1_name"], "Garcia, Jonathan")
        self.assertEqual(rows[1]["winner_2_name"], "Sala, Manuel")
        self.assertEqual(rows[0]["outcome"], "Completed")

    def test_team_ties_retain_knockout_round(self):
        tie = {**TEAM_TIE, "tieId": 100319882}

        ties = _itftennis._extract_team_ties(
            {
                "koGroups": [
                    {"rounds": [{"roundDesc": "Final", "ties": [tie]}]}
                ]
            }
        )

        self.assertEqual(ties, [(tie, "Final")])
