"""The Bybit provider plugin in `plugins/bybit`.

A port of the native provider (`app.providers.bybit`), so most of these guard
the same behaviour as `test_providers_bybit*.py`, and one test runs both on
the same Bybit responses and compares what they write. Every key, secret, id
and amount is synthetic; no test contacts Bybit.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bybit"
sys.path.insert(0, str(PLUGIN_DIR))

import bybit_api  # noqa: E402  # ty: ignore[import-not-found]
import bybit_ledger  # noqa: E402  # ty: ignore[import-not-found]
import bybit_plugin  # noqa: E402  # ty: ignore[import-not-found]

from app.plugins.contract import parse_payload  # noqa: E402
from app.plugins.manifest import parse_manifest  # noqa: E402
from app.providers.bybit import ACCOUNT_EXTERNAL_ID  # noqa: E402
from tests.test_providers_bybit import NOW_S, FakeBybit, _creds, _install, _ledger_row, _query_api  # noqa: E402

KEY = "synthetic-key-0003"
#: The state after the connection's first sync, which only links.
STARTED = {"started": True}
SECRET = "synthetic-secret-plugin-abcdefghijklmnopqrst"


def _client(handler, *, clock=lambda: float(NOW_S)):
    """A plugin BybitClient whose HTTP goes to `handler(httpx.Request)`: the native tests' fakes fit."""
    sleeps: list[float] = []

    def transport(method, url, headers, body):
        response = handler(httpx.Request(method, url, headers=headers, content=body))
        return response.status_code, response.content

    client = bybit_api.BybitClient(KEY, SECRET, base_url="https://api.bybit.test", transport=transport,
                                   sleep=sleeps.append, clock=clock)
    return client, sleeps


def _plugin(fake):
    return bybit_plugin.Plugin(client_factory=lambda key, secret: _client(fake)[0], clock=lambda: float(NOW_S))


def _context(key=KEY, secret=SECRET):
    return SimpleNamespace(secrets={"api_key": key, "api_secret": secret}, config={})


# ----- manifest ----------------------------------------------------------


def test_manifest_is_valid_and_declares_only_the_bybit_api():
    manifest = parse_manifest(json.loads((PLUGIN_DIR / "securo-plugin.json").read_text()))
    [integration] = manifest.contributes.integrations
    assert integration.provides == ["accounts", "transactions"]
    assert all(f.secret for f in integration.fields)
    assert [h.host for h in manifest.network] == ["api.bybit.com"]
    assert bybit_api.BASE_URL == "https://api.bybit.com"


# ----- client: allowlist, signing, retries --------------------------------


def test_allowlist_is_reads_only_and_covers_every_v5_literal():
    write_prefixes = (
        "/v5/order/", "/v5/position/", "/v5/asset/withdraw/create", "/v5/asset/withdraw/cancel",
        "/v5/asset/transfer/inter-transfer", "/v5/asset/transfer/universal-transfer",
        "/v5/user/update-api", "/v5/user/create", "/v5/user/delete", "/v5/earn/place-order",
    )
    allowed = {p for _, p in bybit_api.ALLOWED_ENDPOINTS}
    assert not any(p.startswith(write_prefixes) for p in allowed)
    assert {p for m, p in bybit_api.ALLOWED_ENDPOINTS if m == "POST"} == {
        "/v5/card/transaction/query-asset-records",
        "/v5/card/reward/points/records",
    }
    # Any string naming a v5 path in the client; any path literal in the rest (whose docs mention paths).
    for module, names_a_path in ((bybit_api, lambda s: "v5/" in s), (bybit_ledger, lambda s: s.startswith("/v5/")),
                                 (bybit_plugin, lambda s: s.startswith("/v5/"))):
        literals = {
            n.value for n in ast.walk(ast.parse(inspect.getsource(module)))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and names_a_path(n.value)
        }
        assert literals <= allowed, literals - allowed


