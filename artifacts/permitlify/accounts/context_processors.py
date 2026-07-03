"""Template context shared across every page.

The topbar notification bell appears on all authenticated pages, so its unread
count + recent list are injected here rather than threaded through every view.
"""

from .models import Notification, Scraper

# How many recent notifications the bell dropdown shows.
BELL_RECENT = 8


def notifications(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    qs = Notification.objects.filter(recipient=user).select_related("ticket", "actor")
    recent = list(qs[:BELL_RECENT])
    unread = qs.filter(is_read=False).count()
    return {
        "nav_notifications": recent,
        "nav_unread_count": unread,
    }


def nav_scrapers(request):
    """Feed the topbar's quick-jump dropdown a lightweight list of every
    scraper (slug + name), ordered by name, so any authenticated page can jump
    straight to a scraper's lab. Only slug/name are fetched to keep it cheap."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {}
    return {
        "nav_scrapers": list(Scraper.objects.order_by("name").values("slug", "name")),
    }
