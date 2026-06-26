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
        to_launch = []  # [(scraper, schedule_pk, due_at), …]
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
                # The instant this cycle was due for (recorded on the Cron-history
                # event below); captured before we advance to the next occurrence.
                due_at = sched.next_run_at
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
                to_launch.append((sched.scraper, sched.pk, due_at))
        for scraper, sched_pk, due_at in to_launch:
            _launch(scraper, sched_pk, due_at)
    finally:
        close_old_connections()


def _launch(scraper, sched_pk, due_at=None):
    """Launch one due scraper via the shared run-start path; record the outcome.

    Every cycle writes a :class:`~accounts.models.ScheduleEvent` (the Lab's
    "Cron history") so operators can see the scheduler is alive and why a given
    cycle did or didn't start a fresh run: launched, a healthy skip (a run was
    already in flight / the source is in maintenance / the schedule was just
    disabled), or a real launch failure with the reason attached.
    """
    from .live_scrapers import registry
    from .models import ScheduleEvent, ScraperSchedule
    from .views import RunStartError, _start_scraper_run, validate_run_params

    Outcome = ScheduleEvent.Outcome
    # Map the shared run-start guard's error codes onto cron-history outcomes.
    # Only "already in progress" / browser-busy are *healthy* skips; maintenance
    # is a skip; anything else (a launch failure or an unknown/validation code,
    # e.g. a URL-required schedule with no URL) is a real FAILED — so the default
    # is FAILED, never a misleading "already running".
    outcome_for_code = {
        "maintenance": Outcome.SKIPPED_MAINTENANCE,
        "already_running": Outcome.SKIPPED_IN_FLIGHT,
        "busy": Outcome.SKIPPED_IN_FLIGHT,
        "launch_failed": Outcome.FAILED,
    }

    # Best-effort Cron-history write. Events are recorded *after* the claim txn
    # commits (the schedule is already advanced), so a crash between commit and
    # here loses the event for that one fire — acceptable under the at-most-once,
    # no-backfill policy. An event-write failure must never break the launch.
    def record(outcome, *, detail="", run=None):
        try:
            ScheduleEvent.objects.create(
                scraper=scraper,
                outcome=outcome,
                detail=(detail or "")[:500],
                run=run,
                scheduled_for=due_at,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record cron event for %s.", scraper.slug)

    # Re-check the schedule is still enabled: the user may have turned it off
    # between this cycle's claim/advance and now, so honour that immediately
    # rather than firing one last already-claimed run.
    if not ScraperSchedule.objects.filter(pk=sched_pk, enabled=True).exists():
        logger.info("Scheduled run for %s skipped: schedule disabled.", scraper.slug)
        record(
            Outcome.SKIPPED_DISABLED,
            detail="The schedule was turned off before this run could start.",
        )
        return

    try:
        spec = registry.spec_for(scraper.slug)
        inputs = validate_run_params(spec, {}, webhook=True)
        run = _start_scraper_run(scraper, inputs=inputs, launched_by=None)
    except RunStartError as exc:
        # already-running / browser-busy = healthy skip; maintenance = skip;
        # launch failure / unknown / validation code = real FAILED. The
        # RunStartError message is curated operator-facing copy (no secrets); a
        # failed launch carries its Run so the history can link to it.
        outcome = outcome_for_code.get(exc.code, Outcome.FAILED)
        logger.info("Scheduled run for %s not started: %s", scraper.slug, exc.message)
        record(outcome, detail=exc.message, run=getattr(exc, "run", None))
        return
    except Exception:  # noqa: BLE001
        # Keep the full traceback in the server log only — raw exception text can
        # carry secrets / proxy addresses, so the persisted detail stays generic.
        logger.exception("Scheduled run for %s failed to start.", scraper.slug)
        record(
            Outcome.FAILED,
            detail="The run could not be started (unexpected error). See server logs.",
        )
        return
    ScraperSchedule.objects.filter(pk=sched_pk).update(last_run=run)
    record(Outcome.LAUNCHED, run=run, detail=f"Run #{run.short_id} started.")
    logger.info("Scheduled run #%s launched for %s.", run.short_id, scraper.slug)
