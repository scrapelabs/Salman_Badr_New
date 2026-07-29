from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from accounts import scheduler
from accounts.live_scrapers import australia_tennis, registry
from accounts.models import Run, ScheduleEvent, Scraper
from accounts.views import validate_run_params


class AustraliaScheduleTests(TestCase):
    def setUp(self):
        self.scraper = Scraper.objects.get(slug="australia_tennis")

    def test_daily_schedule_is_seeded_for_6am_utc(self):
        schedule = self.scraper.schedule

        self.assertTrue(schedule.enabled)
        self.assertEqual(schedule.frequency, "daily")
        self.assertEqual(schedule.time_of_day, time(6, 0))
        self.assertEqual(schedule.timezone, "UTC")
        self.assertIsNotNone(schedule.next_run_at)
        self.assertEqual(schedule.next_run_at.hour, 6)
        self.assertEqual(schedule.next_run_at.minute, 0)
        self.assertEqual(schedule.next_run_at.utcoffset(), timedelta(0))

    def test_blank_scheduled_inputs_use_yesterday_through_today(self):
        today = date(2026, 7, 25)
        spec = registry.get_spec(self.scraper.slug)

        with patch("accounts.views.timezone.localdate", return_value=today):
            inputs = validate_run_params(spec, {}, webhook=True)

        self.assertEqual(spec.default_range_days, 1)
        self.assertEqual(inputs.date_from, today - timedelta(days=1))
        self.assertEqual(inputs.date_to, today)
        self.assertEqual(inputs.params["date_from"], "2026-07-24")
        self.assertEqual(inputs.params["date_to"], "2026-07-25")

    def test_scheduler_enqueues_the_two_day_window(self):
        today = date(2026, 7, 25)
        schedule = self.scraper.schedule

        with patch("accounts.views.timezone.localdate", return_value=today), patch(
            "accounts.views._dispatch_next", return_value=[]
        ):
            scheduler._launch(self.scraper, schedule.pk)

        run = Run.objects.get(scraper=self.scraper)
        self.assertEqual(run.status, Run.Status.QUEUED)
        self.assertEqual(run.date_from, today - timedelta(days=1))
        self.assertEqual(run.date_to, today)
        self.assertEqual(run.params["date_from"], "2026-07-24")
        self.assertEqual(run.params["date_to"], "2026-07-25")

    def test_due_tick_launches_and_advances_the_daily_schedule(self):
        today = date(2026, 7, 25)
        now = datetime(2026, 7, 25, 23, 59, tzinfo=timezone.utc)
        due_at = now - timedelta(seconds=1)
        schedule = self.scraper.schedule
        schedule.next_run_at = due_at
        schedule.save(update_fields=["next_run_at", "updated_at"])

        with patch("django.db.close_old_connections"), patch(
            "accounts.views.timezone.localdate", return_value=today
        ), patch("accounts.views._dispatch_next", return_value=[]):
            scheduler.tick(now=now)

        run = Run.objects.get(scraper=self.scraper)
        event = ScheduleEvent.objects.get(scraper=self.scraper)
        schedule.refresh_from_db()

        self.assertEqual(run.status, Run.Status.QUEUED)
        self.assertIsNone(run.launched_by)
        self.assertEqual(run.date_from, date(2026, 7, 24))
        self.assertEqual(run.date_to, today)
        self.assertEqual(event.outcome, ScheduleEvent.Outcome.QUEUED)
        self.assertEqual(event.scheduled_for, due_at)
        self.assertEqual(event.run, run)
        self.assertEqual(schedule.last_run, run)
        self.assertEqual(schedule.last_fired_at, now)
        self.assertEqual(
            schedule.next_run_at,
            datetime(2026, 7, 26, 6, 0, tzinfo=timezone.utc),
        )


class AustraliaBlobListingTests(SimpleTestCase):
    def test_listing_queries_only_the_requested_date_prefixes(self):
        def page(name, marker=""):
            return SimpleNamespace(
                status_code=200,
                text=(
                    "<EnumerationResults><Blobs><Blob>"
                    f"<Name>{name}</Name><Properties>"
                    "<Last-Modified>Sat, 25 Jul 2026 00:00:00 GMT</Last-Modified>"
                    "</Properties></Blob></Blobs>"
                    f"<NextMarker>{marker}</NextMarker></EnumerationResults>"
                ),
            )

        client = Mock()
        client.get.side_effect = [
            page("tennis_australia/20260724_000000000.json", "page-2"),
            page("tennis_australia/20260724_010000000.json"),
            page("tennis_australia/20260725_000000000.json"),
        ]
        messages = []

        with patch.object(australia_tennis, "LIST_PROGRESS_EVERY_PAGES", 1):
            blobs = australia_tennis._list_blobs(
                client,
                "https://example.blob.core.windows.net/result-submissions",
                [("sig", "not-a-real-secret")],
                date(2026, 7, 24),
                date(2026, 7, 25),
                lambda level, message: messages.append((level, message)),
            )

        request_params = [dict(call.kwargs["params"]) for call in client.get.call_args_list]
        self.assertEqual(
            [params["prefix"] for params in request_params],
            [
                "tennis_australia/20260724",
                "tennis_australia/20260724",
                "tennis_australia/20260725",
            ],
        )
        self.assertNotIn("marker", request_params[0])
        self.assertEqual(request_params[1]["marker"], "page-2")
        self.assertNotIn("marker", request_params[2])
        self.assertEqual(len(blobs), 3)
        self.assertTrue(any("so far" in message for _level, message in messages))
