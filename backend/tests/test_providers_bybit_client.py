"""BybitClient tests. Synthetic keys only; all HTTP via MockTransport."""
from __future__ import annotations

import ast
import hashlib
import hmac
import inspect

import httpx
import pytest

import app.providers.bybit_client as mod
from app.providers.base import ProviderRateLimited
from app.providers.bybit_client import (
    ALLOWED_ENDPOINTS,
    PATH_CARD_RECORDS,
    PATH_QUERY_API,
    BybitClient,
    BybitError,
    sign,
)

KEY = "synthetic-key-0001"
SECRET = "synthetic-secret-abcdefghijklmnopqrstuvwxyz"


def _client(handler, *, clock=lambda: 1_700_000_000.0):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.bybit.test")
    return BybitClient(http, KEY, SECRET, sleep=fake_sleep, clock=clock), sleeps


def test_sign_is_hmac_sha256_hex_over_ts_key_recv_payload():
    expected = hmac.new(SECRET.encode(), b"1700000000000" + KEY.encode() + b"5000" + b"a=1&b=2", hashlib.sha256).hexdigest()
    assert sign(SECRET, "1700000000000", KEY, "5000", "a=1&b=2") == expected


@pytest.mark.asyncio
async def test_get_signs_the_exact_query_string_sent():
    seen = {}

    def handler(request):
        seen["query"] = request.url.query.decode()
        seen["headers"] = request.headers
        return httpx.Response(200, json={"retCode": 0, "result": {"ok": 1}})

    client, _ = _client(handler)
    result = await client.get(PATH_QUERY_API, {"b": "2", "a": "1"}, "query_api")
    assert result == {"ok": 1}
    h = seen["headers"]
    assert h["X-BAPI-API-KEY"] == KEY
    assert h["X-BAPI-RECV-WINDOW"] == "5000"
    assert h["X-BAPI-SIGN"] == sign(SECRET, h["X-BAPI-TIMESTAMP"], KEY, "5000", seen["query"])
    assert seen["query"] == "b=2&a=1"


@pytest.mark.asyncio
async def test_post_signs_the_compact_json_body_sent():
    seen = {}

    def handler(request):
        seen["body"] = request.content.decode()
        seen["headers"] = request.headers
        return httpx.Response(200, json={"retCode": 0, "result": {"data": []}})

    client, _ = _client(handler)
    await client.post(PATH_CARD_RECORDS, {"type": "SIDE_QUERY_AUTH_ALL", "page": 1}, "card")
    assert seen["body"] == '{"type":"SIDE_QUERY_AUTH_ALL","page":1}'
    assert seen["headers"]["Content-Type"] == "application/json"
    assert seen["headers"]["X-BAPI-SIGN"] == sign(SECRET, seen["headers"]["X-BAPI-TIMESTAMP"], KEY, "5000", seen["body"])


@pytest.mark.asyncio
async def test_unlisted_endpoint_is_refused_before_any_io():
    def handler(request):  # pragma: no cover - must never be called
        raise AssertionError("network was touched")

    client, _ = _client(handler)
    with pytest.raises(BybitError) as exc:
        await client.post("/v5/order/create", {"symbol": "X"}, "nope")
    assert exc.value.code == "forbidden_endpoint"
    with pytest.raises(BybitError):
        await client.get("/v5/asset/withdraw/create", {}, "nope")


def test_allowlist_is_reads_only():
    write_prefixes = (
        "/v5/order/", "/v5/position/", "/v5/asset/withdraw/create", "/v5/asset/withdraw/cancel",
        "/v5/asset/transfer/inter-transfer", "/v5/asset/transfer/universal-transfer",
        "/v5/user/update-api", "/v5/user/create", "/v5/user/delete", "/v5/earn/place-order",
    )
    for method, path in ALLOWED_ENDPOINTS:
        assert method in {"GET", "POST"}
        assert not path.startswith(write_prefixes), path
    assert {p for m, p in ALLOWED_ENDPOINTS if m == "POST"} == {
        "/v5/card/transaction/query-asset-records",
        "/v5/card/reward/points/records",
    }


def test_every_v5_literal_in_the_module_is_allowlisted():
    tree = ast.parse(inspect.getsource(mod))
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "v5/" in n.value
    }
    allowed_paths = {p for _, p in ALLOWED_ENDPOINTS}
    assert literals <= allowed_paths, literals - allowed_paths


@pytest.mark.asyncio
async def test_rate_limit_backs_off_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, json={"retCode": 10006, "retMsg": "Too many visits"})
        return httpx.Response(200, json={"retCode": 0, "result": {}})

    client, sleeps = _client(handler)
    await client.get(PATH_QUERY_API, {}, "query_api")
    assert sleeps[:2] == [1.5, 10]


@pytest.mark.asyncio
async def test_rate_limit_three_times_raises_provider_rate_limited():
    def handler(request):
        return httpx.Response(403, text="access too frequent")

    client, sleeps = _client(handler)
    with pytest.raises(ProviderRateLimited):
        await client.get(PATH_QUERY_API, {}, "query_api")
    assert sleeps == [1.5, 10, 20]


@pytest.mark.asyncio
async def test_timestamp_error_resyncs_clock_once_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "result": {}, "time": 1_700_000_090_000})
        if len([c for c in calls if c == PATH_QUERY_API]) == 1:
            return httpx.Response(200, json={"retCode": 10002, "retMsg": "time window"})
        return httpx.Response(200, json={"retCode": 0, "result": {"ok": 1}})

    client, _ = _client(handler)
    assert await client.get(PATH_QUERY_API, {}, "query_api") == {"ok": 1}
    assert calls == [PATH_QUERY_API, "/v5/market/time", PATH_QUERY_API]


@pytest.mark.asyncio
async def test_other_retcodes_become_bybit_error_with_the_code():
    def handler(request):
        return httpx.Response(200, json={"retCode": 10005, "retMsg": "Permission denied"})

    client, _ = _client(handler)
    with pytest.raises(BybitError) as exc:
        await client.get(PATH_QUERY_API, {}, "query_api")
    assert (exc.value.stage, exc.value.code) == ("query_api", "10005")


@pytest.mark.asyncio
async def test_card_posts_are_spaced_at_least_1_5_seconds():
    now = {"t": 1_700_000_000.0}

    def handler(request):
        return httpx.Response(200, json={"retCode": 0, "result": {"data": []}})

    client, sleeps = _client(handler, clock=lambda: now["t"])
    await client.post(PATH_CARD_RECORDS, {"page": 1}, "card")
    now["t"] += 0.5
    await client.post(PATH_CARD_RECORDS, {"page": 2}, "card")
    assert sleeps == [pytest.approx(1.0)]


@pytest.mark.asyncio
async def test_errors_never_carry_secrets_or_response_values():
    def handler(request):
        return httpx.Response(200, content=b'{"retCode": "not-json-int" ' + SECRET.encode())

    client, _ = _client(handler)
    with pytest.raises(BybitError) as exc:
        await client.get(PATH_QUERY_API, {}, "query_api")
    chain = [exc.value, exc.value.__cause__, exc.value.__context__]
    text = " ".join(repr(e) + str(e) for e in chain if e is not None)
    assert SECRET not in text and KEY not in text
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_network_error_is_a_bybit_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    client, _ = _client(handler)
    with pytest.raises(BybitError) as exc:
        await client.get(PATH_QUERY_API, {}, "query_api")
    assert exc.value.code == "network"