def test_unlisted_endpoint_is_refused_before_any_io():
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("network was touched")

    client, _ = _client(handler)
    with pytest.raises(bybit_api.BybitError) as exc:
        client.post("/v5/order/create", {"symbol": "X"}, "nope")
    assert exc.value.code == "forbidden_endpoint"


def test_requests_are_signed_over_the_exact_query_or_body_sent():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"retCode": 0, "result": {}})

    client, _ = _client(handler)
    client.get(bybit_api.PATH_QUERY_API, {"b": "2", "a": "1"}, "query_api")
    client.post(bybit_api.PATH_CARD_RECORDS, {"type": "SIDE_QUERY_AUTH_ALL", "page": 1}, "card")
    get, post = seen
    assert get.url.query.decode() == "b=2&a=1"
    assert post.content.decode() == '{"type":"SIDE_QUERY_AUTH_ALL","page":1}'
    for request, payload in ((get, "b=2&a=1"), (post, post.content.decode())):
        h = request.headers
        assert h["X-BAPI-API-KEY"] == KEY and h["X-BAPI-RECV-WINDOW"] == "5000"
        message = (h["X-BAPI-TIMESTAMP"] + KEY + "5000" + payload).encode()
        assert h["X-BAPI-SIGN"] == hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()


def test_rate_limit_backs_off_then_raises_rate_limited():
    client, sleeps = _client(lambda request: httpx.Response(403))
    with pytest.raises(bybit_api.RateLimited):
        client.get(bybit_api.PATH_QUERY_API, {}, "query_api")
    assert sleeps == [1.5, 10, 20]


def test_timestamp_error_resyncs_the_clock_once():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "result": {}, "time": 1_700_000_090_000})
        if calls.count(bybit_api.PATH_QUERY_API) == 1:
            return httpx.Response(200, json={"retCode": 10002, "retMsg": "time window"})
        return httpx.Response(200, json={"retCode": 0, "result": {"ok": 1}})

    client, _ = _client(handler)
    assert client.get(bybit_api.PATH_QUERY_API, {}, "query_api") == {"ok": 1}
    assert calls == [bybit_api.PATH_QUERY_API, "/v5/market/time", bybit_api.PATH_QUERY_API]


def test_network_errors_and_bad_bodies_carry_no_secret():
    def broken(method, url, headers, body):
        raise OSError("refused " + SECRET)

    client = bybit_api.BybitClient(KEY, SECRET, transport=broken, sleep=lambda s: None)
    with pytest.raises(bybit_api.BybitError) as exc:
        client.get(bybit_api.PATH_QUERY_API, {}, "query_api")
    assert exc.value.code == "network" and exc.value.__cause__ is None

    leaky, _ = _client(lambda request: httpx.Response(200, content=b"{not json " + SECRET.encode()))
    with pytest.raises(bybit_api.BybitError) as exc:
        leaky.get(bybit_api.PATH_QUERY_API, {}, "query_api")
    assert SECRET not in repr(exc.value) and exc.value.__suppress_context__ is True


# ----- ledger: classification, clusters, refunds, cashback, drift ----------

T0 = 1_760_000_000
NOW = T0 + 30 * 86400
SINCE = T0 - 86400


def L(cursor, busi, desc, currency, io, amt, ts, after="0"):
    row = bybit_ledger.parse_ledger_row({
        "currency": currency, "ioDirection": io, "txnAmt": amt, "afterAmt": after, "createTime": str(ts),
        "showBusiTypeEn": busi, "descriptionEn": desc, "currcCursor": cursor,
    })
    assert row is not None
    return row


def card(txn="t1", ts=T0, merch="SYNTH SHOP", basic="50.00", paid="43.00", paid_cur="EUR", sold=True, side="1"):
    return {"txnId": txn, "txnCreate": str(ts * 1000), "merchName": merch, "basicAmount": basic, "paidAmount": paid,
            "paidCurrency": paid_cur, "mccCode": "5999", "merchCategoryDesc": "Misc retail", "side": side,
            "cryptoSold": sold, "uid": "424242", "pan6": "411111"}


