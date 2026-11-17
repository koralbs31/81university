"""Registry of content backfills: work that normally happens on upload or save,
re-run for whatever predates the feature or slipped through.

Each job is idempotent and capped per run, so the periodic scheduler and the
``backfill`` CLI can run them all without piling up work:

* ``hls``: queue hosted videos that never got an HLS ladder (or failed) for
  transcoding, when HLS is enabled.

"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
logger = logging.getLogger(__name__)
DEFAULT_HLS_LIMIT = 50
DEFAULT_CHAPTERS_LIMIT = 100
_HLS_IN_FLIGHT = frozenset({'queued', 'processing'})

def _env_limit(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default

@dataclass(frozen=True)
class BackfillJob:
    name: str
    description: str
    run: Callable[[], Awaitable[dict]]
    enabled: Callable[[], bool] = lambda: True

async def _hls() -> dict:
    """LEARNHOUSE_BACKFILL_HLS_LIMIT caps how many videos one run queues."""
    from sqlmodel import select
    from src.core.events.database import _async_session_factory
    from src.db.courses.activities import Activity, ActivitySubTypeEnum
    from src.services.utils import hls_jobs
    async with _async_session_factory() as db:
        activities = (await db.execute(select(Activity).where(Activity.activity_sub_type == ActivitySubTypeEnum.SUBTYPE_VIDEO_HOSTED))).scalars().all()
    pending = []
    for a in activities:
        status = ((a.extra_metadata or {}).get('hls') or {}).get('status')
        if status in hls_jobs.TERMINAL_STATUSES or status in _HLS_IN_FLIGHT:
            continue
        if (a.content or {}).get('filename'):
            pending.append(a.activity_uuid)
    limit = _env_limit('LEARNHOUSE_BACKFILL_HLS_LIMIT', DEFAULT_HLS_LIMIT)
    batch = pending[:limit] if limit else pending
    for uuid in batch:
        hls_jobs.enqueue(uuid)
    return {'queued': len(batch), 'deferred': len(pending) - len(batch)}

def _hls_enabled() -> bool:
    from src.services.utils.hls_jobs import hls_enabled
    return hls_enabled()
JOBS: tuple[BackfillJob, ...] = (BackfillJob('hls', 'Queue hosted videos without an HLS ladder for transcoding', _hls, _hls_enabled),)

async def run_all(only: Optional[list[str]]=None) -> dict[str, dict]:
    """Run every enabled job (or just ``only``), in order. One job failing is
    logged and reported without stopping the others."""
    results: dict[str, dict] = {}
    for job in JOBS:
        if only and job.name not in only:
            continue
        if not job.enabled():
            results[job.name] = {'skipped': 'disabled'}
            continue
        try:
            results[job.name] = await job.run()
        except Exception as exc:
            logger.exception('Backfill %s failed', job.name)
            results[job.name] = {'error': str(exc)[:200]}
    return results
