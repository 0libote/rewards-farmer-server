import asyncio
from typing import Optional, List, Dict, Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from server.config import TIME_RE, get_config
from server.runner import start_run

scheduler: Optional[BackgroundScheduler] = None
event_loop: Optional[asyncio.AbstractEventLoop] = None


async def _run_scheduled():
    """Scheduler entrypoint: skips quietly if a run is already active."""
    try:
        from server.runner import should_skip_scheduled_run

        decision = should_skip_scheduled_run()
        if decision.get("skip"):
            print(f"[SCHEDULER] Skipping empty scheduled run: {decision.get('reason')}")
            return
    except Exception as exc:
        print(f"[SCHEDULER] Smart-skip check failed, proceeding anyway: {exc}")
    result = await start_run()
    if not result.get("success"):
        print(f"[SCHEDULER] Scheduled run skipped: {result.get('error')}")


def _scheduled_job():
    global event_loop
    if event_loop and event_loop.is_running():
        print("[SCHEDULER] Triggering scheduled Rewards Farmer run...")
        asyncio.run_coroutine_threadsafe(_run_scheduled(), event_loop)
    else:
        print("[SCHEDULER] Event loop not available to run scheduled task.")


def init_scheduler(loop: asyncio.AbstractEventLoop):
    global scheduler, event_loop
    event_loop = loop

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.start()

    reload_schedule()

    config = get_config()
    if config.schedule.run_on_startup:
        # Schedule run 10 seconds after startup
        loop.call_later(10, lambda: asyncio.create_task(start_run()))


def reload_schedule():
    global scheduler
    if not scheduler:
        return

    config = get_config()

    # Remove all existing farmer jobs
    for job in list(scheduler.get_jobs()):
        if job.id.startswith("rewards_farmer_"):
            scheduler.remove_job(job.id)

    if not config.schedule.enabled:
        print("[SCHEDULER] Scheduled runs are currently disabled.")
        return

    mode = config.schedule.mode or "daily"

    if mode == "interval":
        hours = max(1, config.schedule.interval_hours)
        scheduler.add_job(
            _scheduled_job,
            trigger=IntervalTrigger(hours=hours, timezone="UTC"),
            id="rewards_farmer_interval",
            replace_existing=True,
        )
        print(f"[SCHEDULER] Scheduled to run every {hours} hour(s) (UTC).")

    elif mode == "custom_times":
        times = config.schedule.custom_times or ["03:00", "15:00"]
        valid_times = [t.strip() for t in times if isinstance(t, str) and TIME_RE.match(t.strip())]
        if not valid_times:
            print("[SCHEDULER] No valid custom times configured (expected HH:MM); scheduled runs disabled until fixed.")
            return
        added = 0
        for idx, t_str in enumerate(valid_times):
            parts = t_str.strip().split(":")
            if len(parts) == 2:
                try:
                    h, m = int(parts[0]), int(parts[1])
                    job_id = f"rewards_farmer_time_{idx}"
                    scheduler.add_job(
                        _scheduled_job,
                        trigger=CronTrigger(hour=h, minute=m, timezone="UTC"),
                        id=job_id,
                        replace_existing=True,
                    )
                    added += 1
                except ValueError:
                    pass
        print(f"[SCHEDULER] Scheduled runs at {added} specific time(s) a day: {', '.join(valid_times)} UTC.")

    else:
        # Default daily run
        hour = config.schedule.cron_hour
        minute = config.schedule.cron_minute
        scheduler.add_job(
            _scheduled_job,
            trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
            id="rewards_farmer_daily",
            replace_existing=True,
        )
        print(f"[SCHEDULER] Scheduled daily run at {hour:02d}:{minute:02d} UTC.")


def shutdown_scheduler():
    global scheduler
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
        except Exception as e:
            print(f"[SCHEDULER] Error during shutdown: {e}")
        scheduler = None


def get_schedule_info() -> Dict[str, Any]:
    config = get_config()
    sched = config.schedule
    next_run = None

    if scheduler:
        jobs = [j for j in scheduler.get_jobs() if j.id.startswith("rewards_farmer_")]
        next_times = [j.next_run_time for j in jobs if j.next_run_time]
        if next_times:
            next_times.sort()
            next_run = next_times[0].isoformat()

    # Friendly human-readable summary
    mode = sched.mode or "daily"
    if not sched.enabled:
        desc = "Disabled"
    elif mode == "interval":
        desc = f"Every {sched.interval_hours} hour(s)"
    elif mode == "custom_times":
        times_str = ", ".join(sched.custom_times or ["03:00", "15:00"])
        desc = f"At {times_str} UTC"
    else:
        desc = f"Daily at {sched.cron_hour:02d}:{sched.cron_minute:02d} UTC"

    return {
        "enabled": sched.enabled,
        "mode": mode,
        "interval_hours": sched.interval_hours,
        "custom_times": sched.custom_times,
        "cron_hour": sched.cron_hour,
        "cron_minute": sched.cron_minute,
        "description": desc,
        "next_run": next_run,
        "skip_empty_runs": bool(getattr(sched, "skip_empty_runs", True)),
        "empty_skip_hours": int(getattr(sched, "empty_skip_hours", 12) or 12),
    }