def auto_earn_purchase(ts, base="50.00", prefix="p"):
    b = Decimal(base)
    return [
        L(f"{prefix}1", "Bybit Card", "Sale", "USDT", "O", str(b * Decimal("1.006")), ts),
        L(f"{prefix}2", "Earn", "Easy Earn card redemption", "USDT", "I", str(b * Decimal("1.006")), ts),
        L(f"{prefix}3", "Bybit Card", "Purchase", "USDT", "O", str(b * Decimal("0.008")), ts),
        L(f"{prefix}4", "Bybit Card", "Purchase", "USD", "O", base, ts + 1),
        L(f"{prefix}5", "Bybit Card", "Coin Purchase", "USD", "I", base, ts + 1),
    ]


def build(rows, *, purchases=(), refunds=(), redemption_ts=(), card_transient_failure=False, internal_cursors=()):
    return bybit_ledger.build_transactions(
        rows, since_ts=SINCE, now_ts=NOW, purchases=list(purchases), refunds=list(refunds),
        redemption_ts=list(redemption_ts), card_transient_failure=card_transient_failure,
        internal_cursors=set(internal_cursors),
    )


@pytest.mark.parametrize("busi,desc,expected", [
    ("Bybit Card", "Sale", ("card", None)),
    ("Earn", "Easy Earn | Flexible (Auto-Earn)", ("skip", None)),
    ("Earn", "Easy Earn card redemption", ("skip", None)),
    ("Earn", "Easy Earn | Flexible Interest Distribution", ("book", "Bybit Earn interest")),
    ("Deposit", "Deposit", ("book", "Bybit deposit")),
    ("Withdraw", "Withdrawal", ("book", "Bybit withdrawal")),
    ("Fiat", "Canceled P2P Sale", ("book", "Bybit P2P sale cancelled")),
    ("Airdrop", "Airdrop Bonus", ("book", "Bybit bonus")),
    ("Something New", "Mystery", ("unknown", "Mystery")),
])
def test_classify(busi, desc, expected):
    assert bybit_ledger.classify(L("c", busi, desc, "USDT", "I", "1", T0)) == expected


def test_auto_earn_purchase_is_one_enriched_debit_whatever_the_card_api_says():
    rows = auto_earn_purchase(T0)
    res = build(rows, purchases=[bybit_ledger.parse_card_record(card())])
    [tx] = res.transactions
    assert (tx.external_id, tx.type, tx.amount) == ("bybit-card:p1", "debit", Decimal("50.70"))
    assert (tx.description, tx.payee) == ("SYNTH SHOP (EUR 43.00)", "SYNTH SHOP")
    assert "411111" not in json.dumps(tx.raw_data)
    assert res.buckets["p2"] == "skipped" and res.buckets["p4"] == "usd_pair"
    [bare] = build(rows).transactions
    assert (bare.external_id, bare.amount, bare.description, bare.payee) == ("bybit-card:p1", Decimal("50.70"), "Bybit Card", None)


def test_young_card_clusters_wait_and_wait_longer_when_the_card_api_failed():
    assert build(auto_earn_purchase(NOW - 60)).transactions == []
    young = auto_earn_purchase(NOW - 3600)
    assert build(young, card_transient_failure=True).transactions == []
    assert len(build(young).transactions) == 1


def test_refund_cluster_is_a_credit_named_from_refund_records():
    rec = bybit_ledger.parse_card_record(card(txn="rf", ts=T0 - 86400, basic="12.34", sold=False, side="5"), refund=True)
    [tx] = build([L("r1", "Bybit Card", "Refund", "USDT", "I", "12.34", T0)], refunds=[rec]).transactions
    assert (tx.type, tx.amount, tx.payee) == ("credit", Decimal("12.34"), "SYNTH SHOP")


def test_airdrop_after_points_redemption_is_cashback():
    rows = [L("a1", "Airdrop", "Airdrop Bonus", "USDT", "I", "1.00", T0)]
    assert build(rows, redemption_ts=[T0 - 3000]).transactions[0].description == "Bybit Card cashback"
    assert build(rows, redemption_ts=[T0 - 9000]).transactions[0].description == "Bybit bonus"


