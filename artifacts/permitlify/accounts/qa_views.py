"""QA Team Tasks — a lightweight Jira-style ticketing board.

Tickets are filed per scraper with rich-text notes (sanitised server-side) and
inline screenshots (stored as :class:`TicketAttachment` rows, served by an
auth-gated view). Filing a ticket / commenting fans a :class:`Notification` out
to every other active user for the topbar bell.

Shared page context comes from :func:`accounts.views._app_ctx`; importing it here
is one-way (``views`` never imports this module), so there is no import cycle.
"""

import re

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.timesince import timesince
from django.views.decorators.http import require_POST

from .models import Notification, Scraper, Ticket, TicketAttachment, TicketComment
from .sanitize import clean_html, is_blank_html
from .views import _app_ctx

User = get_user_model()

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MB per screenshot.

STATUS_COLUMNS = [
    (Ticket.Status.TODO, "To Do"),
    (Ticket.Status.IN_PROGRESS, "In Progress"),
    (Ticket.Status.DONE, "Done"),
]


# --------------------------------------------------------------------------- #
# Notifications fan-out
# --------------------------------------------------------------------------- #
def _notify(actor, ticket, kind, text, exclude_pks=None):
    """Create one notification per active user except the actor (fan-out).

    ``exclude_pks`` skips recipients already getting a higher-signal row for the
    same event (an @mention), so they see one bell entry rather than two.
    """
    recipients = User.objects.filter(is_active=True).exclude(pk=actor.pk)
    if exclude_pks:
        recipients = recipients.exclude(pk__in=exclude_pks)
    Notification.objects.bulk_create(
        [
            Notification(
                recipient=u, actor=actor, ticket=ticket, kind=kind, text=text
            )
            for u in recipients
        ]
    )


# --------------------------------------------------------------------------- #
# @mentions
# --------------------------------------------------------------------------- #
# Mention tokens are <span class="rt-mention" data-username="…">@name</span>
# emitted by the editor; we also accept a plain "@username" typed by hand so a
# mention still lands if the token never formed. Both are resolved against real
# active accounts, so an unknown handle quietly notifies no one.
_MENTION_TOKEN_RE = re.compile(r'data-username="([^"]+)"')
_MENTION_TEXT_RE = re.compile(r"(?:^|[\s>(\u00a0])@([\w.@+\-]{1,150})")


def _mentioned_users(html, *, exclude=None):
    """Return the active users referenced by @mentions in ``html``."""
    if not html:
        return []
    names = set(_MENTION_TOKEN_RE.findall(html))
    names.update(_MENTION_TEXT_RE.findall(html))
    if not names:
        return []
    qs = User.objects.filter(is_active=True, username__in=names)
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return list(qs)


def _notify_mentions(actor, ticket, users, text):
    """Fan a MENTIONED notification out to a specific set of users (not actor)."""
    rows = [
        Notification(
            recipient=u,
            actor=actor,
            ticket=ticket,
            kind=Notification.Kind.MENTIONED,
            text=text,
        )
        for u in users
        if u.pk != actor.pk
    ]
    if rows:
        Notification.objects.bulk_create(rows)


