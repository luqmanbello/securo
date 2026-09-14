"""POST /api/transactions/auto-categorize and its status endpoint."""
import uuid

import pytest

from app.services import auto_categorize_service
from app.services.auto_categorize_service import AutoCategorizeResult
from app.tasks import categorize_tasks

ENDPOINT = "/api/transactions/auto-categorize"


class _QueuedTask:
    def __init__(self, task_id: str):
        self.id = task_id


def _install_dispatch(monkeypatch, *, raises: Exception | None = None):
    """Replace the celery dispatch with a recorder. Returns the call list."""
    calls: list[tuple[str, str]] = []

    def _delay(workspace_id, user_id):
        if raises is not None:
            raise raises
        calls.append((workspace_id, user_id))
        return _QueuedTask("11111111-2222-3333-4444-555555555555")

    monkeypatch.setattr(categorize_tasks.auto_categorize_workspace_task, "delay", _delay)
    return calls


def _install_preflight(monkeypatch, result):
    async def _fake(session, workspace_id, user_id, **_k):
        return result

    monkeypatch.setattr(auto_categorize_service, "preflight", _fake)


class _FakeAsyncResult:
    """Stands in for celery.result.AsyncResult without a broker."""

    def __init__(self, *, ready: bool, failed: bool = False, result=None):
        self._ready = ready
        self._failed = failed
        self.result = result

    def ready(self):
        return self._ready

    def failed(self):
        return self._failed


def _install_async_result(monkeypatch, fake):
    import celery.result

    monkeypatch.setattr(celery.result, "AsyncResult", lambda *_a, **_k: fake)


# --- Access -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_requires_authentication(client):
    response = await client.post(ENDPOINT)
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_viewers_cannot_trigger_it(client, viewer_auth_headers, monkeypatch):
    """It writes categories, so it sits behind the same write gate as every
    other mutation — asserted over HTTP because the gate is route wiring."""
    calls = _install_dispatch(monkeypatch)
    _install_preflight(monkeypatch, None)

    response = await client.post(ENDPOINT, headers=viewer_auth_headers)

    assert response.status_code == 403
    assert calls == []


# --- Starting a run ------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_for_the_model_is_queued_not_awaited(client, auth_headers, test_workspace, monkeypatch):
    """The whole point of the change: the request returns at once with a task
    id instead of holding the connection open for the model, which nginx cuts
    at 60s."""
    calls = _install_dispatch(monkeypatch)
    _install_preflight(monkeypatch, None)

    response = await client.post(ENDPOINT, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "queued", "task_id": "11111111-2222-3333-4444-555555555555"}
    assert len(calls) == 1
    assert calls[0][0] == str(test_workspace.id)


@pytest.mark.asyncio
async def test_a_cheap_answer_comes_back_immediately_without_queueing(client, auth_headers, monkeypatch):
    """Agents off, nothing to do, no connection: no reason to make anyone wait
    for a worker to discover the same thing."""
    calls = _install_dispatch(monkeypatch)
    _install_preflight(monkeypatch, AutoCategorizeResult(status="no_candidates"))

    response = await client.post(ENDPOINT, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "no_candidates"
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_feature_is_a_200_with_a_status_not_an_error(client, auth_headers, monkeypatch):
    calls = _install_dispatch(monkeypatch)
    _install_preflight(
        monkeypatch, AutoCategorizeResult(status="disabled", detail="Agents feature is disabled")
    )

    response = await client.post(ENDPOINT, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert calls == []


@pytest.mark.asyncio
async def test_an_unreachable_queue_is_a_503_not_a_silent_spinner(client, auth_headers, monkeypatch):
    _install_dispatch(monkeypatch, raises=ConnectionError("redis down"))
    _install_preflight(monkeypatch, None)

    response = await client.post(ENDPOINT, headers=auth_headers)

    assert response.status_code == 503


# --- Polling ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_running_until_the_worker_finishes(client, auth_headers, monkeypatch):
    _install_async_result(monkeypatch, _FakeAsyncResult(ready=False))

    response = await client.get(f"{ENDPOINT}/{uuid.uuid4()}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"status": "running"}


@pytest.mark.asyncio
async def test_status_returns_the_finished_result_without_its_workspace_stamp(
    client, auth_headers, test_workspace, monkeypatch
):
    finished = {
        "status": "ok",
        "considered": 4,
        "categorized": 3,
        "skipped_low_confidence": 1,
        "detail": "",
        "by_category": {"Groceries": 3},
        "workspace_id": str(test_workspace.id),
    }
    _install_async_result(monkeypatch, _FakeAsyncResult(ready=True, result=finished))

    response = await client.get(f"{ENDPOINT}/{uuid.uuid4()}", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["categorized"] == 3
    assert body["skipped_low_confidence"] == 1
    assert "workspace_id" not in body


@pytest.mark.asyncio
async def test_another_workspaces_result_is_not_shown(client, auth_headers, monkeypatch):
    """A task id alone must not be enough to read someone else's outcome."""
    foreign = {"status": "ok", "categorized": 9, "workspace_id": str(uuid.uuid4())}
    _install_async_result(monkeypatch, _FakeAsyncResult(ready=True, result=foreign))

    response = await client.get(f"{ENDPOINT}/{uuid.uuid4()}", headers=auth_headers)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_a_run_killed_by_the_time_limit_reads_as_an_error(client, auth_headers, monkeypatch):
    _install_async_result(monkeypatch, _FakeAsyncResult(ready=True, failed=True))

    response = await client.get(f"{ENDPOINT}/{uuid.uuid4()}", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "error"


@pytest.mark.asyncio
async def test_status_rejects_a_task_id_that_is_not_a_uuid(client, auth_headers):
    response = await client.get(f"{ENDPOINT}/not-a-task", headers=auth_headers)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_status_requires_authentication(client):
    response = await client.get(f"{ENDPOINT}/{uuid.uuid4()}")
    assert response.status_code in (401, 403)