def test_internal_transfers_are_skipped_and_unknown_types_imported():
    rows = [L("i1", "Transfer", "To Unified", "USDT", "O", "5", T0), L("i2", "Brand New", "Thing", "USDT", "I", "2", T0)]
    res = build(rows, internal_cursors={"i1"})
    assert [t.external_id for t in res.transactions] == ["bybit:i2"]
    assert res.unknown_types == {"Brand New/Thing"}


def test_drift_check_flags_a_wrong_total_and_a_missing_ledger_row():
    rows = [L("w1", "Withdraw", "Withdrawal", "USDT", "O", "3", T0)]
    buckets = build(rows).buckets
    assert bybit_ledger.drift_warnings(rows, buckets, emitted_total=Decimal("-3.00")) == []
    assert bybit_ledger.drift_warnings(rows, buckets, emitted_total=Decimal("-2.00")) != []
    chain = [
        L("g1", "Deposit", "Deposit", "USDT", "I", "100", T0, after="100"),
        L("g2", "Withdraw", "Withdrawal", "USDT", "O", "30", T0 + 100, after="70"),
        L("g3", "Withdraw", "Withdrawal", "USDT", "O", "10", T0 + 200, after="60"),
    ]
    assert build(chain).warnings == []
    assert any("running balance" in w for w in build([chain[0], chain[2]]).warnings)


# ----- the plugin's sync ---------------------------------------------------


def test_first_sync_only_links_and_calls_nothing():
    fake = FakeBybit()
    reply = _plugin(fake).sync({"since": None, "state": {}, "last_sync_at": None}, _context())
    assert reply == {"state": STARTED, "data": None}
    assert fake.calls == []
    assert _plugin(FakeBybit()).sync({"since": None, "state": reply["state"]}, _context())["state"]["started"] is True


@pytest.mark.parametrize("code", [10003, 33004, 10005])
def test_expired_or_revoked_key_needs_auth(code):
    class Expired(FakeBybit):
        def __call__(self, request):
            if request.url.path == "/v5/user/query-api":
                return httpx.Response(200, json={"retCode": code, "retMsg": "x"})
            return super().__call__(request)

    with pytest.raises(bybit_plugin.NeedsAuth):
        _plugin(Expired()).sync({"since": None, "state": STARTED}, _context())


def test_missing_secret_needs_auth_before_any_request():
    fake = FakeBybit()
    with pytest.raises(bybit_plugin.NeedsAuth):
        _plugin(fake).sync({"since": None, "state": STARTED}, _context(secret=""))
    assert fake.calls == []


@pytest.mark.parametrize("query_api", [_query_api(read_only=0), _query_api(perms={"BitCard": ["BitCard"]})])
def test_a_key_that_can_trade_or_lacks_earn_is_refused(query_api):
    with pytest.raises(bybit_plugin.KeyNotAllowed):
        _plugin(FakeBybit(query_api=query_api)).sync({"since": None, "state": STARTED}, _context())


def test_first_sync_walks_52_weeks_in_7_day_windows():
    fake = FakeBybit()
    _plugin(fake).sync({"since": None, "state": STARTED}, _context())
    assert len([c for c in fake.calls if c[1] == "/v5/asset/fundinghistory"]) == 53


def test_a_changed_response_shape_is_a_schema_error_without_values():
    class Odd(FakeBybit):
        def __call__(self, request):
            if request.url.path == "/v5/account/wallet-balance":
                return httpx.Response(200, json={"retCode": 0, "result": {"list": [SECRET]}})
            return super().__call__(request)

    with pytest.raises(bybit_api.BybitError) as exc:
        _plugin(Odd()).sync({"since": "2025-10-01", "state": STARTED}, _context())
    assert (exc.value.stage, exc.value.code) == ("accounts", "schema")
    assert SECRET not in str(exc.value) and exc.value.__suppress_context__ is True


# ----- the same Bybit, native and plugin ------------------------------------


