#!/usr/bin/env python3
# scripts/scheduler.py
"""Cron-based scheduler for the agent."""

import os
import sys
import time
import signal
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from agent.monitor import run_monitor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

scheduler = BlockingScheduler(timezone="UTC")

# Meeting monitor schedule
cron_expr = os.getenv("AGENT_CRON_SCHEDULE", "*/30 * * * *")
parts = cron_expr.split()
if len(parts) == 5:
    minute, hour, day, month, dow = parts
    scheduler.add_job(
        run_monitor,
        CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=dow),
        id="monitor",
        name="Drive Monitor",
    )



def shutdown(signum, frame):
    logger.info("Shutting down scheduler...")
    scheduler.shutdown()
    sys.exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

logger.info(f"Scheduler starting. Monitor cron: {cron_expr}")
scheduler.start()
