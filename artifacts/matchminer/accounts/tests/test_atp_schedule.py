from datetime import date, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts import scheduler
from accounts.live_scrapers import _rankings, registry
from accounts.models import Run, Scraper
from accounts.views import validate_run_params


class AtpScheduleTests(TestCase):
    def setUp(self):
        self.scraper = Scraper.objects.get(slug="atptour")

    def test_weekly_schedule_is_seeded_for_monday_1pm_utc(self):
        schedule = self.scraper.schedule

        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.frequency, "weekly")
        self.assertEqual(schedule.weekday, 0)
        self.assertEqual(schedule.time_of_day, time(13, 0))
        self.assertEqual(schedule.timezone, "UTC")
        self.assertIsNotNone(schedule.next_run_at)
        self.assertEqual(schedule.next_run_at.weekday(), 0)
        self.assertEqual(schedule.next_run_at.hour, 13)
        self.assertEqual(schedule.next_run_at.minute, 0)
        self.assertEqual(schedule.next_run_at.utcoffset(), timedelta(0))

    def test_blank_scheduled_inputs_use_one_current_monday_snapshot(self):
        today = date(2026, 7, 27)
        spec = registry.get_spec(self.scraper.slug)

        with patch("accounts.views.timezone.localdate", return_value=today):
            inputs = validate_run_params(spec, {}, webhook=True)

        self.assertEqual(spec.input_kind, registry.INPUT_RANK_SNAPSHOT)
        self.assertEqual(inputs.date_from, today)
        self.assertEqual(inputs.date_to, today)
        self.assertEqual(inputs.params["single_date"], "2026-07-27")
        self.assertEqual(inputs.params["rank_type"], "both")

    def test_scheduler_enqueues_both_rankings_for_the_current_monday(self):
        today = date(2026, 7, 27)
        schedule = self.scraper.schedule

        with patch("accounts.views.timezone.localdate", return_value=today), patch(
            "accounts.views._dispatch_next", return_value=[]
        ):
            scheduler._launch(self.scraper, schedule.pk)

        run = Run.objects.get(scraper=self.scraper)
        self.assertEqual(run.status, Run.Status.QUEUED)
        self.assertIsNone(run.launched_by)
        self.assertEqual(run.date_from, today)
        self.assertEqual(run.date_to, today)
        self.assertEqual(run.params["single_date"], "2026-07-27")
        self.assertEqual(run.params["rank_type"], "both")
        self.assertEqual(_rankings.snapshot_dates(run), [today])

    def test_start_forms_match_wta_single_date_layout(self):
        user = get_user_model().objects.create_user("atp-ui-user", password="pass")
        self.client.force_login(user)

        url = reverse("scraper_detail", args=[self.scraper.slug])
        for tab in ("batch", "real-time"):
            with self.subTest(tab=tab):
                response = self.client.get(f"{url}?tab={tab}")

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'name="snapshot_date"', count=1)
                self.assertContains(response, ">Ranking date</label>", count=1)
                self.assertContains(response, 'name="rank_type"', count=1)
                self.assertNotContains(response, 'name="date_from"')
                self.assertNotContains(response, 'name="date_to"')
