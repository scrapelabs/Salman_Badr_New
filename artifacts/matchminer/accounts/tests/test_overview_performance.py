from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Run, Scraper
from accounts.views import OVERVIEW_RECENT_RUN_LIMIT


class OverviewPerformanceTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("operator", password="pass")
        self.scraper = Scraper.objects.create(
            slug="overview_perf_scraper",
            code="OVP",
            name="Overview Perf Scraper",
            tour="QA",
            domain="example.com",
        )
        self.client.force_login(self.user)

    def test_overview_caps_recent_runs(self):
        base = timezone.now()
        total = OVERVIEW_RECENT_RUN_LIMIT + 5
        for idx in range(total):
            Run.objects.create(
                scraper=self.scraper,
                status=Run.Status.SUCCESS,
                tournament=f"Run {idx}",
                started_at=base - timedelta(minutes=idx),
            )

        response = self.client.get(reverse("overview"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["recent_runs_data"]), OVERVIEW_RECENT_RUN_LIMIT)
        self.assertContains(response, "Run 0")
        self.assertNotContains(response, f"Run {total - 1}")

    def test_live_stats_caps_recent_runs(self):
        base = timezone.now()
        total = OVERVIEW_RECENT_RUN_LIMIT + 5
        for idx in range(total):
            Run.objects.create(
                scraper=self.scraper,
                status=Run.Status.SUCCESS,
                tournament=f"Run {idx}",
                started_at=base - timedelta(minutes=idx),
            )

        response = self.client.get(reverse("live_stats"))

        self.assertEqual(response.status_code, 200)
        recent = response.json()["overview"]["recent_runs"]
        self.assertEqual(len(recent), OVERVIEW_RECENT_RUN_LIMIT)
        self.assertEqual(recent[0]["tournament"], "Run 0")
        self.assertEqual(recent[-1]["tournament"], f"Run {OVERVIEW_RECENT_RUN_LIMIT - 1}")

    def test_recent_tournament_url_renders_as_compact_open_link(self):
        tournament_url = (
            "https://te.tournamentsoftware.com/sport/teammatch.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&match=18"
        )
        Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.SUCCESS,
            tournament=tournament_url,
            params={"tournament_url": tournament_url},
        )

        response = self.client.get(reverse("overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="run-url-tooltip"')
        self.assertContains(
            response,
            (
                'data-link="https://te.tournamentsoftware.com/sport/teammatch.aspx'
                '?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;match=18"'
            ),
        )
        self.assertContains(response, 'class="run-url-link"')
        self.assertContains(response, '>Open link</a>')
        self.assertContains(response, 'target="_blank"')

        live = self.client.get(reverse("live_stats"))

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["overview"]["recent_runs"][0]["tournament_url"], tournament_url)

    def test_overview_and_live_stats_do_not_load_run_blob_fields(self):
        Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.SUCCESS,
            tournament="Large output run",
            csv_data="match,data\n" * 1000,
            log_text="log line\n" * 1000,
            requests_csv="url,status\n" * 1000,
            errors_csv="url,error\n" * 1000,
        )

        for url_name in ("overview", "live_stats"):
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(reverse(url_name))

            self.assertEqual(response.status_code, 200)
            sql = "\n".join(q["sql"] for q in ctx.captured_queries)
            self.assertNotIn('"accounts_run"."csv_data"', sql)
            self.assertNotIn('"accounts_run"."log_text"', sql)
            self.assertNotIn('"accounts_run"."requests_csv"', sql)
            self.assertNotIn('"accounts_run"."errors_csv"', sql)
