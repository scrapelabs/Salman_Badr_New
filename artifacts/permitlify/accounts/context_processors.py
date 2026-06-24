"""Template context shared across every page.

The topbar notification bell appears on all authenticated pages, so its unread
count + recent list are injected here rather than threaded through every view.
"""

from .models import Notification

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
