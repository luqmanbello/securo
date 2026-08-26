"""POST /api/transactions/auto-categorize."""
import pytest

from app.services import auto_categorize_service
from app.services.auto_categorize_service import AutoCategorizeResult

ENDPOINT = "/api/transactions/auto-categorize"


@pytest.mark.asyncio
async def test_requires_authentication(client):
    response = await client.post(ENDPOINT)
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_viewers_cannot_trigger_it(client, viewer_auth_headers, monkeypatch):
    """It writes categories, so it sits behind the same write gate as
    every other mutation — asserted over HTTP because the gate is route
    wiring, not service logic."""
    called = False

    async def _spy(*_a, **_k):
        nonlocal called
        called = True
        return AutoCategorizeResult(status="ok")

    monkeypatch.setattr(auto_categorize_service, "auto_categorize_workspace", _spy)

    response = await client.post(ENDPOINT, headers=viewer_auth_headers)

    assert response.status_code == 403
    assert called is False


@pytest.mark.asyncio
async def test_returns_the_service_result(client, auth_headers, monkeypatch):
    async def _fake(session, workspace_id, user_id, **_k):
        return AutoCategorizeResult(
            status="ok",
            considered=4,
            categorized=3,
            skipped_low_confidence=1,
            by_category={"Groceries": 2, "Transport": 1},
        )

    monkeypatch.setattr(auto_categorize_service, "auto_categorize_workspace", _fake)

    response = await client.post(ENDPOINT, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["considered"] == 4
    assert body["categorized"] == 3
    assert body["skipped_low_confidence"] == 1
    assert body["by_category"] == {"Groceries": 2, "Transport": 1}


@pytest.mark.asyncio
async def test_disabled_feature_is_a_200_with_a_status_not_an_error(
    client, auth_headers, monkeypatch
):
    """The UI needs to tell the user *why* nothing happened, which a 500
    would not let it do."""

    async def _fake(session, workspace_id, user_id, **_k):
        return AutoCategorizeResult(status="disabled", detail="Agents feature is disabled")

    monkeypatch.setattr(auto_categorize_service, "auto_categorize_workspace", _fake)

    response = await client.post(ENDPOINT, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    assert response.json()["categorized"] == 0


@pytest.mark.asyncio
async def test_runs_against_the_callers_own_workspace(
    client, auth_headers, test_workspace, monkeypatch
):
    seen = {}

    async def _fake(session, workspace_id, user_id, **_k):
        seen["workspace_id"] = workspace_id
        return AutoCategorizeResult(status="ok")

    monkeypatch.setattr(auto_categorize_service, "auto_categorize_workspace", _fake)

    await client.post(ENDPOINT, headers=auth_headers)

    assert seen["workspace_id"] == test_workspace.id
