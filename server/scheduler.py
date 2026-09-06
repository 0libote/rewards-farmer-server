import asyncio
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from server.config import get_config
from server.runner import start_run

scheduler: Optional[BackgroundScheduler] = None
event_loop: Optional[asyncio.AbstractEventLoop] = None


def _scheduled_job():
    global event_loop
    if event_loop and event_loop.is_running():
        print("[SCHEDULER] Triggering scheduled Rewards Farmer run...")
        asyncio.run_coroutine_threadsafe(start_run(), event_loop)
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
    job_id = "rewards_farmer_daily_run"

    # Remove existing job if any
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    if config.schedule.enabled:
        hour = config.schedule.cron_hour
        minute = config.schedule.cron_minute
        scheduler.add_job(
            _scheduled_job,
            trigger=CronTrigger(hour=hour, minute=minute),
            id=job_id,
            replace_existing=True,
        )
        print(f"[SCHEDULER] Scheduled daily run at {hour:02d}:{minute:02d} UTC.")
    else:
        print("[SCHEDULER] Scheduled runs are currently disabled.")


def get_schedule_info():
    config = get_config()
    job_id = "rewards_farmer_daily_run"
    next_run = None
    if scheduler and scheduler.get_job(job_id):
        next_fire = scheduler.get_job(job_id).next_run_time
        if next_fire:
            next_run = next_fire.isoformat()

    return {
        "enabled": config.schedule.enabled,
        "cron_hour": config.schedule.cron_hour,
        "cron_minute": config.schedule.cron_minute,
        "next_run": next_run,
    }
