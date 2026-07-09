from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import Scraper, Ticket, TicketComment


class ScraperDetailQATabTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("operator", password="pass")
        self.reporter = User.objects.create_user("reporter", password="pass")
        self.assignee = User.objects.create_user("assignee", password="pass")
        self.scraper = Scraper.objects.create(
            slug="qa_detail_scraper",
            code="QAD",
            name="QA Detail Scraper",
            tour="QA",
            domain="example.com",
        )
        self.other_scraper = Scraper.objects.create(
            slug="qa_detail_other",
            code="QAO",
            name="Other QA Scraper",
            tour="QA",
            domain="example.org",
        )
        self.client.force_login(self.user)

    def test_qa_tab_lists_scraper_tickets_with_five_row_pager(self):
        tickets = []
        for idx in range(6):
            tickets.append(
                Ticket.objects.create(
                    scraper=self.scraper,
                    title=f"Scoped ticket {idx}",
                    created_by=self.reporter,
                    assignee=self.assignee,
                    priority=(
                        Ticket.Priority.HIGH if idx == 0 else Ticket.Priority.MEDIUM
                    ),
                    status=(
                        Ticket.Status.QA_REVIEW if idx == 0 else Ticket.Status.TODO
                    ),
                )
            )
        TicketComment.objects.create(
            ticket=tickets[0], author=self.reporter, body_html="<p>Needs review</p>"
        )
        Ticket.objects.create(
            scraper=self.other_scraper,
            title="Other scraper ticket",
            created_by=self.reporter,
        )

        response = self.client.get(
            reverse("scraper_detail", args=[self.scraper.slug]) + "?tab=qa"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "QA tickets")
        self.assertContains(response, "6 tickets")
        self.assertContains(response, "Scoped ticket 0")
        self.assertContains(response, "Scoped ticket 5")
        self.assertNotContains(response, "Other scraper ticket")
        self.assertContains(response, "qa-status--qa_review")
        self.assertContains(response, "qa-pri--high")
        self.assertContains(response, 'id="qaTicketsPager"')
        self.assertContains(response, "data-qa-tickets-next")
        self.assertContains(response, "var PAGE_SIZE = 5")
        self.assertContains(response, reverse("qa_ticket", args=[tickets[0].uuid]))
