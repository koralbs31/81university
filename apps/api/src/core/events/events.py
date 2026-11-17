import logging
from typing import Callable
from fastapi import FastAPI
from config.config import get_learnhouse_config
from src.core.events.autoinstall import auto_install
from src.core.events.content import check_content_directory
from src.core.events.database import close_database, connect_to_db
from src.core.events.logs import create_logs_dir, init_logging
from src.core.ee_hooks import run_ee_shutdown, run_ee_startup
from src.services.nudges import scheduler as nudges
from src.services.courses import drip_scheduler
from src.services.messaging import scheduler as messaging
from src.services.demo import scheduler as demo
from src.services.utils import hls_jobs
from src.services.backfill import scheduler as backfill

logger = logging.getLogger(__name__)


def startup_app(app: FastAPI) -> Callable:
    async def start_app() -> None:
        app.learnhouse_config = get_learnhouse_config()
        init_logging()
        await connect_to_db(app)
        await create_logs_dir()
        await check_content_directory()
        await auto_install()
        nudges.start_scheduler()
        drip_scheduler.start_scheduler()
        messaging.start_scheduler()
        demo.start_scheduler()
        hls_jobs.start_consumer()
        backfill.start_scheduler()
        run_ee_startup(app)
    return start_app


def shutdown_app(app: FastAPI) -> Callable:
    async def close_app() -> None:
        await hls_jobs.stop_consumer()
        await backfill.stop_scheduler()
        from src.services.webhooks.dispatch import close_webhook_client, _background_tasks
        if _background_tasks:
            import asyncio
            await asyncio.gather(*list(_background_tasks), return_exceptions=True)
        await close_webhook_client()
        await nudges.stop_scheduler()
        await drip_scheduler.stop_scheduler()
        await messaging.stop_scheduler()
        await demo.stop_scheduler()
        await run_ee_shutdown(app)
        await close_database(app)
    return close_app
