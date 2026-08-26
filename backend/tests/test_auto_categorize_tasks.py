"""The celery task and the post-sync hook that dispatches it."""
import uuid

import pytest

from app.tasks import categorize_tasks, sync_tasks


def test_task_returns_the_service_result(monkeypatch):
    async def _fake_run(workspace_id, user_id):
        return {"status": "ok", "categorized": 3}

    monkeypatch.setattr(categorize_tasks, "_run", _fake_run)

    out = categorize_tasks.auto_categorize_workspace_task(
        str(uuid.uuid4()), str(uuid.uuid4())
    )

    assert out == {"status": "ok", "categorized": 3}


def test_task_never_raises(monkeypatch):
    """A bank sync has already written its rows by the time this runs.
    Losing that work to an unreachable model would be a bad trade."""

    async def _boom(workspace_id, user_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(categorize_tasks, "_run", _boom)

    out = categorize_tasks.auto_categorize_workspace_task(
        str(uuid.uuid4()), str(uuid.uuid4())
    )

    assert out["status"] == "error"
    assert out["categorized"] == 0
    assert "database went away" in out["detail"]


def test_task_survives_a_malformed_id(monkeypatch):
    out = categorize_tasks.auto_categorize_workspace_task("not-a-uuid", "also-not")

    assert out["status"] == "error"
    assert out["categorized"] == 0


def test_hook_dispatches_one_task_per_workspace(monkeypatch):
    dispatched: list[tuple[str, str]] = []

    class _Task:
        @staticmethod
        def delay(workspace_id, user_id):
            dispatched.append((workspace_id, user_id))

    monkeypatch.setattr(
        categorize_tasks, "auto_categorize_workspace_task", _Task
    )

    ws, user = uuid.uuid4(), uuid.uuid4()
    sync_tasks._queue_auto_categorize({(ws, user)})

    assert dispatched == [(str(ws), str(user))]


def test_hook_swallows_a_broker_failure(monkeypatch):
    """Redis being down must not turn a successful sync into a failed one."""

    class _Task:
        @staticmethod
        def delay(workspace_id, user_id):
            raise ConnectionError("redis unreachable")

    monkeypatch.setattr(
        categorize_tasks, "auto_categorize_workspace_task", _Task
    )

    sync_tasks._queue_auto_categorize({(uuid.uuid4(), uuid.uuid4())})


def test_hook_is_a_noop_for_an_empty_set(monkeypatch):
    called = False

    class _Task:
        @staticmethod
        def delay(workspace_id, user_id):
            nonlocal called
            called = True

    monkeypatch.setattr(
        categorize_tasks, "auto_categorize_workspace_task", _Task
    )

    sync_tasks._queue_auto_categorize(set())

    assert called is False


@pytest.mark.asyncio
async def test_sync_all_queues_each_synced_workspace_once(monkeypatch):
    """Two connections in one workspace should mean one categorize pass,
    not two — the service works on the whole backlog either way."""
    ws, user = uuid.uuid4(), uuid.uuid4()
    conn_a, conn_b = uuid.uuid4(), uuid.uuid4()

    class _Result:
        def all(self):
            return [(conn_a, user, None), (conn_b, user, None)]

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Engine:
        async def dispose(self):
            return None

    monkeypatch.setattr(
        sync_tasks, "_make_session_maker", lambda: (_Engine(), lambda: _Session())
    )

    async def _fake_sync_one(session_maker, connection_id, user_id):
        return ws

    monkeypatch.setattr(sync_tasks, "_sync_one", _fake_sync_one)

    queued: list[set] = []
    monkeypatch.setattr(sync_tasks, "_queue_auto_categorize", queued.append)

    synced = await sync_tasks._sync_all()

    assert synced == 2
    assert queued == [{(ws, user)}]


@pytest.mark.asyncio
async def test_sync_all_does_not_queue_a_connection_that_failed(monkeypatch):
    conn = uuid.uuid4()
    user = uuid.uuid4()

    class _Result:
        def all(self):
            return [(conn, user, None)]

    class _Session:
        async def execute(self, *_a, **_k):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _Engine:
        async def dispose(self):
            return None

    monkeypatch.setattr(
        sync_tasks, "_make_session_maker", lambda: (_Engine(), lambda: _Session())
    )

    async def _boom(session_maker, connection_id, user_id):
        raise RuntimeError("provider refused")

    monkeypatch.setattr(sync_tasks, "_sync_one", _boom)

    queued: list[set] = []
    monkeypatch.setattr(sync_tasks, "_queue_auto_categorize", queued.append)

    synced = await sync_tasks._sync_all()

    assert synced == 0
    assert queued == [set()], "nothing synced, so nothing to categorize"
