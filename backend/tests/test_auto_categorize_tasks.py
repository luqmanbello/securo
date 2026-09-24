"""The celery task and the post-sync hook that dispatches it."""
import uuid

import pytest

from app.tasks import categorize_tasks, sync_tasks


def test_task_returns_the_service_result_stamped_with_its_workspace(monkeypatch):
    """The stamp is what lets the status endpoint refuse a foreign workspace."""

    async def _fake_run(workspace_id, user_id):
        return {"status": "ok", "categorized": 3}

    monkeypatch.setattr(categorize_tasks, "_run", _fake_run)
    ws = str(uuid.uuid4())

    out = categorize_tasks.auto_categorize_workspace_task(ws, str(uuid.uuid4()))

    assert out == {"status": "ok", "categorized": 3, "workspace_id": ws}


def test_task_never_raises(monkeypatch):
    """A bank sync has already written its rows by the time this runs.
    Losing that work to an unreachable model would be a bad trade."""

    async def _boom(workspace_id, user_id):
        raise RuntimeError("database went away")

    monkeypatch.setattr(categorize_tasks, "_run", _boom)
    ws = str(uuid.uuid4())

    out = categorize_tasks.auto_categorize_workspace_task(ws, str(uuid.uuid4()))

    assert out["status"] == "error"
    assert out["categorized"] == 0
    assert "database went away" in out["detail"]
    assert out["workspace_id"] == ws, "an error still has to be attributable"


def test_a_soft_time_limit_becomes_an_ordinary_error_result(monkeypatch):
    """Celery raises SoftTimeLimitExceeded inside the task when the limit hits.
    It must land in the same error path as any other failure, so a slow
    provider produces a result instead of a crashed task."""
    from celery.exceptions import SoftTimeLimitExceeded

    async def _too_slow(workspace_id, user_id):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(categorize_tasks, "_run", _too_slow)

    out = categorize_tasks.auto_categorize_workspace_task(str(uuid.uuid4()), str(uuid.uuid4()))

    assert out["status"] == "error"
    assert out["categorized"] == 0


def test_the_task_carries_time_limits_that_protect_the_worker_pool():
    """The worker runs two slots and the model call has no timeout of its own.
    Without these, one provider that never answers holds a slot for good."""
    task = categorize_tasks.auto_categorize_workspace_task

    assert task.soft_time_limit == categorize_tasks.SOFT_TIME_LIMIT_SECONDS
    assert task.time_limit == categorize_tasks.TIME_LIMIT_SECONDS
    assert task.soft_time_limit < task.time_limit, "soft must fire before the hard kill"
    # Comfortably above the slowest real run observed (104s).
    assert task.soft_time_limit >= 120


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
            return [(conn_a, user, None, {}), (conn_b, user, None, {})]

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

    async def _fake_sync_one(session_maker, connection_id, user_id, **_kw):
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
            return [(conn, user, None, {})]

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

    async def _boom(session_maker, connection_id, user_id, **_kw):
        raise RuntimeError("provider refused")

    monkeypatch.setattr(sync_tasks, "_sync_one", _boom)

    queued: list[set] = []
    monkeypatch.setattr(sync_tasks, "_queue_auto_categorize", queued.append)

    synced = await sync_tasks._sync_all()

    assert synced == 0
    assert queued == [set()], "nothing synced, so nothing to categorize"
