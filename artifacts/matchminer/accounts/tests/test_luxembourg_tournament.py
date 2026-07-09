from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.live_scrapers import _ts_tournament, luxembourg_tournament
from accounts.models import Scraper


class LuxembourgTournamentFilteringTests(SimpleTestCase):
    def test_luxembourg_excludes_padel_events_by_name(self):
        kept = _ts_tournament._filter_tournaments(
            luxembourg_tournament.CONFIG,
            [
                {"tournament_name": "Luxembourg Junior Open"},
                {"tournament_name": "Luxembourg Padel Tour"},
                {"tournament_name": "Open de PADEL Indoor"},
            ],
            lambda *_args: None,
        )

        self.assertEqual(kept, [{"tournament_name": "Luxembourg Junior Open"}])


class LuxembourgTournamentFormTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("operator", password="pass")
        self.scraper, _created = Scraper.objects.get_or_create(
            slug="luxembourg_tournament",
            defaults={
                "code": "LUX_T",
                "name": "Luxembourg Tournament",
                "tour": "FLT",
                "domain": "flt.tournamentsoftware.com",
            },
        )
        self.client.force_login(self.user)

    def test_luxembourg_defaults_to_rolling_window(self):
        response = self.client.get(reverse("scraper_detail", args=[self.scraper.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="bi_weekly_on"')
        self.assertContains(response, 'data-biweekly-toggle checked')
