from datetime import datetime, time as dtime, timedelta, timezone as dt_timezone
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


class SchedulerProcessTests(TestCase):
    def test_scheduler_runs_inside_gunicorn_process(self):
        with patch.object(
            scheduler.sys,
            "argv",
            ["gunicorn", "matchminer.wsgi:application"],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true"},
        ):
            self.assertTrue(scheduler.should_run_in_this_process())

    def test_scheduler_runs_inside_gunicorn_module_process(self):
        with patch.object(
            scheduler.sys,
            "argv",
            [
                "C:\\Python310\\Lib\\site-packages\\gunicorn\\__main__.py",
                "matchminer.wsgi:application",
            ],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true"},
        ):
            self.assertTrue(scheduler.should_run_in_this_process())

    def test_scheduler_runs_inside_runserver_child_process(self):
        with patch.object(
            scheduler.sys,
            "argv",
            ["manage.py", "runserver"],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true", "RUN_MAIN": "true"},
        ):
            self.assertTrue(scheduler.should_run_in_this_process())

    def test_scheduler_runs_inside_runserver_noreload_process(self):
        with patch.object(
            scheduler.sys,
            "argv",
            ["manage.py", "runserver", "--noreload"],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true", "RUN_MAIN": "false"},
        ):
            self.assertTrue(scheduler.should_run_in_this_process())

    def test_scheduler_skips_runserver_parent_process(self):
        with patch.object(
            scheduler.sys,
            "argv",
            ["manage.py", "runserver"],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true", "RUN_MAIN": "false"},
        ):
            self.assertFalse(scheduler.should_run_in_this_process())

    def test_scheduler_runs_inside_waitress_module_process(self):
        with patch.object(
            scheduler.sys,
            "argv",
            [
                "C:\\Python310\\Lib\\site-packages\\waitress\\__main__.py",
                "--listen=0.0.0.0:80",
                "matchminer.wsgi:application",
            ],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true"},
        ):
            self.assertTrue(scheduler.should_run_in_this_process())

    def test_scheduler_runs_inside_waitress_serve_process(self):
        with patch.object(
            scheduler.sys,
            "argv",
            [
                "waitress-serve",
                "--listen=0.0.0.0:80",
                "matchminer.wsgi:application",
            ],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true"},
        ):
            self.assertTrue(scheduler.should_run_in_this_process())

    def test_scheduler_env_disable_wins_for_waitress_process(self):
        with patch.object(
            scheduler.sys,
            "argv",
            [
                "C:\\Python310\\Lib\\site-packages\\waitress\\__main__.py",
                "--listen=0.0.0.0:80",
                "matchminer.wsgi:application",
            ],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "false"},
        ):
            self.assertFalse(scheduler.should_run_in_this_process())

    def test_scheduler_does_not_run_for_management_command_with_waitress_label(self):
        with patch.object(
            scheduler.sys,
            "argv",
            [
                "manage.py",
                "test",
                "accounts.tests.test_scheduler."
                "SchedulerProcessTests."
                "test_scheduler_runs_inside_waitress_module_process",
            ],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true"},
        ):
            self.assertFalse(scheduler.should_run_in_this_process())

    def test_scheduler_does_not_run_for_management_command_with_runserver_label(self):
        with patch.object(
            scheduler.sys,
            "argv",
            ["manage.py", "test", "runserver"],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true", "RUN_MAIN": "true"},
        ):
            self.assertFalse(scheduler.should_run_in_this_process())

    def test_scheduler_does_not_run_for_pytest_marker_named_waitress(self):
        with patch.object(
            scheduler.sys,
            "argv",
            ["pytest", "-m", "waitress"],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true"},
        ):
            self.assertFalse(scheduler.should_run_in_this_process())

    def test_scheduler_does_not_run_for_non_server_module_under_waitress_path(self):
        with patch.object(
            scheduler.sys,
            "argv",
            ["C:\\work\\waitress\\tools\\__main__.py"],
        ), patch.dict(
            scheduler.os.environ,
            {"MATCHMINER_SCHEDULER_ENABLED": "true"},
        ):
            self.assertFalse(scheduler.should_run_in_this_process())


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

    def test_schedule_page_shows_utc_date_clock_and_fixed_timezone(self):
        response = self.client.get(
            reverse("scraper_detail", args=[self.non_itf_scraper.slug])
            + "?tab=schedule"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="serverClockDate"')
        self.assertContains(response, "All schedule dates and times")
        self.assertContains(
            response,
            'id="schedTz" name="timezone" value="UTC" readonly',
        )
        self.assertNotContains(response, '<option value="America/New_York"')

    def test_schedule_save_ignores_non_utc_timezone_and_computes_in_utc(self):
        now = datetime(2026, 7, 15, 5, 30, tzinfo=dt_timezone.utc)
        with patch("accounts.views.timezone.now", return_value=now):
            response = self.client.post(
                reverse("scraper_detail", args=[self.non_itf_scraper.slug])
                + "?tab=schedule",
                {
                    "form": "schedule-config",
                    "enabled": "on",
                    "frequency": "daily",
                    "time_of_day": "06:00",
                    "timezone": "America/New_York",
                },
            )

        self.assertEqual(response.status_code, 302)
        schedule = ScraperSchedule.objects.get(scraper=self.non_itf_scraper)
        self.assertEqual(schedule.timezone, "UTC")
        self.assertEqual(schedule.time_of_day, dtime(6, 0))
        self.assertEqual(
            schedule.next_run_at,
            datetime(2026, 7, 15, 6, 0, tzinfo=dt_timezone.utc),
        )

    def test_seeded_sportradar_schedule_is_normalized_to_utc(self):
        schedule = ScraperSchedule.objects.get(scraper__slug="sportradar")
        next_run_utc = schedule.next_run_at.astimezone(dt_timezone.utc)

        self.assertEqual(schedule.timezone, "UTC")
        self.assertEqual(
            schedule.time_of_day,
            next_run_utc.time().replace(tzinfo=None),
        )
        self.assertNotIn("America/New_York", schedule.scraper.description)

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
