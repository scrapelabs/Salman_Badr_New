from django.test import SimpleTestCase
from parsel import Selector

from accounts.live_scrapers import _itftennis


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


class FakeBrowserClient:
    def get_selector(self, _url):
        return Selector(text=MALFORMED_JSONLD_PAGE)


class ItfTennisMetadataTests(SimpleTestCase):
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
