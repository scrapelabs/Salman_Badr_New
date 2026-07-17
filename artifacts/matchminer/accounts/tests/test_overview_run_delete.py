from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Run, Scraper


class OverviewRunDeleteTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            "admin", "admin@example.com", "pass"
        )
        self.member = User.objects.create_user("member", password="pass")
        self.scraper = Scraper.objects.create(
            slug="overview_delete_scraper",
            code="OVD",
            name="Overview Delete Scraper",
            tour="QA",
            domain="example.com",
        )
        self.other_scraper = Scraper.objects.create(
            slug="overview_delete_other",
            code="OVO",
            name="Overview Delete Other",
            tour="QA",
            domain="example.org",
        )
        self.run = Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.SUCCESS,
            tournament="Finished run",
        )
        self.other_run = Run.objects.create(
            scraper=self.other_scraper,
            status=Run.Status.FAILED,
            tournament="Other scraper run",
        )
        self.running_run = Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.RUNNING,
            tournament="Running run",
        )

    def test_recently_active_delete_controls_are_admin_only(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("overview"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "overviewBulkDeleteForm")
        self.assertNotContains(response, "Delete selected")
        self.assertNotContains(response, "Delete run #")

        self.client.force_login(self.admin)
        response = self.client.get(reverse("overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "overviewBulkDeleteForm")
        self.assertContains(response, "Delete selected")
        self.assertContains(response, f"Delete run #{self.run.short_id}")

    def test_member_cannot_delete_overview_run_by_posting_directly(self):
        self.client.force_login(self.member)

        response = self.client.post(reverse("overview_run_delete", args=[self.run.uuid]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Run.objects.filter(pk=self.run.pk).exists())

    def test_member_cannot_bulk_delete_overview_runs_by_posting_directly(self):
        self.client.force_login(self.member)

        response = self.client.post(
            reverse("overview_runs_bulk_delete"),
            {"run_uuids": [str(self.run.uuid)]},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Run.objects.filter(pk=self.run.pk).exists())

    def test_member_cannot_use_scraper_detail_delete_endpoints(self):
        self.client.force_login(self.member)

        single = self.client.post(
            reverse("run_delete", args=[self.scraper.slug, self.run.uuid])
        )
        bulk = self.client.post(
            reverse("runs_bulk_delete", args=[self.scraper.slug]),
            {"run_uuids": [str(self.run.uuid)]},
        )

        self.assertEqual(single.status_code, 403)
        self.assertEqual(bulk.status_code, 403)
        self.assertTrue(Run.objects.filter(pk=self.run.pk).exists())

    def test_calls_history_delete_controls_are_admin_only(self):
        url = reverse("scraper_detail", args=[self.scraper.slug]) + "?tab=calls"

        self.client.force_login(self.member)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "bulkDeleteForm")
        self.assertNotContains(response, "Delete selected")
        self.assertNotContains(response, "Delete this run")

        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bulkDeleteForm")
        self.assertContains(response, "Delete selected")
        self.assertContains(response, "Delete this run")

    def test_admin_can_delete_single_run_from_overview(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse("overview_run_delete", args=[self.run.uuid]))

        self.assertRedirects(response, reverse("overview"))
        self.assertFalse(Run.objects.filter(pk=self.run.pk).exists())

    def test_admin_bulk_delete_from_overview_spans_scrapers_and_skips_running_runs(self):
        self.client.force_login(self.admin)
        queued_run = Run.objects.create(
            scraper=self.scraper,
            status=Run.Status.QUEUED,
            tournament="Queued run",
        )

        response = self.client.post(
            reverse("overview_runs_bulk_delete"),
            {
                "run_uuids": [
                    str(self.run.uuid),
                    str(self.other_run.uuid),
                    str(self.running_run.uuid),
                    str(queued_run.uuid),
                ]
            },
        )

        self.assertRedirects(response, reverse("overview"))
        self.assertFalse(Run.objects.filter(pk=self.run.pk).exists())
        self.assertFalse(Run.objects.filter(pk=self.other_run.pk).exists())
        self.assertTrue(Run.objects.filter(pk=self.running_run.pk).exists())
        self.assertTrue(Run.objects.filter(pk=queued_run.pk).exists())

    def test_live_stats_delete_urls_are_admin_only_and_deletable_only(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("live_stats"))
        self.assertEqual(response.status_code, 200)
        member_runs = response.json()["overview"]["recent_runs"]
        self.assertTrue(member_runs)
        self.assertFalse(any("delete_url" in run for run in member_runs))
        self.assertFalse(any(run["can_delete"] for run in member_runs))

        self.client.force_login(self.admin)
        response = self.client.get(reverse("live_stats"))
        self.assertEqual(response.status_code, 200)
        admin_runs = response.json()["overview"]["recent_runs"]
        by_uuid = {run["uuid"]: run for run in admin_runs}

        self.assertIn("delete_url", by_uuid[str(self.run.uuid)])
        self.assertTrue(by_uuid[str(self.run.uuid)]["can_delete"])
        self.assertNotIn("delete_url", by_uuid[str(self.running_run.uuid)])
        self.assertFalse(by_uuid[str(self.running_run.uuid)]["can_delete"])
