from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Notification, Scraper, Ticket


class NotificationHistoryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.recipient = User.objects.create_user("recipient", password="pass")
        self.actor = User.objects.create_user("actor", password="pass")
        self.other = User.objects.create_user("other", password="pass")
        self.scraper = Scraper.objects.create(
            slug="history_scraper",
            code="HIST",
            name="History Scraper",
            tour="QA",
            domain="example.com",
        )
        self.ticket = Ticket.objects.create(
            scraper=self.scraper,
            title="Missing score",
            created_by=self.actor,
        )
        self.client.force_login(self.recipient)

    def _notification(self, *, recipient=None, text="Actor updated the ticket"):
        return Notification.objects.create(
            recipient=recipient or self.recipient,
            actor=self.actor,
            ticket=self.ticket,
            kind=Notification.Kind.STATUS_CHANGED,
            text=text,
        )

    def test_overview_shows_notification_history(self):
        self.ticket.status = Ticket.Status.DONE
        self.ticket.save(update_fields=["status"])
        self._notification(text="Actor changed status")

        response = self.client.get(reverse("overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Notification history")
        self.assertContains(response, reverse("overview_notifications"))
        self.assertContains(response, "Loading notification history")

        response = self.client.get(reverse("overview_notifications"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        html = payload["html"]
        self.assertEqual(payload["count"], 1)
        self.assertIn("Actor changed status", html)
        self.assertIn("actor", html)
        self.assertIn("Go to ticket", html)
        self.assertIn("Ticket status", html)
        self.assertIn("qa-status--done", html)
        self.assertIn("Done", html)

    def test_overview_includes_five_row_notification_pager(self):
        for idx in range(6):
            self._notification(text=f"Notification {idx}")

        response = self.client.get(reverse("overview_notifications"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 6)
        self.assertEqual(payload["page"], 1)
        self.assertEqual(payload["num_pages"], 2)
        self.assertIn('id="notificationPager"', payload["html"])
        self.assertIn('data-notification-page="2"', payload["html"])
        self.assertNotIn("Notification 0", payload["html"])

        response = self.client.get(reverse("overview_notifications") + "?page=2")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["page"], 2)
        self.assertIn("Notification 0", payload["html"])

    def test_single_delete_only_removes_current_users_notification(self):
        mine = self._notification(text="Mine")
        other = self._notification(recipient=self.other, text="Other")

        response = self.client.post(reverse("qa_notification_delete", args=[mine.pk]))

        self.assertTrue(response["Location"].endswith("#notification-history"))
        self.assertFalse(Notification.objects.filter(pk=mine.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=other.pk).exists())

    def test_bulk_delete_only_removes_current_users_notifications(self):
        mine = self._notification(text="Mine")
        mine_two = self._notification(text="Mine two")
        other = self._notification(recipient=self.other, text="Other")

        response = self.client.post(
            reverse("qa_notifications_bulk_delete"),
            {"notification_ids": [mine.pk, mine_two.pk, other.pk]},
        )

        self.assertTrue(response["Location"].endswith("#notification-history"))
        self.assertFalse(Notification.objects.filter(pk=mine.pk).exists())
        self.assertFalse(Notification.objects.filter(pk=mine_two.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=other.pk).exists())
