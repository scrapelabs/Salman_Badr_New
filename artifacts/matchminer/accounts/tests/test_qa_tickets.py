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
