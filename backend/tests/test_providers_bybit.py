"""Bybit provider tests.

Every key, secret, id and amount here is synthetic. All HTTP is served by
httpx.MockTransport. No test contacts Bybit.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import cast
from unittest.mock import patch

import httpx
import pytest

from app.agents.services.crypto import encrypt
from app.providers import KNOWN_PROVIDERS, all_known_providers
from app.providers.base import ProviderUserActionRequired, SessionExpiredError
from app.providers.bybit import ACCOUNT_EXTERNAL_ID, BybitProvider
from app.providers.bybit_client import BybitError


def test_bybit_is_a_known_credentials_provider_with_its_own_fields():
    entry = next(p for p in KNOWN_PROVIDERS if p["name"] == "bybit")
    assert entry["flow_type"] == "credentials"
    assert entry["display_name"] == "Bybit"
    fields = cast(list[dict[str, object]], entry["credential_fields"])
    assert [f["name"] for f in fields] == ["api_key", "api_secret"]
    secret = next(f for f in fields if f["name"] == "api_secret")
    assert secret["secret"] is True
    assert all(str(f["label_key"]).startswith("accounts.credentialsConnect.bybit.") for f in fields)
    assert any(p["name"] == "bybit" for p in all_known_providers())


def test_accessbank_entry_is_unchanged_and_has_no_credential_fields():
    entry = next(p for p in KNOWN_PROVIDERS if p["name"] == "accessbank")
    assert "credential_fields" not in entry


def test_bybit_settings_defaults():
    from app.core.config import Settings

    s = Settings()
    assert s.bybit_enabled is False
    assert s.bybit_base_url == "https://api.bybit.com"


FAKE_KEY = "synthetic-key-0002"
FAKE_SECRET = "synthetic-secret-zyxwvutsrqponmlkjihgfedcba"
NOW_S = 1_760_000_000


def _query_api(read_only=1, perms=None, deadline=60, uid=777000):
    perms = perms if perms is not None else {"Wallet": ["AccountTransfer"], "Earn": ["Earn"], "BitCard": ["BitCard"], "Spot": ["SpotTrade"]}
    return {"readOnly": read_only, "permissions": perms, "deadlineDay": deadline, "expiredAt": "2030-01-01T00:00:00Z", "userID": uid}


class FakeBybit:
    """A synthetic Bybit. Routes by path; records every call."""

    def __init__(self, *, ledger=None, cards=None, refunds=None, points=None, query_api=None, fund=None, uta=None, earn=None,
                 card_code=0, ledger_pages=None):
        self.ledger = ledger or []
        self.cards = cards or []
        self.refunds = refunds or []
        self.points = points or []
        self.query_api = query_api or _query_api()
        self.fund = fund if fund is not None else [{"coin": "USDT", "walletBalance": "100.10"}, {"coin": "BTC", "walletBalance": "1"}]
        self.uta = uta if uta is not None else [{"coin": "USDC", "walletBalance": "5"}]
        self.earn = earn if earn is not None else [{"coin": "USDT", "amount": "20", "claimableYield": "0.01"}]
        self.card_code = card_code
        self.ledger_pages = ledger_pages
        self.calls: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))

        def ok(result):
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})

        if path == "/v5/user/query-api":
            return ok(self.query_api)
        if path == "/v5/asset/transfer/query-account-coins-balance":
            return ok({"accountType": "FUND", "balance": self.fund})
        if path == "/v5/account/wallet-balance":
            return ok({"list": [{"coin": self.uta}]})
        if path == "/v5/earn/position":
            return ok({"list": self.earn if request.url.params.get("category") == "FlexibleSaving" else []})
        if path == "/v5/asset/fundinghistory":
            start, end = int(request.url.params["createTimeFrom"]), int(request.url.params["createTimeTo"])
            rows = [r for r in self.ledger if start <= int(r["createTime"]) <= end]
            cursor = request.url.params.get("cursor")
            if self.ledger_pages and rows:
                half = len(rows) // 2 or 1
                if cursor == "page2":
                    return ok({"list": rows[half:], "nextPageCursor": ""})
                return ok({"list": rows[:half], "nextPageCursor": "page2" if rows[half:] else ""})
            return ok({"list": rows, "nextPageCursor": ""})
        if path == "/v5/asset/transfer/query-inter-transfer-list":
            return ok({"list": [], "nextPageCursor": ""})
        if path == "/v5/card/transaction/query-asset-records":
            if self.card_code:
                return httpx.Response(200, json={"retCode": self.card_code, "retMsg": "x"})
            body = json.loads(request.content)
            data = self.refunds if body["type"] == "SIDE_QUERY_FINANCIAL_REFUND" else self.cards
            return ok({"pageSize": 500, "pageNo": body["page"], "totalCount": len(data), "data": data if body["page"] == 1 else []})
        if path == "/v5/card/reward/points/records":
            body = json.loads(request.content)
            return ok({"pageSize": 50, "pageNo": body["pageNo"], "totalCount": len(self.points), "data": self.points if body["pageNo"] == 1 else []})
        return httpx.Response(404)


def _install(fake: FakeBybit):
    """Patch the provider's three seams: HTTP client, Bybit client (no real
    sleeping, fixed clock) and `_now`. Nothing global is patched."""
    from app.providers.bybit_client import BybitClient

    transport = httpx.MockTransport(fake)

    def _fake_client(self):
        return httpx.AsyncClient(transport=transport, base_url="https://api.bybit.test")

    async def _no_sleep(_s):
        return None

    def _fake_bybit(self, http, key, secret):
        return BybitClient(http, key, secret, sleep=_no_sleep, clock=lambda: float(NOW_S))

    return (
        patch.object(BybitProvider, "_client", _fake_client),
        patch.object(BybitProvider, "_bybit", _fake_bybit),
        patch.object(BybitProvider, "_now", lambda self: NOW_S),
    )


def _creds():
    return {"api_key_enc": encrypt(FAKE_KEY), "api_secret_enc": encrypt(FAKE_SECRET), "key_expires_at": None}


def _ledger_row(cursor, busi, desc, io, amt, ts, currency="USDT"):
    return {"currency": currency, "ioDirection": io, "txnAmt": amt, "afterAmt": "0", "createTime": str(ts),
            "showBusiTypeEn": busi, "descriptionEn": desc, "currcCursor": cursor, "memberId": "1"}


async def _run(fake, coro_fn):
    a, b, c = _install(fake)
    with a, b, c:
        return await coro_fn(BybitProvider())


@pytest.mark.asyncio
async def test_claim_stores_only_encrypted_credentials_and_returns_one_usd_account():
    fake = FakeBybit()
    code = json.dumps({"api_key": f"  {FAKE_KEY} ", "api_secret": FAKE_SECRET})
    data = await _run(fake, lambda p: p.handle_oauth_callback(code))
    assert data.institution_name == "Bybit"
    assert data.external_id.startswith("bybit:") and "777000" not in data.external_id
    assert set(data.credentials) == {"api_key_enc", "api_secret_enc", "key_expires_at"}
    assert FAKE_KEY not in json.dumps(data.credentials) and FAKE_SECRET not in json.dumps(data.credentials)
    assert data.credentials["key_expires_at"] == "2030-01-01T00:00:00Z"
    [acc] = data.accounts
    assert (acc.external_id, acc.name, acc.type, acc.currency) == (ACCOUNT_EXTERNAL_ID, "Bybit", "checking", "USD")
    assert acc.balance == Decimal("125.11")  # 100.10 FUND USDT + 5 UTA USDC + 20 + 0.01 Earn; BTC ignored


@pytest.mark.asyncio
async def test_claim_refuses_a_key_that_can_trade():
    fake = FakeBybit(query_api=_query_api(read_only=0))
    with pytest.raises(ProviderUserActionRequired) as exc:
        await _run(fake, lambda p: p.handle_oauth_callback(json.dumps({"api_key": FAKE_KEY, "api_secret": FAKE_SECRET})))
    assert exc.value.code == "bybit_key_not_read_only"


@pytest.mark.asyncio
async def test_claim_refuses_a_key_without_earn():
    fake = FakeBybit(query_api=_query_api(perms={"BitCard": ["BitCard"]}))
    with pytest.raises(ProviderUserActionRequired) as exc:
        await _run(fake, lambda p: p.handle_oauth_callback(json.dumps({"api_key": FAKE_KEY, "api_secret": FAKE_SECRET})))
    assert exc.value.code == "bybit_key_missing_earn"


@pytest.mark.asyncio
async def test_claim_with_missing_fields_asks_the_user():
    with pytest.raises(ProviderUserActionRequired) as exc:
        await _run(FakeBybit(), lambda p: p.handle_oauth_callback(json.dumps({"api_key": FAKE_KEY})))
    assert exc.value.code == "bybit_credential_missing"


@pytest.mark.asyncio
async def test_refresh_refuses_a_key_edited_to_read_write():
    fake = FakeBybit(query_api=_query_api(read_only=0))
    with pytest.raises(ProviderUserActionRequired):
        await _run(fake, lambda p: p.refresh_credentials(_creds()))


@pytest.mark.asyncio
async def test_refresh_updates_expiry_and_ip_bound_keys_have_none():
    fresh = await _run(FakeBybit(), lambda p: p.refresh_credentials(_creds()))
    assert fresh["key_expires_at"] == "2030-01-01T00:00:00Z"
    bound = FakeBybit(query_api={**_query_api(), "deadlineDay": -2, "expiredAt": "1970-01-01T00:00:00Z"})
    assert (await _run(bound, lambda p: p.refresh_credentials(_creds())))["key_expires_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [10003, 33004, 10005])
async def test_expired_or_revoked_key_is_session_expired(code):
    class Expired(FakeBybit):
        def __call__(self, request):
            if request.url.path == "/v5/user/query-api":
                return httpx.Response(200, json={"retCode": code, "retMsg": "x"})
            return super().__call__(request)

    with pytest.raises(SessionExpiredError):
        await _run(Expired(), lambda p: p.refresh_credentials(_creds()))


@pytest.mark.asyncio
async def test_unreadable_secret_asks_the_user_before_any_network_call():
    fake = FakeBybit()
    creds = {**_creds(), "api_secret_enc": "not-a-fernet-token"}
    with pytest.raises(ProviderUserActionRequired) as exc:
        await _run(fake, lambda p: p.get_accounts(creds))
    assert exc.value.code == "bybit_credential_unreadable"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_get_transactions_end_to_end():
    t = NOW_S - 5 * 86400
    ledger = [
        _ledger_row("c1", "Bybit Card", "Sale", "O", "50.30", t),
        _ledger_row("c2", "Earn", "Easy Earn card redemption", "I", "50.30", t),
        _ledger_row("c3", "Bybit Card", "Purchase", "O", "0.40", t),
        _ledger_row("c4", "Bybit Card", "Purchase", "O", "50.00", t + 1, currency="USD"),
        _ledger_row("c5", "Bybit Card", "Coin Purchase", "I", "50.00", t + 1, currency="USD"),
        _ledger_row("d1", "Deposit", "Deposit", "I", "200", t - 86400),
        _ledger_row("btc", "Deposit", "Deposit", "I", "1", t, currency="BTC"),
    ]
    cards = [{"txnId": "tx1", "txnCreate": str(t * 1000), "merchName": "SYNTH SHOP", "basicAmount": "50.00", "paidAmount": "43.00",
              "paidCurrency": "EUR", "mccCode": "5999", "merchCategoryDesc": "Misc", "side": "1", "cryptoSold": True, "uid": "1", "pan6": "411111"}]
    fake = FakeBybit(ledger=ledger, cards=cards)
    txs = await _run(fake, lambda p: p.get_transactions(_creds(), ACCOUNT_EXTERNAL_ID, date(2025, 9, 1)))
    by_id = {tx.external_id: tx for tx in txs}
    assert set(by_id) == {"bybit-card:c1", "bybit:d1"}
    assert by_id["bybit-card:c1"].description == "SYNTH SHOP (EUR 43.00)"
    assert by_id["bybit-card:c1"].amount == Decimal("50.70")
    assert all(tx.currency == "USD" for tx in txs)
    assert ("POST", "/v5/card/transaction/query-asset-records") in fake.calls


@pytest.mark.asyncio
async def test_first_sync_walks_52_weeks_in_7_day_windows():
    fake = FakeBybit()
    await _run(fake, lambda p: p.get_transactions(_creds(), ACCOUNT_EXTERNAL_ID, None))
    ledger_calls = [c for c in fake.calls if c[1] == "/v5/asset/fundinghistory"]
    assert len(ledger_calls) == 53  # 52 weeks + the partial week up to now


@pytest.mark.asyncio
async def test_multi_page_ledger_windows_are_read_fully():
    t = NOW_S - 2 * 86400
    ledger = [_ledger_row(f"w{i}", "Withdraw", "Withdrawal", "O", "1", t + i * 100) for i in range(6)]
    fake = FakeBybit(ledger=ledger, ledger_pages=True)
    txs = await _run(fake, lambda p: p.get_transactions(_creds(), ACCOUNT_EXTERNAL_ID, date(2025, 10, 1)))
    assert len(txs) == 6


@pytest.mark.asyncio
async def test_card_api_down_holds_back_young_purchases_without_changing_ids():
    t = NOW_S - 3600
    ledger = [_ledger_row("k1", "Bybit Card", "Purchase", "O", "10", t)]
    down = await _run(FakeBybit(ledger=ledger, card_code=10006), lambda p: p.get_transactions(_creds(), ACCOUNT_EXTERNAL_ID, date(2025, 10, 1)))
    assert down == []
    up = await _run(FakeBybit(ledger=ledger), lambda p: p.get_transactions(_creds(), ACCOUNT_EXTERNAL_ID, date(2025, 10, 1)))
    assert [tx.external_id for tx in up] == ["bybit-card:k1"]


@pytest.mark.asyncio
async def test_missing_bitcard_writes_purchases_at_once():
    t = NOW_S - 3600
    ledger = [_ledger_row("k1", "Bybit Card", "Purchase", "O", "10", t)]
    txs = await _run(FakeBybit(ledger=ledger, card_code=10005), lambda p: p.get_transactions(_creds(), ACCOUNT_EXTERNAL_ID, date(2025, 10, 1)))
    assert [(tx.external_id, tx.description, tx.payee) for tx in txs] == [("bybit-card:k1", "Bybit Card", None)]


@pytest.mark.asyncio
async def test_no_exception_from_the_provider_carries_the_secret():
    class Broken(FakeBybit):
        def __call__(self, request):
            if request.url.path == "/v5/asset/transfer/query-account-coins-balance":
                return httpx.Response(200, content=b"{not json " + FAKE_SECRET.encode())
            return super().__call__(request)

    with pytest.raises(Exception) as exc:
        await _run(Broken(), lambda p: p.get_accounts(_creds()))
    seen, e = [], exc.value
    while e is not None and e not in seen:
        seen.append(e)
        e = e.__cause__ or e.__context__
    text = " ".join(repr(x) + str(x) for x in seen)
    assert FAKE_SECRET not in text and FAKE_KEY not in text


def test_get_oauth_url_is_not_supported():
    with pytest.raises(NotImplementedError):
        BybitProvider().get_oauth_url("x", "y")


def test_bybit_ids_are_unique_ledger_movements():
    assert BybitProvider.movement_ids_are_unique is True


@pytest.mark.asyncio
async def test_card_total_count_of_the_wrong_type_is_a_bybit_error():
    class Odd(FakeBybit):
        def __call__(self, request):
            if request.url.path == "/v5/card/transaction/query-asset-records":
                return httpx.Response(200, json={"retCode": 0, "result": {"data": [{"x": 1}], "totalCount": "n/a"}})
            return super().__call__(request)

    with pytest.raises(BybitError):
        await _run(Odd(), lambda p: p.get_transactions(_creds(), ACCOUNT_EXTERNAL_ID, date(2025, 10, 1)))


@pytest.mark.asyncio
async def test_malformed_unified_balance_is_a_bybit_error():
    class Odd(FakeBybit):
        def __call__(self, request):
            if request.url.path == "/v5/account/wallet-balance":
                return httpx.Response(200, json={"retCode": 0, "result": {"list": ["not-a-dict"]}})
            return super().__call__(request)

    with pytest.raises(BybitError):
        await _run(Odd(), lambda p: p.get_accounts(_creds()))


@pytest.mark.asyncio
async def test_permissions_not_a_dict_is_treated_as_missing_earn():
    fake = FakeBybit(query_api={**_query_api(), "permissions": ["Earn"]})
    with pytest.raises(ProviderUserActionRequired) as exc:
        await _run(fake, lambda p: p.handle_oauth_callback(json.dumps({"api_key": FAKE_KEY, "api_secret": FAKE_SECRET})))
    assert exc.value.code == "bybit_key_missing_earn"


@pytest.mark.asyncio
async def test_wrong_secret_at_connect_says_so():
    class BadSig(FakeBybit):
        def __call__(self, request):
            if request.url.path == "/v5/user/query-api":
                return httpx.Response(200, json={"retCode": 10004, "retMsg": "Error sign"})
            return super().__call__(request)

    with pytest.raises(ProviderUserActionRequired) as exc:
        await _run(BadSig(), lambda p: p.handle_oauth_callback(json.dumps({"api_key": FAKE_KEY, "api_secret": FAKE_SECRET})))
    assert exc.value.code == "bybit_secret_mismatch"


@pytest.mark.asyncio
async def test_empty_balance_strings_count_as_zero():
    # Seen live: Unified returns walletBalance "" for USD when the coin filter
    # names a coin the wallet has never held.
    fake = FakeBybit(uta=[{"coin": "USDT", "walletBalance": "0"}, {"coin": "USD", "walletBalance": ""}])
    [acc] = await _run(fake, lambda p: p.get_accounts(_creds()))
    assert acc.balance == Decimal("120.11")  # 100.10 FUND + 20.01 Earn
