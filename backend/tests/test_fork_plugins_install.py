"""The fork's own bank plugins install from the private catalog's real archives.

scripts/build-plugin-index.py packs plugins/*/ into deploy/plugin-index/. Each
archive is installed on the real (in-process) runner and connected; the first
sync only links (it writes nothing), which is what lets existing native
accounts be moved onto the new connection before any data arrives.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account
from app.plugins import connections
from tests import test_code_plugins as code_plugins
from tests.test_code_plugins import PLUGIN, REPO, V1, _apply
from tests.test_provider_plugins import _bank, _rows

catalog = code_plugins.catalog
plugin_runner = code_plugins.plugin_runner
sync_locks = code_plugins.sync_locks
ids = code_plugins.ids

INDEX = Path(__file__).resolve().parents[2] / "deploy" / "plugin-index"
VALUES = {
    "accessbank": {"user_id": "synthetic-user", "password": "synthetic-password"},
    "bybit": {"api_key": "synthetic-key", "api_secret": "synthetic-secret"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", sorted(VALUES))
async def test_a_fork_plugin_installs_and_its_first_sync_only_links(
    session: AsyncSession, ids, catalog, plugin_runner, slug
):
    manifest = json.loads((INDEX / f"{slug}.manifest.json").read_text())
    catalog.manifests[V1] = {**manifest, "id": PLUGIN, "repository": REPO}
    catalog.archives[V1] = (INDEX / f"{slug}.tar.gz").read_bytes()
    workspace_id, user_id = ids
    await _apply(session, workspace_id, user_id)
    (integration,) = manifest["contributes"]["integrations"]
    connection, _ = await connections.create(
        session, workspace_id=workspace_id, user_id=user_id, can_write=True, plugin_id=PLUGIN,
        integration_key=integration["key"], name="", values=VALUES[slug], lang="en",
    )

    await connections.sync(session, connection)

    assert (connection.status, connection.last_error) == ("active", None)
    assert connection.state == {"started": True}
    bank = await _bank(session, connection)
    assert await _rows(session, Account, connection_id=bank.id) == []
