from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Scraper, Ticket


class QATicketCreateTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.reporter = User.objects.create_user("reporter", password="pass")
        self.khemiri = User.objects.create_user("khemiri", password="pass")
        self.scraper = Scraper.objects.create(
            slug="qa_assignment_scraper",
            code="QA",
            name="QA Assignment Scraper",
            tour="QA",
            domain="example.com",
        )
        self.client.force_login(self.reporter)

    def test_new_ticket_is_assigned_to_khemiri(self):
        response = self.client.post(
            reverse("qa_ticket_create"),
            {
                "scraper": self.scraper.slug,
                "title": "Check assignment",
                "status": Ticket.Status.TODO,
                "priority": Ticket.Priority.MEDIUM,
                "body_html": "<p>Please check</p>",
            },
        )

        ticket = Ticket.objects.get(title="Check assignment")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ticket.assignee, self.khemiri)


class QATicketBulkDeleteTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            "qa-admin", "qa-admin@example.com", "pass"
        )
        self.member = User.objects.create_user("qa-member", password="pass")
        self.scraper = Scraper.objects.create(
            slug="qa_bulk_delete",
            code="QABD",
            name="QA Bulk Delete",
            tour="QA",
            domain="example.com",
        )
        self.done_ticket = Ticket.objects.create(
            scraper=self.scraper,
            title="Completed request",
            status=Ticket.Status.DONE,
            created_by=self.member,
        )
        self.other_done_ticket = Ticket.objects.create(
            scraper=self.scraper,
            title="Another completed request",
            status=Ticket.Status.DONE,
            created_by=self.member,
        )
        self.active_ticket = Ticket.objects.create(
            scraper=self.scraper,
            title="Active request",
            status=Ticket.Status.IN_PROGRESS,
            created_by=self.member,
        )

    def test_bulk_delete_controls_are_admin_only_and_done_only(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("qa_board"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<form id="qaDoneBulkDeleteForm"')
        self.assertNotContains(response, 'class="qa-done-check"')

        self.client.force_login(self.admin)
        response = self.client.get(reverse("qa_board"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<form id="qaDoneBulkDeleteForm"')
        self.assertContains(
            response,
            f'name="ticket_uuids" value="{self.done_ticket.uuid}"',
        )
        self.assertContains(
            response,
            f'name="ticket_uuids" value="{self.other_done_ticket.uuid}"',
        )
        self.assertNotContains(
            response,
            f'name="ticket_uuids" value="{self.active_ticket.uuid}"',
        )

    def test_member_cannot_bulk_delete_tickets_by_posting_directly(self):
        self.client.force_login(self.member)

        response = self.client.post(
            reverse("qa_tickets_bulk_delete"),
            {"ticket_uuids": [str(self.done_ticket.uuid)]},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Ticket.objects.filter(pk=self.done_ticket.pk).exists())

    def test_admin_bulk_delete_removes_selected_done_and_preserves_active(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("qa_tickets_bulk_delete"),
            {
                "ticket_uuids": [
                    str(self.done_ticket.uuid),
                    str(self.active_ticket.uuid),
                    "not-a-uuid",
                ]
            },
        )

        self.assertRedirects(response, reverse("qa_board"))
        self.assertFalse(Ticket.objects.filter(pk=self.done_ticket.pk).exists())
        self.assertTrue(Ticket.objects.filter(pk=self.other_done_ticket.pk).exists())
        self.assertTrue(Ticket.objects.filter(pk=self.active_ticket.pk).exists())

    def test_empty_or_malformed_selection_is_safe(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("qa_tickets_bulk_delete"),
            {"ticket_uuids": ["not-a-uuid"]},
        )

        self.assertRedirects(response, reverse("qa_board"))
        self.assertEqual(Ticket.objects.count(), 3)

    def test_bulk_delete_retains_valid_scraper_filter(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("qa_tickets_bulk_delete"),
            {
                "ticket_uuids": [str(self.done_ticket.uuid)],
                "scraper": self.scraper.slug,
            },
        )

        self.assertRedirects(
            response,
            f'{reverse("qa_board")}?scraper={self.scraper.slug}',
        )
