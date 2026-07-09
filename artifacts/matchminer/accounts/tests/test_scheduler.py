from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts import scheduler
from accounts.models import Run, Scraper, ScraperSchedule


class _SelectParser(HTMLParser):
    def __init__(self, select_id):
        super().__init__()
        self.select_id = select_id
        self.select_attrs = None
        self.options = []
        self._in_select = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select" and attrs.get("id") == self.select_id:
            self.select_attrs = attrs
            self._in_select = True
        elif self._in_select and tag == "option":
            self.options.append(attrs)

    def handle_endtag(self, tag):
        if tag == "select" and self._in_select:
            self._in_select = False


def _select_from(response, select_id):
    parser = _SelectParser(select_id)
    parser.feed(response.content.decode())
    return parser


class SchedulerScheduleTests(TestCase):
    ITF_SCHEDULE_SLUGS = [
        "itf_juniors_tournament_software",
        "itftennis_juniors",
        "itftennis_masters",
        "itftennis_mens",
        "itftennis_womens",
    ]

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("operator", password="pass")
        self.client.force_login(self.user)
        self.itf_scraper, _created = Scraper.objects.get_or_create(
            slug="itftennis_mens",
            defaults={
                "code": "ITF_M",
                "name": "ITF Tennis Mens",
                "tour": "ITF",
                "domain": "www.itftennis.com",
            },
        )
        self.non_itf_scraper, _created = Scraper.objects.get_or_create(
            slug="ireland_tournament",
            defaults={
                "code": "IRL_T",
                "name": "Ireland Tournament",
                "tour": "Tennis Ireland",
                "domain": "ti.tournamentsoftware.com",
            },
        )

    def test_itf_schedule_tab_renders_lookback_options_with_default_15_selected(self):
        response = self.client.get(
            reverse("scraper_detail", args=[self.itf_scraper.slug]) + "?tab=schedule"
        )

        self.assertEqual(response.status_code, 200)
        lookback = _select_from(response, "schedItfLookback")
        self.assertIsNotNone(lookback.select_attrs)
        self.assertEqual(lookback.select_attrs.get("name"), "itf_lookback_days")
        self.assertEqual(
            [opt.get("value") for opt in lookback.options],
            [str(days) for days in range(5, 50, 5)],
        )
        self.assertEqual(
            [opt.get("value") for opt in lookback.options if "selected" in opt],
            ["15"],
        )

    def test_all_itf_schedule_slugs_render_lookback_dropdown(self):
        for slug in self.ITF_SCHEDULE_SLUGS:
            scraper, _created = Scraper.objects.get_or_create(
                slug=slug,
                defaults={
                    "code": slug[:16].upper(),
                    "name": slug.replace("_", " ").title(),
                    "tour": "ITF",
                    "domain": "www.itftennis.com",
                },
            )

            with self.subTest(slug=slug):
                response = self.client.get(
                    reverse("scraper_detail", args=[scraper.slug]) + "?tab=schedule"
                )

                self.assertEqual(response.status_code, 200)
                self.assertIsNotNone(
                    _select_from(response, "schedItfLookback").select_attrs
                )

    def test_saving_itf_schedule_with_30_persists_itf_lookback_days(self):
        response = self.client.post(
            reverse("scraper_detail", args=[self.itf_scraper.slug]) + "?tab=schedule",
            {
                "form": "schedule-config",
                "enabled": "on",
                "frequency": "daily",
                "time_of_day": "06:00",
                "weekday": "2",
                "day_of_month": "10",
                "timezone": "UTC",
                "itf_lookback_days": "30",
            },
        )

        self.assertEqual(response.status_code, 302)
        schedule = ScraperSchedule.objects.get(scraper=self.itf_scraper)
        self.assertEqual(schedule.itf_lookback_days, 30)

    def test_invalid_itf_schedule_lookback_falls_back_to_default_15(self):
        response = self.client.post(
            reverse("scraper_detail", args=[self.itf_scraper.slug]) + "?tab=schedule",
            {
                "form": "schedule-config",
                "enabled": "on",
                "frequency": "daily",
                "time_of_day": "06:00",
                "timezone": "UTC",
                "itf_lookback_days": "999",
            },
        )

        self.assertEqual(response.status_code, 302)
        schedule = ScraperSchedule.objects.get(scraper=self.itf_scraper)
        self.assertEqual(
            schedule.itf_lookback_days,
            ScraperSchedule.ITF_LOOKBACK_DEFAULT_DAYS,
        )

    def test_scheduler_created_itf_run_uses_selected_lookback(self):
        schedule = ScraperSchedule.objects.create(
            scraper=self.itf_scraper,
            enabled=True,
            itf_lookback_days=30,
        )

        with patch("accounts.views._dispatch_next", return_value=[]):
            scheduler._launch(self.itf_scraper, schedule.pk, due_at=timezone.now())

        today = timezone.localdate()
        run = Run.objects.get(scraper=self.itf_scraper)
        self.assertEqual(run.date_from, today - timedelta(days=30))
        self.assertEqual(run.date_to, today)
        self.assertEqual(run.params["bi_weekly"], 30)

    def test_scheduler_created_itf_juniors_tournament_software_run_uses_selected_lookback(self):
        scraper, _created = Scraper.objects.get_or_create(
            slug="itf_juniors_tournament_software",
            defaults={
                "code": "ITFJ",
                "name": "ITF Juniors TournamentSoftware",
                "tour": "ITF Juniors",
                "domain": "itfjuniors.tournamentsoftware.com",
            },
        )
        schedule = ScraperSchedule.objects.create(
            scraper=scraper,
            enabled=True,
            itf_lookback_days=30,
        )

        with patch("accounts.views._dispatch_next", return_value=[]):
            scheduler._launch(scraper, schedule.pk, due_at=timezone.now())

        today = timezone.localdate()
        run = Run.objects.get(scraper=scraper)
        self.assertEqual(run.date_from, today - timedelta(days=30))
        self.assertEqual(run.date_to, today)
        self.assertEqual(run.params["bi_weekly"], 30)

    def test_non_itf_schedule_tab_does_not_render_itf_lookback_dropdown(self):
        response = self.client.get(
            reverse("scraper_detail", args=[self.non_itf_scraper.slug])
            + "?tab=schedule"
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="itf_lookback_days"')
        self.assertIsNone(_select_from(response, "schedItfLookback").select_attrs)

    def test_daily_schedule_hides_and_disables_weekday_and_month_day_controls(self):
        response = self.client.get(
            reverse("scraper_detail", args=[self.non_itf_scraper.slug])
            + "?tab=schedule"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="rt-field sched-weekday" hidden')
        self.assertContains(response, 'class="rt-field sched-dom" hidden')
        self.assertIn("disabled", _select_from(response, "schedWeekday").select_attrs)
        self.assertIn("disabled", _select_from(response, "schedDom").select_attrs)
        self.assertContains(response, "freq.addEventListener('change', sync);")
        self.assertContains(response, "wkSelect.disabled = !showWk;")
        self.assertContains(response, "domSelect.disabled = !showDom;")
