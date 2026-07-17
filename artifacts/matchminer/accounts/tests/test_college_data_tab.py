from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import CollegeMatch, Scraper


class CollegeDataTabTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("operator", password="pass")
        self.scraper, _created = Scraper.objects.get_or_create(
            slug="college_dual_match",
            defaults={
                "code": "COLL",
                "name": "College Dual Match",
                "tour": "College",
                "domain": "example.edu",
            },
        )
        self.client.force_login(self.user)

    def test_data_tab_uses_scoped_panel_layout(self):
        CollegeMatch.objects.create(
            match_hash="college-data-tab-row",
            tournament_name="College Invitational",
            winner_team="Home College",
            loser_team="Away College",
            data={"score": "4-3"},
        )

        response = self.client.get(
            reverse("scraper_detail", args=[self.scraper.slug]) + "?tab=data"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "?v=20260715-responsive")
        self.assertContains(response, 'class="data-tab-panel"')
        self.assertContains(response, 'class="stat-cards data-stat-cards"')
        self.assertContains(response, "Match database")
        self.assertContains(response, "Download by date")
        self.assertContains(response, "College Invitational")
