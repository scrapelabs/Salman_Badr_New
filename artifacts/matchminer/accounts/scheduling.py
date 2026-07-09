"""Pure date math for the in-app recurring scheduler.

No Django imports — callers pass the schedule's fields and a reference instant,
and every function returns timezone-aware **UTC** datetimes so the scheduler can
compare against ``timezone.now()`` and persist the result directly. Keeping the
math here (free of models / the DB) makes it trivially unit-testable.

Cadence semantics (all "strictly after" the reference instant):

* ``daily``    — every day at ``time_of_day``.
* ``weekly``   — every week on ``weekday`` (0=Mon … 6=Sun) at ``time_of_day``.
* ``biweekly`` — every 14 days on ``weekday``, anchored on ``anchor_date`` (the
  first scheduled local date) so the fortnight parity stays stable across fires.
* ``monthly``  — every month on ``day_of_month`` (clamped to the month's length)
  at ``time_of_day``.

Times are interpreted in the schedule's IANA ``timezone`` (DST-aware via
``zoneinfo``); minute-level drift across a DST transition is acceptable.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = ZoneInfo("UTC")

DAILY = "daily"
WEEKLY = "weekly"
BIWEEKLY = "biweekly"
MONTHLY = "monthly"


def get_zone(name):
    """Return a ZoneInfo for ``name``, falling back to UTC on anything invalid."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return UTC


def _clamp_day(year, month, day):
    """Clamp ``day`` to the last valid day of ``year``/``month``."""
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = (nxt - timedelta(days=1)).day
    return min(day, last)


def _combine(d, t, tz):
    return datetime(d.year, d.month, d.day, t.hour, t.minute, 0, tzinfo=tz)


def _next_weekday_on_or_after(d, weekday):
    return d + timedelta(days=(weekday - d.weekday()) % 7)


def first_anchor_date(*, time_of_day, weekday, tz_name, after_utc):
    """Local date of the first future weekly slot — the biweekly parity anchor.

    The first run lands on the next ``weekday`` whose ``time_of_day`` is still in
    the future, so biweekly schedules start on their next valid weekday and then
    step by 14 days from there.
    """
    tz = get_zone(tz_name)
    after_local = after_utc.astimezone(tz)
    d = _next_weekday_on_or_after(after_local.date(), weekday)
    if _combine(d, time_of_day, tz) <= after_local:
        d = d + timedelta(days=7)
    return d


def compute_next_run(
    *, frequency, time_of_day, weekday, day_of_month, tz_name, anchor_date, after_utc
):
    """Next due instant strictly after ``after_utc`` as a tz-aware UTC datetime."""
    tz = get_zone(tz_name)
    after_local = after_utc.astimezone(tz)
    t = time_of_day

    if frequency == DAILY:
        cand = _combine(after_local.date(), t, tz)
        if cand <= after_local:
            cand = _combine(after_local.date() + timedelta(days=1), t, tz)
        return cand.astimezone(UTC)

    if frequency == WEEKLY:
        d = _next_weekday_on_or_after(after_local.date(), weekday)
        cand = _combine(d, t, tz)
        if cand <= after_local:
            cand = _combine(d + timedelta(days=7), t, tz)
        return cand.astimezone(UTC)

    if frequency == BIWEEKLY:
        anchor = anchor_date or _next_weekday_on_or_after(after_local.date(), weekday)
        if anchor.weekday() != weekday:
            anchor = _next_weekday_on_or_after(anchor, weekday)
        days = (after_local.date() - anchor).days
        if days <= 0:
            cand_date = anchor
        else:
            cand_date = anchor + timedelta(days=14 * ((days + 13) // 14))
        cand = _combine(cand_date, t, tz)
        if cand <= after_local:
            cand = _combine(cand_date + timedelta(days=14), t, tz)
        return cand.astimezone(UTC)

    # MONTHLY (and any unknown value, treated as monthly-safe default).
    y, m = after_local.year, after_local.month
    cand = _combine(date(y, m, _clamp_day(y, m, day_of_month)), t, tz)
    if cand <= after_local:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        cand = _combine(date(y, m, _clamp_day(y, m, day_of_month)), t, tz)
    return cand.astimezone(UTC)
