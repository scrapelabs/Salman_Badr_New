"""In-app recurring scheduler.

A single background daemon thread that fires due :class:`ScraperSchedule` rows
on their cadence — no external cron (GitHub Actions etc.) required. Started from
:meth:`AccountsConfig.ready` but **only** in a web-serving process; management
commands (``migrate``, the ``run_scrape`` worker subprocess, ``scrape_now``,
``shell``, ``check`` …) never spawn it.

Cross-worker safety: production runs gunicorn with several workers, each of which
calls ``ready()`` and so starts its own thread. Every tick first grabs a Postgres
``pg_try_advisory_xact_lock`` so exactly one worker makes scheduling decisions per
cycle; due rows are claimed with ``select_for_update`` and their ``next_run_at`` is
advanced **before** the worker is launched. The downstream run-start path
(:func:`accounts.views._start_scraper_run`) already enforces maintenance, the
single-in-flight-run rule, and browser-exclusivity, so this is layered safety.

Policy: at-most-once / no backfill. A schedule missed while the app was offline
fires once on recovery, then resumes its normal cadence.
"""

import logging
import os
import sys
import threading

from django.utils import timezone

logger = logging.getLogger("accounts.scheduler")

TICK_SECONDS = 45
STARTUP_DELAY_SECONDS = 10
# Postgres advisory-lock key: only one web worker's tick acts per cycle.
SCHED_LOCK_KEY = 0x6D6D7363  # "mmsc" — MatchMiner scheduler

_started = False
_start_lock = threading.Lock()
_stop = threading.Event()
_thread = None


def _disabled_by_env():
    return os.environ.get("MATCHMINER_SCHEDULER_ENABLED", "true").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    )


def should_run_in_this_process():
    """True only in a web-serving process (gunicorn / runserver).

    Excludes every management command: their argv carries the command name
    (``migrate``, ``run_scrape``, ``scrape_now`` …) rather than ``runserver`` /
    ``gunicorn``, so this returns ``False`` and no thread is spawned there.
    """
    if _disabled_by_env():
        return False
    argv = sys.argv
    if any("gunicorn" in str(a) for a in argv):
        return True
    if "runserver" in argv:
        # Avoid a double-start under the autoreloader: with ``--noreload`` there
        # is a single process (RUN_MAIN unset); with the reloader only the child
        # (RUN_MAIN == "true") should own the thread.
        if "--noreload" in argv:
            return True
        return os.environ.get("RUN_MAIN") == "true"
    return False


def start():
    """Spawn the scheduler thread once per web process (no-op elsewhere)."""
    global _started, _thread
    with _start_lock:
        if _started or not should_run_in_this_process():
            return
        _started = True
        _stop.clear()
        _thread = threading.Thread(
            target=_loop, name="matchminer-scheduler", daemon=True
        )
        _thread.start()
        logger.info("In-app scraper scheduler started (tick=%ss).", TICK_SECONDS)


def stop():
    """Signal the loop to exit (used by tests / clean shutdown)."""
    _stop.set()


def _loop():
    # Let app startup / migrations settle before the first tick.
    if _stop.wait(STARTUP_DELAY_SECONDS):
        return
    while not _stop.is_set():
        try:
            tick()
        except Exception:  # noqa: BLE001
            logger.exception("Scheduler tick failed.")
        _stop.wait(TICK_SECONDS)


def tick(now=None):
    """One scheduling cycle: claim due schedules, advance them, launch each.

    Claiming + advancing happens inside a single transaction guarded by a global
    advisory lock; launches happen after the lock is released so a slow process
    start never blocks other workers.
    """
    from django.db import close_old_connections, connection, transaction

    from . import scheduling
    from .models import ScraperSchedule

    close_old_connections()
    try:
        now = now or timezone.now()
        to_launch = []  # [(scraper, schedule_pk), …]
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_xact_lock(%s)", [SCHED_LOCK_KEY])
                if not cur.fetchone()[0]:
                    return  # another worker is ticking this cycle
            due = list(
                ScraperSchedule.objects.select_for_update()
                .filter(
                    enabled=True,
                    next_run_at__isnull=False,
                    next_run_at__lte=now,
                )
                .select_related("scraper")
                .order_by("next_run_at")
            )
            for sched in due:
                sched.next_run_at = scheduling.compute_next_run(
                    frequency=sched.frequency,
                    time_of_day=sched.time_of_day,
                    weekday=sched.weekday,
                    day_of_month=sched.day_of_month,
                    tz_name=sched.timezone,
                    anchor_date=sched.anchor_date,
                    after_utc=now,
                )
                sched.last_fired_at = now
                sched.save(
                    update_fields=["next_run_at", "last_fired_at", "updated_at"]
                )
                to_launch.append((sched.scraper, sched.pk))
        for scraper, sched_pk in to_launch:
            _launch(scraper, sched_pk)
    finally:
        close_old_connections()


def _launch(scraper, sched_pk):
    """Launch one due scraper via the shared run-start path; record the run."""
    from .live_scrapers import registry
    from .models import ScraperSchedule
    from .views import RunStartError, _start_scraper_run, validate_run_params

    # Re-check the schedule is still enabled: the user may have turned it off
    # between this cycle's claim/advance and now, so honour that immediately
    # rather than firing one last already-claimed run.
    if not ScraperSchedule.objects.filter(pk=sched_pk, enabled=True).exists():
        logger.info("Scheduled run for %s skipped: schedule disabled.", scraper.slug)
        return

    try:
        spec = registry.spec_for(scraper.slug)
        inputs = validate_run_params(spec, {}, webhook=True)
        run = _start_scraper_run(scraper, inputs=inputs, launched_by=None)
    except RunStartError as exc:
        # Maintenance / already-running / browser-exclusivity — skip this cycle.
        logger.info("Scheduled run for %s skipped: %s", scraper.slug, exc.message)
        return
    except Exception:  # noqa: BLE001
        logger.exception("Scheduled run for %s failed to start.", scraper.slug)
        return
    ScraperSchedule.objects.filter(pk=sched_pk).update(last_run=run)
    logger.info("Scheduled run #%s launched for %s.", run.short_id, scraper.slug)