def _equivalence_bybit() -> FakeBybit:
    t = NOW_S - 5 * 86400
    ledger = [
        # A card purchase out of Earn, enriched from the card record.
        _ledger_row("c1", "Bybit Card", "Sale", "O", "50.30", t),
        _ledger_row("c2", "Earn", "Easy Earn card redemption", "I", "50.30", t),
        _ledger_row("c3", "Bybit Card", "Purchase", "O", "0.40", t),
        _ledger_row("c4", "Bybit Card", "Purchase", "O", "50.00", t + 1, currency="USD"),
        _ledger_row("c5", "Bybit Card", "Coin Purchase", "I", "50.00", t + 1, currency="USD"),
        # A refund, a cashback airdrop after a points redemption, money in and out.
        _ledger_row("r1", "Bybit Card", "Refund", "I", "12.34", t + 3600),
        _ledger_row("a1", "Airdrop", "Airdrop Bonus", "I", "1.00", t + 7200),
        _ledger_row("i1", "Earn", "Easy Earn | Flexible Interest Distribution", "I", "0.42", t + 8000),
        _ledger_row("d1", "Deposit", "Deposit", "I", "200", t - 86400),
        _ledger_row("w1", "Withdraw", "Withdrawal", "O", "25.5", t - 3600),
        _ledger_row("u1", "Brand New", "Thing", "I", "2", t + 9000, currency="USDC"),
        # Weeks back: only a first sync reaches it.
        _ledger_row("old", "Deposit", "Deposit", "I", "10", NOW_S - 40 * 86400),
        _ledger_row("btc", "Deposit", "Deposit", "I", "1", t, currency="BTC"),
    ]
    cards = [card(txn="tx1", ts=t, basic="50.00")]
    refunds = [card(txn="rf1", ts=t - 86400, merch="SYNTH REFUND", basic="12.34", sold=False, side="5")]
    points = [{"side": "2", "createTime": str((t + 6000) * 1000)}]
    return FakeBybit(ledger=ledger, cards=cards, refunds=refunds, points=points)


def _shape(tx) -> tuple:
    return (tx["external_id"], Decimal(str(tx["amount"])), tx["type"], str(tx["date"]),
            tx["description"], tx["payee"], tx["currency"], tx["status"])


@pytest.mark.asyncio
@pytest.mark.parametrize("since", [None, date(2025, 10, 1)])
async def test_plugin_writes_what_the_native_provider_writes(since):
    from app.providers.bybit import BybitProvider

    a, b, c = _install(_equivalence_bybit())
    with a, b, c:
        native = BybitProvider()
        [native_account] = await native.get_accounts(_creds())
        native_txs = await native.get_transactions(_creds(), ACCOUNT_EXTERNAL_ID, since)

    reply = _plugin(_equivalence_bybit()).sync(
        {"since": since.isoformat() if since else None, "state": STARTED, "last_sync_at": None}, _context()
    )
    # Through JSON, as the runner sends it.
    data = json.loads(json.dumps(reply["data"]))
    payload = parse_payload(data, ["accounts", "transactions"])
    assert payload is not None and payload.movement_ids_are_unique is True
    [account] = payload.accounts or []
    assert (account.external_id, account.name, account.type, account.currency, account.balance) == (
        native_account.external_id, native_account.name, native_account.type, native_account.currency, native_account.balance
    )

    plugin_txs = sorted(_shape(tx) for tx in data["accounts"][0]["transactions"])
    expected = sorted(_shape(vars(tx)) for tx in native_txs)
    assert plugin_txs == expected
    assert {"bybit-card:c1", "bybit-card:r1", "bybit:a1", "bybit:d1"} <= {t[0] for t in plugin_txs}
    assert ("old" in {t[0].split(":")[1] for t in plugin_txs}) is (since is None)
    by_id = {tx.external_id: tx for tx in account.transactions or []}
    native_by_id = {tx.external_id: tx for tx in native_txs}
    assert {k: v.raw_data for k, v in by_id.items()} == {k: v.raw_data for k, v in native_by_id.items()}