# --------------------------------------------------------------------------- #
# Attachments (inline screenshots)
# --------------------------------------------------------------------------- #
def _sniff_image(data):
    """Return the MIME type from magic bytes, or None if not an allowed image.

    Deliberately excludes SVG — it is XML and can carry script.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@login_required
@require_POST
def attachment_upload(request):
    f = request.FILES.get("image") or request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "No image was uploaded."}, status=400)
    if f.size > MAX_ATTACHMENT_BYTES:
        return JsonResponse({"error": "Image exceeds the 5 MB limit."}, status=400)
    data = f.read()
    if len(data) > MAX_ATTACHMENT_BYTES:
        return JsonResponse({"error": "Image exceeds the 5 MB limit."}, status=400)
    content_type = _sniff_image(data)
    if not content_type:
        return JsonResponse(
            {"error": "Only PNG, JPEG, GIF or WebP images are allowed."}, status=400
        )
    att = TicketAttachment.objects.create(
        uploaded_by=request.user,
        content_type=content_type,
        data=data,
        byte_size=len(data),
    )
    return JsonResponse({"url": reverse("qa_attachment", args=[att.uuid])})


@login_required
def attachment_serve(request, uuid):
    att = get_object_or_404(TicketAttachment, uuid=uuid)
    resp = HttpResponse(bytes(att.data), content_type=att.content_type)
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Disposition"] = "inline"
    resp["Cache-Control"] = "private, max-age=86400"
    return resp


# --------------------------------------------------------------------------- #
# Board + tickets
# --------------------------------------------------------------------------- #
@login_required
def board(request):
    tickets = (
        Ticket.objects.select_related("scraper", "created_by", "assignee")
        .annotate(n_comments=Count("comments"))
        .order_by("-created_at")
    )
    active_scraper = (request.GET.get("scraper") or "").strip()
    if active_scraper:
        tickets = tickets.filter(scraper__slug=active_scraper)

    all_tickets = list(tickets)
    columns = []
    for status, label in STATUS_COLUMNS:
        items = [t for t in all_tickets if t.status == status]
        columns.append(
            {"status": status, "label": label, "items": items, "count": len(items)}
        )

    ctx = _app_ctx(
        "qa",
        columns=columns,
        total_tickets=len(all_tickets),
        scrapers=Scraper.objects.order_by("name"),
        active_scraper=active_scraper,
        statuses=Ticket.Status.choices,
        priorities=Ticket.Priority.choices,
    )
    return render(request, "qa_board.html", ctx)


@login_required
@require_POST
def ticket_create(request):
    scraper_slug = (request.POST.get("scraper") or "").strip()
    title = (request.POST.get("title") or "").strip()
    status = request.POST.get("status") or Ticket.Status.TODO
    priority = request.POST.get("priority") or Ticket.Priority.MEDIUM
    body_html = clean_html(request.POST.get("body_html") or "")

    scraper = Scraper.objects.filter(slug=scraper_slug).first()
    if not scraper:
        messages.error(request, "Pick a scraper for this ticket.")
        return redirect("qa_board")
    if not title:
        messages.error(request, "Give the ticket a title.")
        return redirect("qa_board")
    if status not in Ticket.Status.values:
        status = Ticket.Status.TODO
    if priority not in Ticket.Priority.values:
        priority = Ticket.Priority.MEDIUM

    ticket = Ticket.objects.create(
        scraper=scraper,
        title=title[:200],
        body_html=body_html,
        status=status,
        priority=priority,
        created_by=request.user,
    )
    mentioned = _mentioned_users(body_html, exclude=request.user)
    _notify(
        request.user,
        ticket,
        Notification.Kind.TICKET_CREATED,
        f"{request.user.username} filed “{title[:80]}” on {scraper.name}",
        exclude_pks={u.pk for u in mentioned},
    )
    if mentioned:
        _notify_mentions(
            request.user,
            ticket,
            mentioned,
            f"{request.user.username} mentioned you in “{title[:80]}”",
        )
    messages.success(request, "Ticket created.")
    return redirect("qa_ticket", uuid=ticket.uuid)


@login_required
def ticket_detail(request, uuid):
    ticket = get_object_or_404(
        Ticket.objects.select_related("scraper", "created_by", "assignee"), uuid=uuid
    )
    comments = ticket.comments.select_related("author").all()
    ctx = _app_ctx(
        "qa",
        ticket=ticket,
        comments=comments,
        statuses=Ticket.Status.choices,
        priorities=Ticket.Priority.choices,
        assignable=User.objects.filter(is_active=True).order_by("username"),
        scrapers=Scraper.objects.order_by("name"),
    )
    return render(request, "qa_ticket.html", ctx)


@login_required
@require_POST
def ticket_edit(request, uuid):
    """Full edit of an existing ticket: scraper, title, priority, status, body.

    Mirrors :func:`ticket_create`'s validation and re-sanitises the rich-text
    body. Like the sidebar :func:`ticket_update`, a status change fans out a
    notification; other edits stay quiet to avoid noise.
    """
    ticket = get_object_or_404(Ticket, uuid=uuid)
    scraper_slug = (request.POST.get("scraper") or "").strip()
    title = (request.POST.get("title") or "").strip()
    status = request.POST.get("status") or ticket.status
    priority = request.POST.get("priority") or ticket.priority
    body_html = clean_html(request.POST.get("body_html") or "")

    scraper = Scraper.objects.filter(slug=scraper_slug).first()
    if not scraper:
        messages.error(request, "Pick a scraper for this ticket.")
        return redirect("qa_ticket", uuid=ticket.uuid)
    if not title:
        messages.error(request, "Give the ticket a title.")
        return redirect("qa_ticket", uuid=ticket.uuid)
    if status not in Ticket.Status.values:
        status = ticket.status
    if priority not in Ticket.Priority.values:
        priority = ticket.priority

    status_changed = status != ticket.status
    old_mention_pks = {u.pk for u in _mentioned_users(ticket.body_html)}

    ticket.scraper = scraper
    ticket.title = title[:200]
    ticket.body_html = body_html
    ticket.status = status
    ticket.priority = priority
    ticket.save(
        update_fields=[
            "scraper",
            "title",
            "body_html",
            "status",
            "priority",
            "updated_at",
        ]
    )

    if status_changed:
        _notify(
            request.user,
            ticket,
            Notification.Kind.STATUS_CHANGED,
            f"{request.user.username} moved “{ticket.title[:50]}” to {ticket.get_status_display()}",
        )
    new_mentions = [
        u
        for u in _mentioned_users(body_html, exclude=request.user)
        if u.pk not in old_mention_pks
    ]
    if new_mentions:
        _notify_mentions(
            request.user,
            ticket,
            new_mentions,
            f"{request.user.username} mentioned you in “{ticket.title[:80]}”",
        )
    messages.success(request, "Ticket updated.")
    return redirect("qa_ticket", uuid=ticket.uuid)


@login_required
@require_POST
def ticket_delete(request, uuid):
    """Delete a ticket — **admins (superusers) only**.

    Mirrors the superuser gate the Users admin uses: a non-superuser is bounced
    back to the ticket with an error and nothing is removed (defence in depth —
    the button is also hidden for them in the template). The cascade drops the
    ticket's comments and notifications; attachments survive (their ticket FK is
    ``SET_NULL``) and stay reachable by their own URL.
    """
    ticket = get_object_or_404(Ticket, uuid=uuid)
    if not request.user.is_superuser:
        messages.error(request, "Only an admin can delete tickets.")
        return redirect("qa_ticket", uuid=ticket.uuid)
    title = ticket.title[:80]
    ticket.delete()
    messages.success(request, f"Ticket “{title}” was deleted.")
    return redirect("qa_board")


@login_required
@require_POST
def ticket_update(request, uuid):
    ticket = get_object_or_404(Ticket, uuid=uuid)
    new_status = request.POST.get("status")
    new_priority = request.POST.get("priority")
    assignee_id = request.POST.get("assignee", None)

    changed = []
    status_changed = False
    if new_status in Ticket.Status.values and new_status != ticket.status:
        ticket.status = new_status
        status_changed = True
        changed.append("status")
    if new_priority in Ticket.Priority.values and new_priority != ticket.priority:
        ticket.priority = new_priority
        changed.append("priority")
    if assignee_id is not None:
        if assignee_id == "" and ticket.assignee_id is not None:
            ticket.assignee = None
            changed.append("assignee")
        elif assignee_id.isdigit() and int(assignee_id) != (ticket.assignee_id or 0):
            user = User.objects.filter(pk=int(assignee_id), is_active=True).first()
            if user:
                ticket.assignee = user
                changed.append("assignee")

    if changed:
        changed.append("updated_at")
        ticket.save(update_fields=changed)
        if status_changed:
            _notify(
                request.user,
                ticket,
                Notification.Kind.STATUS_CHANGED,
                f"{request.user.username} moved “{ticket.title[:50]}” to {ticket.get_status_display()}",
            )
        messages.success(request, "Ticket updated.")
    return redirect("qa_ticket", uuid=ticket.uuid)


@login_required
@require_POST
def comment_add(request, uuid):
    ticket = get_object_or_404(Ticket, uuid=uuid)
    body = request.POST.get("body_html") or ""
    if is_blank_html(body):
        messages.error(request, "Write something before posting a comment.")
        return redirect("qa_ticket", uuid=ticket.uuid)
    cleaned = clean_html(body)
    TicketComment.objects.create(
        ticket=ticket, author=request.user, body_html=cleaned
    )
    mentioned = _mentioned_users(cleaned, exclude=request.user)
    _notify(
        request.user,
        ticket,
        Notification.Kind.COMMENT_ADDED,
        f"{request.user.username} commented on “{ticket.title[:80]}”",
        exclude_pks={u.pk for u in mentioned},
    )
    if mentioned:
        _notify_mentions(
            request.user,
            ticket,
            mentioned,
            f"{request.user.username} mentioned you in a comment on “{ticket.title[:80]}”",
        )
    messages.success(request, "Comment posted.")
    return redirect(reverse("qa_ticket", args=[ticket.uuid]) + "#comments")


@login_required
def mention_users(request):
    """Active accounts for the @mention autocomplete (username + display label)."""
    out = []
    for u in User.objects.filter(is_active=True).order_by("username"):
        full = (u.get_full_name() or "").strip()
        out.append(
            {"username": u.username, "label": f"{full} (@{u.username})" if full else u.username}
        )
    return JsonResponse({"users": out})


# --------------------------------------------------------------------------- #
# Notifications (bell)
# --------------------------------------------------------------------------- #
@login_required
def notifications_poll(request):
    qs = Notification.objects.filter(recipient=request.user).select_related(
        "ticket", "actor"
    )
    unread = qs.filter(is_read=False).count()
    items = [
        {
            "id": n.id,
            "text": n.text,
            "kind": n.kind,
            "is_read": n.is_read,
            "url": reverse("qa_notification_open", args=[n.id]),
            "ago": timesince(n.created_at).split(",")[0] + " ago",
        }
        for n in qs[:8]
    ]
    return JsonResponse({"unread": unread, "items": items})


@login_required
@require_POST
def notifications_read_all(request):
    Notification.objects.filter(recipient=request.user, is_read=False).update(
        is_read=True
    )
    return JsonResponse({"ok": True})


@login_required
def notification_open(request, pk):
    n = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if not n.is_read:
        n.is_read = True
        n.save(update_fields=["is_read"])
    if n.ticket_id:
        return redirect("qa_ticket", uuid=n.ticket.uuid)
    return redirect("qa_board")
