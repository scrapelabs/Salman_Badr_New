from datetime import timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Run, Scraper


class BatchJobsTableTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("operator", password="pass")
        self.scraper, _created = Scraper.objects.get_or_create(
            slug="ireland_tournament",
            defaults={
                "code": "IRL_T",
                "name": "Ireland Tournament",
                "tour": "Tennis Ireland",
                "domain": "ti.tournamentsoftware.com",
            },
        )
        self.scraper.runs.all().delete()
        self.client.force_login(self.user)

    def test_jobs_table_shows_started_and_finished_times(self):
        base = timezone.now().replace(second=0, microsecond=0)
        expected_start = timezone.localtime(base, timezone=dt_timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        expected_finish = timezone.localtime(
            base + timedelta(minutes=15), timezone=dt_timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.RUNNING,
            tournament="Running job",
            started_at=base,
        )
        Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.QUEUED,
            tournament="Queued job",
            started_at=base,
        )
        Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.SUCCESS,
            tournament="Finished job",
            started_at=base,
            finished_at=base + timedelta(minutes=15),
        )

        with timezone.override("UTC"):
            response = self.client.get(
                reverse("scraper_detail", args=[self.scraper.slug]) + "?tab=batch"
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Started")
        self.assertContains(response, "Finished")
        self.assertContains(response, "Running job")
        self.assertContains(response, "Queued job")
        self.assertContains(response, "Finished job")
        self.assertContains(response, "Waiting")
        self.assertContains(response, "Running")
        self.assertContains(response, expected_start, count=2)
        self.assertContains(response, expected_finish)

    def test_queue_events_signature_changes_when_job_finishes(self):
        started = timezone.now().replace(second=0, microsecond=0)
        run = Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.RUNNING,
            tournament="Running job",
            started_at=started,
        )
        url = reverse("queue_events", args=[self.scraper.slug])

        before = self.client.get(f"{url}?page=1&ids={run.uuid}")
        self.assertEqual(before.status_code, 200)

        run.status = Run.Status.SUCCESS
        run.finished_at = started + timedelta(minutes=5)
        run.save(update_fields=["status", "finished_at"])

        after = self.client.get(f"{url}?page=1&ids={run.uuid}")
        self.assertEqual(after.status_code, 200)
        self.assertNotEqual(before.json()["table_sig"], after.json()["table_sig"])

    def test_queue_events_do_not_select_large_run_blob_fields(self):
        run = Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.SUCCESS,
            tournament="Large finished job",
            csv_data="match,data\n" * 1000,
            requests_csv="url,status\n" * 1000,
            errors_csv="url,error\n" * 1000,
            log_text="log line\n" * 1000,
            output_size_bytes=128,
        )
        url = reverse("queue_events", args=[self.scraper.slug])

        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(f"{url}?page=1&ids={run.uuid}")

        self.assertEqual(response.status_code, 200)
        job = response.json()["jobs"][str(run.uuid)]
        self.assertTrue(job["has_csv"])
        self.assertTrue(job["has_requests"])
        self.assertTrue(job["has_errors"])
        sql = "\n".join(q["sql"] for q in ctx.captured_queries)
        # LENGTH() annotations may reference blob columns inside the database,
        # but polling must not trigger deferred-field fetches that transfer the
        # full text payloads into Python memory.
        self.assertNotIn('SELECT "accounts_run"."id", "accounts_run"."csv_data"', sql)
        self.assertNotIn(
            'SELECT "accounts_run"."id", "accounts_run"."requests_csv"', sql
        )
        self.assertNotIn(
            'SELECT "accounts_run"."id", "accounts_run"."errors_csv"', sql
        )
        self.assertNotIn('SELECT "accounts_run"."id", "accounts_run"."log_text"', sql)

    def test_url_parameters_render_as_compact_open_link(self):
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

        response = self.client.get(
            reverse("scraper_detail", args=[self.scraper.slug]) + "?tab=batch"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open link")
        self.assertContains(response, 'class="batch-param-tooltip"')
        self.assertContains(
            response,
            (
                'data-link="https://te.tournamentsoftware.com/sport/teammatch.aspx'
                '?id=E371BB26-50B1-461C-9ADF-A147ADBE272E&amp;match=18"'
            ),
        )
        self.assertContains(response, 'class="batch-param-link"')
        self.assertContains(response, 'target="_blank"')
        self.assertNotIn(
            f'<span class="batch-params-main">{tournament_url}</span>',
            response.content.decode(),
        )

    def test_calls_history_url_tournament_renders_as_compact_open_link(self):
        tournament_url = (
            "https://ti.tournamentsoftware.com/sport/tournament.aspx"
            "?id=E371BB26-50B1-461C-9ADF-A147ADBE272E"
        )
        Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.SUCCESS,
            tournament=tournament_url,
            params={"tournament_url": tournament_url},
        )

        response = self.client.get(
            reverse("scraper_detail", args=[self.scraper.slug]) + "?tab=calls"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="run-url-tooltip"')
        self.assertContains(
            response,
            (
                'data-link="https://ti.tournamentsoftware.com/sport/tournament.aspx'
                '?id=E371BB26-50B1-461C-9ADF-A147ADBE272E"'
            ),
        )
        self.assertContains(response, 'class="run-url-link"')
        self.assertContains(response, '>Open link</a>')
        self.assertContains(response, 'target="_blank"')
        self.assertNotIn(f"<td>{tournament_url}</td>", response.content.decode())
