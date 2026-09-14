"""Background auto-categorization.

Runs in the celery worker, which already holds everything this needs: the
database, `SECRET_KEY` (to decrypt the stored LLM key), and outbound HTTPS
to the model provider. Nothing here talks to the MCP server, so the
worker's network policy is unchanged.
"""
import asyncio
import logging
import uuid

from app.worker import celery_app

logger = logging.getLogger(__name__)


def _make_session_maker():
    """Mirror of sync_tasks' helper — a fresh engine per Celery event loop."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().database_url)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _run(workspace_id: uuid.UUID, user_id: uuid.UUID) -> dict:
    from app.services.auto_categorize_service import auto_categorize_workspace

    engine, session_maker = _make_session_maker()
    try:
        async with session_maker() as session:
            result = await auto_categorize_workspace(session, workspace_id, user_id)
            return result.as_dict()
    finally:
        await engine.dispose()


# The LLM call has no timeout of its own, and the worker runs with
# --concurrency=2: one provider that never answers would hold half the pool
# indefinitely, and two would stop every bank sync behind them. Real runs
# have taken 3s to 104s. The soft limit raises inside the task, where the
# `except Exception` below turns it into an ordinary error result; the hard
# limit is the backstop if the soft one cannot interrupt the call.
SOFT_TIME_LIMIT_SECONDS = 180
TIME_LIMIT_SECONDS = 210


@celery_app.task(
    name="app.tasks.categorize_tasks.auto_categorize_workspace_task",
    soft_time_limit=SOFT_TIME_LIMIT_SECONDS,
    time_limit=TIME_LIMIT_SECONDS,
)
def auto_categorize_workspace_task(workspace_id: str, user_id: str) -> dict:
    """Categorize a workspace's uncategorized backlog.

    Never raises: the caller is usually a bank sync that has already
    written its transactions, and losing that work because a model was
    unreachable would be a bad trade.

    The result carries `workspace_id` so the status endpoint can refuse to
    show one workspace's outcome to a member of another. Task ids are
    unguessable, but that should not be the only thing standing between them.
    """
    try:
        result = asyncio.run(_run(uuid.UUID(workspace_id), uuid.UUID(user_id)))
    except Exception as exc:  # noqa: BLE001
        logger.exception("Auto-categorize task failed for workspace %s", workspace_id)
        result = {"status": "error", "detail": str(exc), "categorized": 0}
    return {**result, "workspace_id": workspace_id}
