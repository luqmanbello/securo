import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.worker import celery_app
from app.core.config import get_settings
from app.models.bank_connection import BankConnection
from app.providers.base import ProviderNotConfiguredError
from app.services import connection_service

logger = logging.getLogger(__name__)

STALE_THRESHOLD = timedelta(hours=4)


def _make_session_maker():
    """Create a fresh engine+session for the Celery worker event loop."""
    engine = create_async_engine(get_settings().database_url)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _sync_all() -> int:
    """Find stale connections and sync each one."""
    engine, session_maker = _make_session_maker()
    try:
        cutoff = datetime.now(timezone.utc) - STALE_THRESHOLD
        synced = 0

        async with session_maker() as session:
            result = await session.execute(
                select(
                    BankConnection.id, BankConnection.user_id, BankConnection.last_sync_at
                ).where(
                    BankConnection.status.in_(["active", "error"]),
                    (BankConnection.last_sync_at < cutoff)
                    | (BankConnection.last_sync_at.is_(None)),
                )
            )
            connections = result.all()

        logger.info(
            "Sync check: found %d stale connections (cutoff=%s)",
            len(connections),
            cutoff.isoformat(),
        )

        categorize_targets: set[tuple[uuid.UUID, uuid.UUID]] = set()

        for conn_id, user_id, last_sync in connections:
            try:
                logger.info("Syncing connection %s (last_sync=%s)", conn_id, last_sync)
                workspace_id = await _sync_one(session_maker, conn_id, user_id)
                if workspace_id is not None:
                    categorize_targets.add((workspace_id, user_id))
                synced += 1
            except ProviderNotConfiguredError as exc:
                # Actionable one-liner instead of a buried traceback: this
                # means THIS process is missing the provider's configuration.
                logger.error("Skipping connection %s: %s", conn_id, exc)
            except Exception:
                logger.exception("Background sync failed for connection %s", conn_id)

        # One pass per workspace, not per connection: the categorizer works
        # on the workspace's whole uncategorized backlog either way.
        _queue_auto_categorize(categorize_targets)

        return synced
    finally:
        await engine.dispose()


async def _sync_one(
    session_maker, connection_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID | None:
    """Sync a single connection. Error status is set by sync_connection itself.

    Returns the workspace that was synced so the caller can queue
    follow-up work for it, or None when there was nothing to sync.
    """
    async with session_maker() as session:
        workspace_id = await session.scalar(
            select(BankConnection.workspace_id).where(BankConnection.id == connection_id)
        )
        if workspace_id is None:
            logger.warning("Connection %s has no workspace; skipping sync", connection_id)
            return None
        await connection_service.sync_connection(
            session, connection_id, workspace_id, user_id
        )
        return workspace_id


def _queue_auto_categorize(pairs: set[tuple[uuid.UUID, uuid.UUID]]) -> None:
    """Hand freshly-synced workspaces to the LLM categorizer.

    Dispatched rather than awaited so a slow or unreachable model never
    holds up the sync that produced the rows. The task itself no-ops when
    the agents feature is off, so this is safe to call unconditionally.
    """
    from app.tasks.categorize_tasks import auto_categorize_workspace_task

    for workspace_id, user_id in pairs:
        try:
            auto_categorize_workspace_task.delay(str(workspace_id), str(user_id))
        except Exception:
            logger.exception(
                "Could not queue auto-categorize for workspace %s", workspace_id
            )


@celery_app.task(name="app.tasks.sync_tasks.sync_all_connections")
def sync_all_connections() -> dict:
    """Celery task: sync all stale bank connections."""
    synced = asyncio.run(_sync_all())
    logger.info("Background sync complete: %d connections synced", synced)
    return {"synced": synced}


@celery_app.task(name="app.tasks.sync_tasks.sync_single_connection")
def sync_single_connection(connection_id: str, user_id: str) -> dict:
    """Celery task: sync a single connection (used for on-demand dispatch)."""
    try:
        asyncio.run(_sync_one_celery(connection_id, user_id))
        return {"status": "ok", "connection_id": connection_id}
    except Exception as e:
        logger.exception("Sync task failed for connection %s", connection_id)
        return {"status": "error", "connection_id": connection_id, "error": str(e)}


async def _sync_one_celery(connection_id: str, user_id: str) -> None:
    engine, session_maker = _make_session_maker()
    try:
        async with session_maker() as session:
            conn_uuid = uuid.UUID(connection_id)
            workspace_id = await session.scalar(
                select(BankConnection.workspace_id).where(BankConnection.id == conn_uuid)
            )
            if workspace_id is None:
                logger.warning("Connection %s has no workspace; skipping sync", connection_id)
                return
            await connection_service.sync_connection(
                session, conn_uuid, workspace_id, uuid.UUID(user_id)
            )
        _queue_auto_categorize({(workspace_id, uuid.UUID(user_id))})
    finally:
        await engine.dispose()
