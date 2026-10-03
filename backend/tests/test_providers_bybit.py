"""Bybit provider tests.

Every key, secret, id and amount here is synthetic. All HTTP is served by
httpx.MockTransport. No test contacts Bybit.
"""
from __future__ import annotations

from app.providers import KNOWN_PROVIDERS, all_known_providers


def test_bybit_is_a_known_credentials_provider_with_its_own_fields():
    entry = next(p for p in KNOWN_PROVIDERS if p["name"] == "bybit")
    assert entry["flow_type"] == "credentials"
    assert entry["display_name"] == "Bybit"
    fields = entry["credential_fields"]
    assert [f["name"] for f in fields] == ["api_key", "api_secret"]
    secret = next(f for f in fields if f["name"] == "api_secret")
    assert secret["secret"] is True
    assert all(f["label_key"].startswith("accounts.credentialsConnect.bybit.") for f in fields)
    assert any(p["name"] == "bybit" for p in all_known_providers())


def test_accessbank_entry_is_unchanged_and_has_no_credential_fields():
    entry = next(p for p in KNOWN_PROVIDERS if p["name"] == "accessbank")
    assert "credential_fields" not in entry


def test_bybit_settings_defaults():
    from app.core.config import Settings

    s = Settings()
    assert s.bybit_enabled is False
    assert s.bybit_base_url == "https://api.bybit.com"
