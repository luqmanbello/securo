# Bybit Read-Only Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Bybit account syncs into Securo as one USD account: stablecoin balance plus every ledger movement, with Bybit Card purchases grouped into one row each and named from the card API.

**Architecture:** Three backend modules. `bybit_client.py` is the only code that talks to Bybit: it signs requests and refuses any endpoint outside a frozen read-only allowlist. `bybit_ledger.py` is pure functions (no I/O): it parses ledger rows, clusters card legs, enriches them, builds `TransactionData`, and runs the drift check. `bybit.py` is the `BankProvider` that wires the two together. The frontend credentials dialog becomes field-driven, so Bybit can ask for an API key + secret while Access Bank keeps user ID + password.

**Tech Stack:** Python 3.12, httpx, pytest + `httpx.MockTransport`, FastAPI provider registry; React + TypeScript + vitest (Node 22).

**Spec:** `docs/superpowers/specs/2026-10-03-bybit-provider-design.md`

## Global Constraints

- The Funding ledger is the only source of money. Card records and points only add names.
- A row's `external_id` and amount depend on ledger rows only, never on whether the card API answered.
- Every request goes through `BybitClient._request`, which raises unless `(method, path)` is in `ALLOWED_ENDPOINTS`. No write endpoint is ever declared.
- A key with `readOnly != 1` is refused at connect and at every sync. The `Earn` permission is required; `BitCard` is optional.
- `AccountData.currency` and every `TransactionData.currency` are `"USD"`. Coin codes live in `raw_data` only.
- `TransactionData.amount` is positive, quantized to 0.01; direction is `type` (`"credit"`/`"debit"`). Rows that quantize to 0.00 are skipped.
- `raw_data` is built from an explicit allowlist (see Task 3). Never store `uid`, `pan6`, `memberId`, addresses or txIDs.
- `api_key` and `api_secret` are stored only via `app.agents.services.crypto.encrypt`. Never store plaintext.
- Exceptions escaping the provider are only `BybitError`, `SessionExpiredError`, `ProviderRateLimited`, `ProviderUserActionRequired`, raised `from None`.
- Card POSTs are signed over the JSON body (`ts + key + "5000" + body`); query-string signing returns `10004`.
- Card and points calls are spaced ≥ 1.5 s.
- Internal timestamps are integer seconds UTC.
- Test fixtures are fully synthetic. Nothing from any real account.
- Commit messages carry no `Co-Authored-By` or `Claude-Session` trailers.
- Backend tests: `cd backend && uv run pytest <path> -q`. Frontend tests: `cd frontend && npx -y node@22 node_modules/.bin/vitest run <path>` (Node 26 breaks the suite).

## Review Focus

1. **Sub-cent amounts** like `"0.00000412"` interest: they must quantize to 0.00 and be skipped, never booked as $0.00 rows. Pinned in Task 3.
2. **USDC next to USDT** in one card cluster: both count toward the net. Pinned in Task 3.
3. **Clock skew** (`10002`): resync once from `/v5/market/time`, then succeed. Pinned in Task 2.
4. **A ledger window with more than one page** (`nextPageCursor`): every page is read. Pinned in Task 5.
5. **A key edited to read-write after connecting:** the next sync refuses it. Pinned in Task 5.

## File map

| File | Responsibility |
|---|---|
| `backend/app/providers/bybit_client.py` (new) | signing, endpoint allowlist, retCode → exceptions, backoff, card pacing |
| `backend/app/providers/bybit_ledger.py` (new) | pure parsing, classification, clustering, enrichment, build, drift check |
| `backend/app/providers/bybit.py` (new) | `BybitProvider`: claim, refresh, balance, transactions |
| `backend/app/providers/__init__.py` | `KNOWN_PROVIDERS` entry with `credential_fields`; registration |
| `backend/app/core/config.py` | `bybit_enabled`, `bybit_base_url` |
| `backend/tests/test_providers_bybit_client.py` (new) | Task 2 |
| `backend/tests/test_providers_bybit_ledger.py` (new) | Tasks 3–4 |
| `backend/tests/test_providers_bybit.py` (new) | Tasks 1, 5 |
| `frontend/src/components/credentials-connect-dialog.tsx` | field-driven dialog |
| `frontend/src/components/credentials-connect-dialog.test.tsx` (new) | Task 6 |
| `frontend/src/pages/accounts.tsx`, `frontend/src/lib/api.ts`, `Provider` type | pass `credential_fields` |
| `frontend/src/locales/*.json` (15) | Bybit strings |
| `docker-compose.yml`, `docker-compose.prod.yml`, `deploy/values.yaml` | `BYBIT_ENABLED`, `BYBIT_BASE_URL` |

Ruling (plan vs spec): the spec names one file `bybit.py`; this plan splits it in three so the money logic is pure and testable without HTTP. Same behaviour. Ruling: the spec says cashback is labelled when "a points redemption of the same amount is within 2 h". Points records carry points, not USDT, and reading the USDT amount costs one paced detail call per record, so this plan matches on time only: an `Airdrop` row within 2 h after a points record with `side == "2"`.

---

### Task 1: Config and registry

**Files:**
- Modify: `backend/app/core/config.py` (next to the `accessbank_*` settings, ~line 53)
- Modify: `backend/app/providers/__init__.py`
- Test: `backend/tests/test_providers_bybit.py`

**Interfaces:**
- Produces: `settings.bybit_enabled: bool`, `settings.bybit_base_url: str`; a `KNOWN_PROVIDERS` entry `name="bybit"` with `credential_fields: list[dict]`; registration of `app.providers.bybit.BybitProvider` when enabled (the class arrives in Task 5; the import is lazy, inside the `if`).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_providers_bybit.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_providers_bybit.py -q`
Expected: FAIL (`StopIteration` — no bybit entry; `AttributeError` on `bybit_enabled`).

- [ ] **Step 3: Implement**

In `backend/app/core/config.py`, directly after the `accessbank_import_currencies` line:

```python
    # Bybit (read-only API key, per connection). Off by default; credentials
    # are stored per connection, never in the environment.
    bybit_enabled: bool = False
    bybit_base_url: str = "https://api.bybit.com"
```

In `backend/app/providers/__init__.py`, append to `KNOWN_PROVIDERS` after the `accessbank` entry:

```python
    {
        "name": "bybit",
        "display_name": "Bybit",
        "description": "Bybit stablecoin balance and Bybit Card payments via a read-only API key",
        "flow_type": "credentials",
        "requires_institution_select": False,
        "supports_asset_sync": False,
        # The credentials dialog renders these instead of its user ID +
        # password default. `secret` fields are masked and never autofilled.
        "credential_fields": [
            {
                "name": "api_key",
                "label_key": "accounts.credentialsConnect.bybit.apiKeyLabel",
                "placeholder_key": "accounts.credentialsConnect.bybit.apiKeyPlaceholder",
                "secret": False,
            },
            {
                "name": "api_secret",
                "label_key": "accounts.credentialsConnect.bybit.apiSecretLabel",
                "placeholder_key": "accounts.credentialsConnect.bybit.apiSecretPlaceholder",
                "secret": True,
            },
        ],
    },
```

And in `_auto_register_providers()`, after the accessbank block:

```python
    if settings.bybit_enabled:
        from app.providers.bybit import BybitProvider
        register_provider("bybit", BybitProvider)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_providers_bybit.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/app/providers/__init__.py backend/tests/test_providers_bybit.py
git commit -m "feat(bybit): settings and provider registry entry"
```

---

### Task 2: Signed, allowlisted client

**Files:**
- Create: `backend/app/providers/bybit_client.py`
- Test: `backend/tests/test_providers_bybit_client.py`

**Interfaces:**
- Produces:
  - `ALLOWED_ENDPOINTS: frozenset[tuple[str, str]]` and path constants `PATH_TIME`, `PATH_QUERY_API`, `PATH_FUND_BALANCE`, `PATH_UTA_BALANCE`, `PATH_EARN_POSITION`, `PATH_FUNDING_HISTORY`, `PATH_INTER_TRANSFER`, `PATH_CARD_RECORDS`, `PATH_POINTS_RECORDS`
  - `class BybitError(RuntimeError)` with `.stage: str`, `.code: str`; `str(e) == f"Bybit {stage} failed ({code})"`
  - `def sign(secret: str, timestamp_ms: str, api_key: str, recv_window: str, payload: str) -> str`
  - `class BybitClient` with `__init__(self, http: httpx.AsyncClient, api_key: str, api_secret: str, *, sleep=asyncio.sleep, clock=time.time)`, `async get(self, path: str, params: dict, stage: str) -> dict` and `async post(self, path: str, body: dict, stage: str) -> dict`, both returning the response's `result` object (`{}` if absent)
  - Raises: `BybitError(stage, "<retCode>")`, `BybitError(stage, "network")`, `BybitError(stage, "http_<status>")`, `BybitError(stage, "schema")`, `BybitError(stage, "forbidden_endpoint")` (before any I/O), `ProviderRateLimited`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_providers_bybit_client.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_providers_bybit_client.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.bybit_client'`.

- [ ] **Step 3: Implement**

Create `backend/app/providers/bybit_client.py`:

```python
"""The only code in Securo that talks to Bybit.

Read-only by construction: every request goes through `_request`, which
refuses any (method, path) outside ALLOWED_ENDPOINTS before touching the
network. Adding an endpoint is a deliberate edit to that set, and the tests
fail on any write prefix or any undeclared `v5/` literal in this module.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any, Awaitable, Callable

import httpx

from app.providers.base import ProviderRateLimited

PATH_TIME = "/v5/market/time"
PATH_QUERY_API = "/v5/user/query-api"
PATH_FUND_BALANCE = "/v5/asset/transfer/query-account-coins-balance"
PATH_UTA_BALANCE = "/v5/account/wallet-balance"
PATH_EARN_POSITION = "/v5/earn/position"
PATH_FUNDING_HISTORY = "/v5/asset/fundinghistory"
PATH_INTER_TRANSFER = "/v5/asset/transfer/query-inter-transfer-list"
PATH_CARD_RECORDS = "/v5/card/transaction/query-asset-records"
PATH_POINTS_RECORDS = "/v5/card/reward/points/records"

ALLOWED_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("GET", PATH_TIME),
    ("GET", PATH_QUERY_API),
    ("GET", PATH_FUND_BALANCE),
    ("GET", PATH_UTA_BALANCE),
    ("GET", PATH_EARN_POSITION),
    ("GET", PATH_FUNDING_HISTORY),
    ("GET", PATH_INTER_TRANSFER),
    # POST, but reads: Bybit's card API only accepts POST, signed over the
    # JSON body (query-string signing returns 10004).
    ("POST", PATH_CARD_RECORDS),
    ("POST", PATH_POINTS_RECORDS),
})

RECV_WINDOW = "5000"
_RATE_LIMIT_CODES = {10006}
_BACKOFF_SECONDS = (1.5, 10, 20)
_CARD_SPACING_SECONDS = 1.5
_MAX_BODY_BYTES = 4 * 1024 * 1024
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class BybitError(RuntimeError):
    """Carries a stage and a code only. Never a key, secret, signature or
    response body, so it is safe to log and to store as a task result."""

    def __init__(self, stage: str, code: str) -> None:
        super().__init__(f"Bybit {stage} failed ({code})")
        self.stage = stage
        self.code = code


def sign(secret: str, timestamp_ms: str, api_key: str, recv_window: str, payload: str) -> str:
    message = (timestamp_ms + api_key + recv_window + payload).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class BybitClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        api_secret: str,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._key = api_key
        self._secret = api_secret
        self._sleep = sleep
        self._clock = clock
        self._offset_ms = 0
        self._last_card_call: float | None = None

    async def get(self, path: str, params: dict, stage: str) -> dict:
        return await self._request("GET", path, params, stage)

    async def post(self, path: str, body: dict, stage: str) -> dict:
        return await self._request("POST", path, body, stage)

    async def _request(self, method: str, path: str, data: dict, stage: str) -> dict:
        if (method, path) not in ALLOWED_ENDPOINTS:
            raise BybitError(stage, "forbidden_endpoint")
        resynced = False
        for attempt in range(len(_BACKOFF_SECONDS) + 1):
            if path.startswith("/v5/card/"):
                await self._pace_card_call()
            status, doc = await self._send(method, path, data, stage)
            code = doc.get("retCode") if isinstance(doc, dict) else None
            if status == 403 or code in _RATE_LIMIT_CODES:
                if attempt < len(_BACKOFF_SECONDS):
                    await self._sleep(_BACKOFF_SECONDS[attempt])
                    continue
                raise ProviderRateLimited(f"Bybit {stage} rate-limited") from None
            if status != 200:
                raise BybitError(stage, f"http_{status}")
            if code == 10002 and not resynced:
                await self._resync_clock(stage)
                resynced = True
                continue
            if not isinstance(code, int):
                raise BybitError(stage, "schema")
            if code != 0:
                raise BybitError(stage, str(code))
            result = doc.get("result")
            return result if isinstance(result, dict) else {}
        raise BybitError(stage, "retry_exhausted")  # pragma: no cover - loop always returns or raises

    async def _pace_card_call(self) -> None:
        now = self._clock()
        if self._last_card_call is not None:
            wait = _CARD_SPACING_SECONDS - (now - self._last_card_call)
            if wait > 0:
                await self._sleep(wait)
                now += wait
        self._last_card_call = now

    async def _resync_clock(self, stage: str) -> None:
        status, doc = await self._send("GET", PATH_TIME, {}, stage, signed=False)
        server_ms = doc.get("time") if isinstance(doc, dict) else None
        if status != 200 or not isinstance(server_ms, int):
            raise BybitError(stage, "clock")
        self._offset_ms = server_ms - int(self._clock() * 1000)

    async def _send(self, method: str, path: str, data: dict, stage: str, *, signed: bool = True) -> tuple[int, Any]:
        timestamp = str(int(self._clock() * 1000) + self._offset_ms)
        headers = {"Accept": "application/json"}
        if method == "GET":
            payload = urllib.parse.urlencode(data)
            url = f"{path}?{payload}" if payload else path
            content = None
        else:
            payload = json.dumps(data, separators=(",", ":"))
            url = path
            content = payload.encode()
            headers["Content-Type"] = "application/json"
        if signed:
            headers.update({
                "X-BAPI-API-KEY": self._key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": RECV_WINDOW,
                "X-BAPI-SIGN": sign(self._secret, timestamp, self._key, RECV_WINDOW, payload),
            })
        try:
            response = await self._http.request(method, url, headers=headers, content=content)
        except httpx.HTTPError:
            raise BybitError(stage, "network") from None
        body = response.content
        if len(body) > _MAX_BODY_BYTES:
            raise BybitError(stage, "body_too_large")
        if response.status_code != 200:
            return response.status_code, None
        try:
            return 200, json.loads(body)
        except ValueError:
            raise BybitError(stage, "schema") from None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_providers_bybit_client.py -q`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/app/providers/bybit_client.py backend/tests/test_providers_bybit_client.py
git commit -m "feat(bybit): signed, allowlisted read-only client"
```

---

### Task 3: Ledger parsing, classification and single rows

**Files:**
- Create: `backend/app/providers/bybit_ledger.py`
- Test: `backend/tests/test_providers_bybit_ledger.py`

**Interfaces:**
- Consumes: `BybitError` from Task 2; `TransactionData` from `app.providers.base`.
- Produces (all pure):
  - `STABLE = frozenset({"USDT", "USDC", "USD"})`
  - `@dataclass(frozen=True) class LedgerRow: cursor: str; busi: str; desc: str; currency: str; direction: str; amount: Decimal; after: Decimal; ts: int` with property `signed -> Decimal` (`+amount` if `direction == "I"` else `-amount`)
  - `def parse_ledger_row(raw: dict) -> LedgerRow | None` — `None` for non-stable coins; `BybitError("ledger", "schema")` for malformed rows
  - `def classify(row: LedgerRow) -> tuple[str, str | None]` → `("card", None)`, `("skip", None)`, `("book", label)` or `("unknown", label)`
  - `def quantize(amount: Decimal) -> Decimal` (0.01, ROUND_HALF_UP)
  - `def ledger_raw(row: LedgerRow) -> dict` (allowlisted)
  - `def single_transaction(row: LedgerRow, label: str) -> TransactionData | None` (`None` when it quantizes to 0)

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_providers_bybit_ledger.py`:

```python
"""Pure ledger logic. Synthetic rows only."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.providers.bybit_client import BybitError
from app.providers.bybit_ledger import (
    LedgerRow,
    classify,
    ledger_raw,
    parse_ledger_row,
    quantize,
    single_transaction,
)

T0 = 1_760_000_000  # synthetic epoch seconds


def raw(cursor="c1", busi="Deposit", desc="Deposit", currency="USDT", io="I", amt="10", after="10", ts=T0, **extra):
    row = {
        "memberId": "999", "currency": currency, "ioDirection": io, "txnAmt": amt, "afterAmt": after,
        "createTime": str(ts), "showBusiType": "x", "showBusiTypeEn": busi, "description": "x",
        "descriptionEn": desc, "currcCursor": cursor,
    }
    row.update(extra)
    return row


def test_parse_reads_seconds_and_decimals():
    row = parse_ledger_row(raw(amt="12.345678", after="100.5"))
    assert row == LedgerRow(cursor="c1", busi="Deposit", desc="Deposit", currency="USDT", direction="I",
                            amount=Decimal("12.345678"), after=Decimal("100.5"), ts=T0)
    assert row.signed == Decimal("12.345678")
    assert parse_ledger_row(raw(io="O")).signed == Decimal("-10")


def test_non_stable_coins_are_ignored():
    assert parse_ledger_row(raw(currency="BTC")) is None


@pytest.mark.parametrize("bad", [{"txnAmt": "abc"}, {"ioDirection": "X"}, {"createTime": None}, {"currcCursor": ""}, {"txnAmt": "-1"}])
def test_malformed_rows_raise_schema_without_values(bad):
    row = raw()
    row.update(bad)
    with pytest.raises(BybitError) as exc:
        parse_ledger_row(row)
    assert (exc.value.stage, exc.value.code) == ("ledger", "schema")
    assert exc.value.__cause__ is None


@pytest.mark.parametrize("busi,desc,expected", [
    ("Bybit Card", "Purchase", ("card", None)),
    ("Bybit Card", "Sale", ("card", None)),
    ("Earn", "Easy Earn | Flexible (Auto-Earn)", ("skip", None)),
    ("Earn", "Easy Earn | Flexible Redemption", ("skip", None)),
    ("Earn", "Easy Earn card redemption", ("skip", None)),
    ("Earn", "Easy Earn | Flexible Interest Distribution", ("book", "Bybit Earn interest")),
    ("Deposit", "Deposit", ("book", "Bybit deposit")),
    ("Deposit", "Deposit (Internal Transfer)", ("book", "Bybit deposit")),
    ("Withdraw", "Withdrawal", ("book", "Bybit withdrawal")),
    ("Withdraw", "Withdraw (Internal Transfer)", ("book", "Bybit withdrawal")),
    ("Fiat", "P2P Sale", ("book", "Bybit P2P sale")),
    ("Fiat", "Canceled P2P Sale", ("book", "Bybit P2P sale cancelled")),
    ("Airdrop", "Airdrop Bonus", ("book", "Bybit bonus")),
    ("Bybit Pay", "Bybit Pay transfer (payment)", ("book", "Bybit Pay")),
    ("Something New", "Mystery", ("unknown", "Mystery")),
])
def test_classify(busi, desc, expected):
    assert classify(parse_ledger_row(raw(busi=busi, desc=desc))) == expected


def test_quantize_and_sub_cent_rows_are_skipped():
    assert quantize(Decimal("1.005")) == Decimal("1.01")
    row = parse_ledger_row(raw(busi="Earn", desc="Easy Earn | Flexible Interest Distribution", amt="0.00000412"))
    assert single_transaction(row, "Bybit Earn interest") is None


def test_single_transaction_shape():
    row = parse_ledger_row(raw(cursor="abc", io="O", amt="25.5", busi="Withdraw", desc="Withdrawal"))
    tx = single_transaction(row, "Bybit withdrawal")
    assert tx.external_id == "bybit:abc"
    assert tx.amount == Decimal("25.50")
    assert tx.type == "debit"
    assert tx.currency == "USD"
    assert tx.date == date(2025, 10, 9)
    assert tx.description == "Bybit withdrawal"
    assert tx.payee is None
    assert tx.status == "posted"


def test_raw_data_is_an_allowlist():
    row = parse_ledger_row(raw(toAddress="T-synthetic-address", txID="0xsynthetic", memberId="123"))
    stored = ledger_raw(row)
    assert set(stored) == {"currcCursor", "showBusiTypeEn", "descriptionEn", "currency", "ioDirection", "txnAmt", "createTime"}
    assert stored["txnAmt"] == "10"
```

Note the synthetic epoch: `1_760_000_000` is 2025-10-09 UTC, which the date assertion checks.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_providers_bybit_ledger.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.bybit_ledger'`.

- [ ] **Step 3: Implement**

Create `backend/app/providers/bybit_ledger.py`:

```python
"""Pure Bybit ledger logic: no I/O, no clock, no settings.

The Funding ledger (`/v5/asset/fundinghistory`) is the only source of money.
Everything here turns its rows into Securo transactions. Card records and
points only ever add names (see enrichment below).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

from app.providers.base import TransactionData
from app.providers.bybit_client import BybitError

STABLE = frozenset({"USDT", "USDC", "USD"})
CENT = Decimal("0.01")


@dataclass(frozen=True)
class LedgerRow:
    cursor: str
    busi: str
    desc: str
    currency: str
    direction: str  # "I" in, "O" out
    amount: Decimal  # always >= 0
    after: Decimal
    ts: int  # seconds UTC

    @property
    def signed(self) -> Decimal:
        return self.amount if self.direction == "I" else -self.amount


def _decimal(value: object, stage: str) -> Decimal:
    if not isinstance(value, (str, int)):
        raise BybitError(stage, "schema")
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        raise BybitError(stage, "schema") from None
    if not out.is_finite():
        raise BybitError(stage, "schema")
    return out


def _int(value: object, stage: str) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise BybitError(stage, "schema") from None


def parse_ledger_row(raw: dict) -> Optional[LedgerRow]:
    if not isinstance(raw, dict):
        raise BybitError("ledger", "schema")
    currency = raw.get("currency")
    if not isinstance(currency, str) or not currency:
        raise BybitError("ledger", "schema")
    if currency not in STABLE:
        return None
    cursor = raw.get("currcCursor")
    direction = raw.get("ioDirection")
    if not isinstance(cursor, str) or not cursor or direction not in ("I", "O"):
        raise BybitError("ledger", "schema")
    amount = _decimal(raw.get("txnAmt"), "ledger")
    if amount < 0:
        raise BybitError("ledger", "schema")
    return LedgerRow(
        cursor=cursor,
        busi=str(raw.get("showBusiTypeEn") or ""),
        desc=str(raw.get("descriptionEn") or ""),
        currency=currency,
        direction=direction,
        amount=amount,
        after=_decimal(raw.get("afterAmt"), "ledger"),
        ts=_int(raw.get("createTime"), "ledger"),
    )


_SKIPPED_EARN = ("Flexible (Auto-Earn)", "Flexible Redemption", "card redemption")
_BOOK_BY_BUSI = {
    "Deposit": "Bybit deposit",
    "Withdraw": "Bybit withdrawal",
    "Airdrop": "Bybit bonus",
    "Bybit Pay": "Bybit Pay",
}


def classify(row: LedgerRow) -> tuple[str, Optional[str]]:
    if row.busi == "Bybit Card":
        return "card", None
    if row.busi == "Earn":
        if "Interest" in row.desc:
            return "book", "Bybit Earn interest"
        if any(marker in row.desc for marker in _SKIPPED_EARN):
            return "skip", None
        return "unknown", row.desc or row.busi
    if row.busi == "Fiat":
        if row.desc == "Canceled P2P Sale":
            return "book", "Bybit P2P sale cancelled"
        if row.desc == "P2P Sale":
            return "book", "Bybit P2P sale"
        return "unknown", row.desc or row.busi
    if row.busi in _BOOK_BY_BUSI:
        return "book", _BOOK_BY_BUSI[row.busi]
    return "unknown", row.desc or row.busi or "Bybit"


def quantize(amount: Decimal) -> Decimal:
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def to_date(ts: int):
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def ledger_raw(row: LedgerRow) -> dict:
    return {
        "currcCursor": row.cursor,
        "showBusiTypeEn": row.busi,
        "descriptionEn": row.desc,
        "currency": row.currency,
        "ioDirection": row.direction,
        "txnAmt": str(row.amount),
        "createTime": str(row.ts),
    }


def single_transaction(row: LedgerRow, label: str) -> Optional[TransactionData]:
    amount = quantize(row.amount)
    if amount == 0:
        return None
    return TransactionData(
        external_id=f"bybit:{row.cursor}",
        description=label,
        amount=amount,
        date=to_date(row.ts),
        type="credit" if row.direction == "I" else "debit",
        currency="USD",
        status="posted",
        payee=None,
        raw_data=ledger_raw(row),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_providers_bybit_ledger.py -q`
Expected: all pass (32 with parametrisation).

- [ ] **Step 5: Commit**

```bash
git add backend/app/providers/bybit_ledger.py backend/tests/test_providers_bybit_ledger.py
git commit -m "feat(bybit): ledger row parsing, classification and single rows"
```

---

### Task 4: Card clusters, enrichment, build and drift check

**Files:**
- Modify: `backend/app/providers/bybit_ledger.py`
- Test: `backend/tests/test_providers_bybit_ledger.py`

**Interfaces:**
- Consumes: Task 3 symbols.
- Produces (all pure):
  - `@dataclass(frozen=True) class CardRecord: txn_id: str; ts: int; merchant: str; basic_amount: Decimal; paid_amount: Decimal; paid_currency: str; mcc: str; mcc_desc: str; side: str`
  - `def parse_card_record(raw: dict, *, refund: bool = False) -> CardRecord | None` — purchases: `None` unless `cryptoSold is True` and `basicAmount > 0`; refunds (`refund=True`): `None` unless `basicAmount > 0`
  - `def parse_point_redemption_ts(raw: dict) -> int | None` — seconds, for records with `side == "2"`
  - `@dataclass class BuildResult: transactions: list[TransactionData]; buckets: dict[str, str]; warnings: list[str]; unknown_types: set[str]`
  - `def build_transactions(rows: list[LedgerRow], *, since_ts: int, now_ts: int, purchases: list[CardRecord], refunds: list[CardRecord], redemption_ts: list[int], card_transient_failure: bool, internal_cursors: set[str]) -> BuildResult`
  - Constants: `CLUSTER_GAP_S = 5`, `ENRICH_WINDOW_S = 10`, `SETTLE_S = 600`, `HOLD_BACK_S = 72 * 3600`, `CASHBACK_WINDOW_S = 7200`, `REFUND_WINDOW_S = 30 * 86400`, `RATIO_MIN = Decimal("0.99")`, `RATIO_MAX = Decimal("1.05")`
  - Bucket names: `"emitted"`, `"skipped"`, `"rounded"`, `"held"`, `"before_since"`, `"usd_pair"`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_providers_bybit_ledger.py`, moving the new import into the file's top import block (ruff `E402`):

```python
from app.providers.bybit_ledger import (
    CardRecord,
    build_transactions,
    parse_card_record,
    parse_point_redemption_ts,
)

NOW = T0 + 30 * 86400
SINCE = T0 - 86400


def L(cursor, busi, desc, currency, io, amt, ts, after="0"):
    return parse_ledger_row(raw(cursor=cursor, busi=busi, desc=desc, currency=currency, io=io, amt=amt, ts=ts, after=after))


def card_raw(txn="t1", ts_ms=T0 * 1000, merch="SYNTH SHOP", basic="50.00", paid="43.00", paid_cur="EUR", sold=True, side="1"):
    return {"txnId": txn, "txnCreate": str(ts_ms), "merchName": merch, "basicAmount": basic, "paidAmount": paid,
            "paidCurrency": paid_cur, "mccCode": "5999", "merchCategoryDesc": "Misc retail", "side": side,
            "cryptoSold": sold, "uid": "424242", "pan6": "411111", "pan4": "0000"}


def auto_earn_purchase(ts, base="50.00", prefix="p"):
    # Sale out of Earn, the Earn redemption that feeds it, the conversion fee
    # leg, and the USD buy/spend pair: the shape Bybit really emits.
    b = Decimal(base)
    return [
        L(f"{prefix}1", "Bybit Card", "Sale", "USDT", "O", str(b * Decimal("1.006")), ts),
        L(f"{prefix}2", "Earn", "Easy Earn card redemption", "USDT", "I", str(b * Decimal("1.006")), ts),
        L(f"{prefix}3", "Bybit Card", "Purchase", "USDT", "O", str(b * Decimal("0.008")), ts),
        L(f"{prefix}4", "Bybit Card", "Purchase", "USD", "O", base, ts + 1),
        L(f"{prefix}5", "Bybit Card", "Coin Purchase", "USD", "I", base, ts + 1),
    ]


def build(rows, **kw):
    args = dict(since_ts=SINCE, now_ts=NOW, purchases=[], refunds=[], redemption_ts=[], card_transient_failure=False, internal_cursors=set())
    args.update(kw)
    return build_transactions(rows, **args)


def test_parse_card_record_ignores_verification_auths_and_strips_personal_fields():
    assert parse_card_record(card_raw(sold=False)) is None
    assert parse_card_record(card_raw(basic="0")) is None
    rec = parse_card_record(card_raw())
    assert rec.ts == T0 and rec.merchant == "SYNTH SHOP" and rec.paid_currency == "EUR"
    assert not hasattr(rec, "uid") and not hasattr(rec, "pan6")


def test_auto_earn_purchase_is_one_enriched_debit():
    rows = auto_earn_purchase(T0)
    res = build(rows, purchases=[parse_card_record(card_raw())])
    [tx] = res.transactions
    assert tx.external_id == "bybit-card:p1"
    assert tx.type == "debit"
    assert tx.amount == Decimal("50.70")  # 50.30 Sale + 0.40 fee leg
    assert tx.description == "SYNTH SHOP (EUR 43.00)"
    assert tx.payee == "SYNTH SHOP"
    assert tx.currency == "USD"
    assert tx.raw_data["card"] == {"side": "1", "merchName": "SYNTH SHOP", "mccCode": "5999", "merchCategoryDesc": "Misc retail",
                                   "paidAmount": "43.00", "paidCurrency": "EUR", "basicAmount": "50.00"}
    assert "uid" not in str(tx.raw_data) and "411111" not in str(tx.raw_data)
    assert res.buckets["p2"] == "skipped" and res.buckets["p4"] == "usd_pair" and res.buckets["p1"] == "emitted"


def test_usd_purchase_needs_no_suffix():
    res = build(auto_earn_purchase(T0), purchases=[parse_card_record(card_raw(paid="50.00", paid_cur="USD"))])
    assert res.transactions[0].description == "SYNTH SHOP"


def test_usdc_and_usdt_legs_both_count():
    rows = [
        L("u1", "Bybit Card", "Purchase", "USDT", "O", "30", T0),
        L("u2", "Bybit Card", "Purchase", "USDC", "O", "20.7", T0),
    ]
    [tx] = build(rows).transactions
    assert tx.amount == Decimal("50.70")


def test_two_purchases_30_seconds_apart_are_two_debits():
    rows = auto_earn_purchase(T0, "50.00", "a") + auto_earn_purchase(T0 + 30, "20.00", "b")
    recs = [parse_card_record(card_raw(txn="ta", ts_ms=T0 * 1000)), parse_card_record(card_raw(txn="tb", ts_ms=(T0 + 30) * 1000, basic="20.00", merch="OTHER"))]
    txs = build(rows, purchases=recs).transactions
    assert [(t.external_id, t.amount, t.payee) for t in txs] == [("bybit-card:a1", Decimal("50.70"), "SYNTH SHOP"), ("bybit-card:b1", Decimal("20.28"), "OTHER")]


def test_identity_does_not_depend_on_the_card_api():
    rows = auto_earn_purchase(T0)
    with_card = build(rows, purchases=[parse_card_record(card_raw())]).transactions
    without = build(rows).transactions
    assert [(t.external_id, t.amount) for t in with_card] == [(t.external_id, t.amount) for t in without]
    assert without[0].description == "Bybit Card" and without[0].payee is None


def test_record_outside_ratio_band_is_not_attached():
    res = build(auto_earn_purchase(T0), purchases=[parse_card_record(card_raw(basic="10.00"))])
    assert res.transactions[0].description == "Bybit Card"


def test_usd_rows_that_do_not_net_are_booked():
    rows = [L("x1", "Bybit Card", "Purchase", "USDT", "O", "10", T0), L("x2", "Bybit Card", "Coin Purchase", "USD", "I", "10", T0)]
    ids = {t.external_id for t in build(rows).transactions}
    assert ids == {"bybit-card:x1", "bybit:x2"}


def test_settle_time_and_hold_back():
    fresh = auto_earn_purchase(NOW - 60)
    assert build(fresh).transactions == []
    young = auto_earn_purchase(NOW - 3600)
    assert build(young, card_transient_failure=True).transactions == []
    assert len(build(young, card_transient_failure=False).transactions) == 1
    old = auto_earn_purchase(NOW - 4 * 86400)
    assert len(build(old, card_transient_failure=True).transactions) == 1


def test_cluster_starting_before_since_is_not_emitted():
    rows = [L("e1", "Bybit Card", "Sale", "USDT", "O", "5", SINCE - 1), L("e2", "Bybit Card", "Purchase", "USDT", "O", "1", SINCE)]
    res = build(rows)
    assert res.transactions == []
    assert res.buckets == {"e1": "before_since", "e2": "before_since"}


def test_refund_cluster_is_a_credit_named_from_refund_records():
    rows = [L("r1", "Bybit Card", "Refund", "USDT", "I", "12.34", T0)]
    rec = parse_card_record(card_raw(txn="rf", ts_ms=(T0 - 86400) * 1000, basic="12.34", sold=False, side="5"), refund=True)
    [tx] = build(rows, refunds=[rec]).transactions
    assert tx.type == "credit" and tx.amount == Decimal("12.34") and tx.payee == "SYNTH SHOP"


def test_airdrop_after_points_redemption_is_cashback():
    rows = [L("a1", "Airdrop", "Airdrop Bonus", "USDT", "I", "1.00", T0)]
    assert build(rows, redemption_ts=[T0 - 3000]).transactions[0].description == "Bybit Card cashback"
    assert build(rows, redemption_ts=[T0 - 9000]).transactions[0].description == "Bybit bonus"
    assert parse_point_redemption_ts({"side": "2", "createTime": str(T0 * 1000)}) == T0
    assert parse_point_redemption_ts({"side": "1", "createTime": str(T0 * 1000)}) is None


def test_internal_transfers_are_skipped_and_unknown_types_reported():
    rows = [L("i1", "Transfer", "To Unified", "USDT", "O", "5", T0), L("i2", "Brand New", "Thing", "USDT", "I", "2", T0)]
    res = build(rows, internal_cursors={"i1"})
    assert [t.external_id for t in res.transactions] == ["bybit:i2"]
    assert res.unknown_types == {"Brand New/Thing"}
    assert res.buckets["i1"] == "skipped"


def test_every_row_lands_in_exactly_one_bucket_and_drift_is_clean():
    rows = auto_earn_purchase(T0, prefix="d") + [
        L("d9", "Earn", "Easy Earn | Flexible Interest Distribution", "USDT", "I", "0.000001", T0 + 10),
        L("d10", "Withdraw", "Withdrawal", "USDT", "O", "3", T0 + 100),
    ]
    res = build(rows, purchases=[parse_card_record(card_raw(ts_ms=T0 * 1000))])
    assert set(res.buckets) == {r.cursor for r in rows}
    assert res.buckets["d9"] == "rounded"
    assert res.warnings == []


def test_drift_check_flags_a_wrong_emitted_total():
    from app.providers import bybit_ledger

    rows = [L("w1", "Withdraw", "Withdrawal", "USDT", "O", "3", T0)]
    res = build(rows)
    assert bybit_ledger.drift_warnings(rows, res.buckets, emitted_total=Decimal("-2.00")) != []
    assert bybit_ledger.drift_warnings(rows, res.buckets, emitted_total=Decimal("-3.00")) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_providers_bybit_ledger.py -q`
Expected: FAIL with `ImportError: cannot import name 'CardRecord'`.

- [ ] **Step 3: Implement**

Append to `backend/app/providers/bybit_ledger.py`:

```python
CLUSTER_GAP_S = 5
ENRICH_WINDOW_S = 10
SETTLE_S = 600
HOLD_BACK_S = 72 * 3600
CASHBACK_WINDOW_S = 7200
REFUND_WINDOW_S = 30 * 86400
RATIO_MIN = Decimal("0.99")
RATIO_MAX = Decimal("1.05")


@dataclass(frozen=True)
class CardRecord:
    txn_id: str
    ts: int
    merchant: str
    basic_amount: Decimal
    paid_amount: Decimal
    paid_currency: str
    mcc: str
    mcc_desc: str
    side: str


def parse_card_record(raw: dict, *, refund: bool = False) -> Optional[CardRecord]:
    """Reads only the fields Securo keeps. uid, pan6, pan4 and the rest are
    never copied. Purchases need `cryptoSold`; zero-amount auths are card
    verifications and are dropped."""
    if not isinstance(raw, dict):
        raise BybitError("card", "schema")
    if not refund and raw.get("cryptoSold") is not True:
        return None
    basic = _decimal(raw.get("basicAmount") or "0", "card")
    if basic <= 0:
        return None
    return CardRecord(
        txn_id=str(raw.get("txnId") or ""),
        ts=_int(raw.get("txnCreate"), "card") // 1000,
        merchant=str(raw.get("merchName") or "").strip(),
        basic_amount=basic,
        paid_amount=_decimal(raw.get("paidAmount") or "0", "card"),
        paid_currency=str(raw.get("paidCurrency") or ""),
        mcc=str(raw.get("mccCode") or ""),
        mcc_desc=str(raw.get("merchCategoryDesc") or ""),
        side=str(raw.get("side") or ""),
    )


def parse_point_redemption_ts(raw: dict) -> Optional[int]:
    if not isinstance(raw, dict) or str(raw.get("side")) != "2":
        return None
    return _int(raw.get("createTime"), "points") // 1000


def card_raw(record: CardRecord) -> dict:
    return {
        "side": record.side,
        "merchName": record.merchant,
        "mccCode": record.mcc,
        "merchCategoryDesc": record.mcc_desc,
        "paidAmount": str(record.paid_amount),
        "paidCurrency": record.paid_currency,
        "basicAmount": str(record.basic_amount),
    }


@dataclass
class BuildResult:
    transactions: list[TransactionData]
    buckets: dict[str, str]
    warnings: list[str]
    unknown_types: set[str]


def _clusters(card_rows: list[LedgerRow]) -> list[list[LedgerRow]]:
    out: list[list[LedgerRow]] = []
    for row in sorted(card_rows, key=lambda r: (r.ts, r.cursor)):
        if out and row.ts - out[-1][-1].ts <= CLUSTER_GAP_S:
            out[-1].append(row)
        else:
            out.append([row])
    return out


def _describe(record: Optional[CardRecord], fallback: str) -> tuple[str, Optional[str]]:
    if record is None or not record.merchant:
        return fallback, None
    if record.paid_currency and record.paid_currency != "USD" and record.paid_amount > 0:
        return f"{record.merchant} ({record.paid_currency} {quantize(record.paid_amount)})", record.merchant
    return record.merchant, record.merchant


def _nearest_purchase(cluster_ts: int, net: Decimal, purchases: list[CardRecord], used: set[str]) -> Optional[CardRecord]:
    best: Optional[CardRecord] = None
    for rec in purchases:
        if rec.txn_id in used or abs(rec.ts - cluster_ts) > ENRICH_WINDOW_S:
            continue
        ratio = abs(net) / rec.basic_amount
        if not (RATIO_MIN <= ratio <= RATIO_MAX):
            continue
        if best is None or abs(rec.ts - cluster_ts) < abs(best.ts - cluster_ts):
            best = rec
    return best


def _matching_refund(cluster_ts: int, net: Decimal, refunds: list[CardRecord], used: set[str]) -> Optional[CardRecord]:
    for rec in refunds:
        if rec.txn_id in used:
            continue
        if 0 <= cluster_ts - rec.ts <= REFUND_WINDOW_S and quantize(rec.basic_amount) == quantize(net):
            return rec
    return None


def drift_warnings(rows: list[LedgerRow], buckets: dict[str, str], *, emitted_total: Decimal) -> list[str]:
    """Two checks the opening-balance plug would otherwise hide:
    1. every fetched row is accounted for exactly once;
    2. what was emitted equals the ledger money assigned to `emitted`
       (rounded per transaction, so compared at cent precision).
    """
    warnings: list[str] = []
    missing = [r.cursor for r in rows if r.cursor not in buckets]
    if missing:
        warnings.append(f"bybit drift: {len(missing)} ledger rows unaccounted")
    ledger_emitted = sum((r.signed for r in rows if buckets.get(r.cursor) == "emitted"), Decimal("0"))
    gap = quantize(ledger_emitted) - quantize(emitted_total)
    if abs(gap) > CENT * max(1, sum(1 for b in buckets.values() if b == "emitted")) / 2:
        warnings.append(f"bybit drift: emitted total differs from ledger by {gap}")
    usd_pair_total = sum((r.signed for r in rows if buckets.get(r.cursor) == "usd_pair"), Decimal("0"))
    if usd_pair_total != 0:
        warnings.append(f"bybit drift: skipped USD card pairs do not net to zero ({usd_pair_total})")
    return warnings


def build_transactions(
    rows: list[LedgerRow],
    *,
    since_ts: int,
    now_ts: int,
    purchases: list[CardRecord],
    refunds: list[CardRecord],
    redemption_ts: list[int],
    card_transient_failure: bool,
    internal_cursors: set[str],
) -> BuildResult:
    txs: list[TransactionData] = []
    buckets: dict[str, str] = {}
    unknown: set[str] = set()
    emitted_total = Decimal("0")

    def emit(tx: Optional[TransactionData], members: list[LedgerRow]) -> None:
        nonlocal emitted_total
        if tx is None:
            for m in members:
                buckets.setdefault(m.cursor, "rounded")
            return
        txs.append(tx)
        emitted_total += tx.amount if tx.type == "credit" else -tx.amount
        for m in members:
            buckets[m.cursor] = "emitted"

    card_rows: list[LedgerRow] = []
    for row in sorted(rows, key=lambda r: (r.ts, r.cursor)):
        kind, label = classify(row)
        if kind == "card":
            card_rows.append(row)
            continue
        if row.ts < since_ts:
            buckets[row.cursor] = "before_since"
            continue
        if kind == "skip" or row.cursor in internal_cursors:
            buckets[row.cursor] = "skipped"
            continue
        if kind == "unknown":
            unknown.add(f"{row.busi}/{row.desc}")
        if row.busi == "Airdrop" and any(0 <= row.ts - t <= CASHBACK_WINDOW_S for t in redemption_ts):
            label = "Bybit Card cashback"
        emit(single_transaction(row, label or "Bybit"), [row])

    used_purchases: set[str] = set()
    used_refunds: set[str] = set()
    for cluster in _clusters(card_rows):
        first, newest = cluster[0].ts, cluster[-1].ts
        if first < since_ts:
            for r in cluster:
                buckets[r.cursor] = "before_since"
            continue
        age = now_ts - newest
        if age < SETTLE_S or (card_transient_failure and age < HOLD_BACK_S):
            for r in cluster:
                buckets[r.cursor] = "held"
            continue
        coin_legs = [r for r in cluster if r.currency in ("USDT", "USDC")]
        usd_rows = [r for r in cluster if r.currency == "USD"]
        if sum((r.signed for r in usd_rows), Decimal("0")) == 0:
            for r in usd_rows:
                buckets[r.cursor] = "usd_pair"
        else:
            for r in usd_rows:
                emit(single_transaction(r, "Bybit Card"), [r])
        if not coin_legs:
            continue
        net = sum((r.signed for r in coin_legs), Decimal("0"))
        amount = quantize(abs(net))
        if amount == 0:
            for r in coin_legs:
                buckets[r.cursor] = "rounded"
            continue
        if net < 0:
            record = _nearest_purchase(first, net, purchases, used_purchases)
            if record:
                used_purchases.add(record.txn_id)
            description, payee = _describe(record, "Bybit Card")
        else:
            record = _matching_refund(first, net, refunds, used_refunds)
            if record:
                used_refunds.add(record.txn_id)
            description, payee = _describe(record, "Bybit Card refund")
        raw: dict = {"legs": [ledger_raw(r) for r in coin_legs]}
        if record:
            raw["card"] = card_raw(record)
        emit(
            TransactionData(
                external_id="bybit-card:" + min(r.cursor for r in cluster),
                description=description,
                amount=amount,
                date=to_date(first),
                type="debit" if net < 0 else "credit",
                currency="USD",
                status="posted",
                payee=payee,
                raw_data=raw,
            ),
            coin_legs,
        )

    warnings = drift_warnings(rows, buckets, emitted_total=emitted_total)
    return BuildResult(transactions=txs, buckets=buckets, warnings=warnings, unknown_types=unknown)
```

Implementation notes:
- The cluster `external_id` uses the smallest cursor of the **whole** cluster (USD rows included), so a cluster's id never depends on which coin legs exist.
- `emitted_total` adds the **rounded** amounts, so `drift_warnings` compares at cent precision with a tolerance of half a cent per emitted row.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_providers_bybit_ledger.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/providers/bybit_ledger.py backend/tests/test_providers_bybit_ledger.py
git commit -m "feat(bybit): card clusters, enrichment, refunds, cashback and drift check"
```

---

### Task 5: The provider

**Files:**
- Create: `backend/app/providers/bybit.py`
- Test: `backend/tests/test_providers_bybit.py` (append)

**Interfaces:**
- Consumes: `BybitClient`, `BybitError`, path constants (Task 2); `parse_ledger_row`, `parse_card_record`, `parse_point_redemption_ts`, `build_transactions`, `quantize`, `STABLE` (Tasks 3–4); `encrypt`/`decrypt` from `app.agents.services.crypto`.
- Produces: `class BybitProvider(BankProvider)` with `name == "bybit"`, `flow_type == "credentials"`; `ACCOUNT_EXTERNAL_ID = "bybit-usd"`; `FIRST_SYNC_WEEKS = 52`; credentials dict `{"api_key_enc": str, "api_secret_enc": str, "key_expires_at": str | None}`; method `_client(self) -> httpx.AsyncClient` (patched in tests).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_providers_bybit.py`, moving the new imports into the file's top import block (ruff `E402` rejects imports below code):

```python
import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest

from app.agents.services.crypto import encrypt
from app.providers.base import ProviderUserActionRequired, SessionExpiredError
from app.providers.bybit import ACCOUNT_EXTERNAL_ID, BybitProvider

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
        ok = lambda result: httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest tests/test_providers_bybit.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.providers.bybit'`.

- [ ] **Step 3: Implement**

Create `backend/app/providers/bybit.py`:

```python
"""Bybit provider: one USD account from a read-only API key.

Money comes only from the Funding ledger; card records and points only add
names. See docs/superpowers/specs/2026-10-03-bybit-provider-design.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx

from app.agents.services.crypto import decrypt, encrypt
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    ProviderRateLimited,
    ProviderUserActionRequired,
    SessionExpiredError,
    TransactionData,
)
from app.providers.bybit_client import (
    PATH_CARD_RECORDS,
    PATH_EARN_POSITION,
    PATH_FUND_BALANCE,
    PATH_FUNDING_HISTORY,
    PATH_INTER_TRANSFER,
    PATH_POINTS_RECORDS,
    PATH_QUERY_API,
    PATH_UTA_BALANCE,
    TIMEOUT,
    BybitClient,
    BybitError,
)
from app.providers.bybit_ledger import (
    STABLE,
    build_transactions,
    classify,
    parse_card_record,
    parse_ledger_row,
    parse_point_redemption_ts,
    quantize,
)

logger = logging.getLogger(__name__)

ACCOUNT_EXTERNAL_ID = "bybit-usd"
FIRST_SYNC_WEEKS = 52
WEEK_S = 7 * 86400
SINCE_MARGIN_S = 60
_EXPIRED_CODES = {"10003", "33004", "10005"}
_TRANSIENT_CODES = {"network", "http_500", "http_502", "http_503", "http_504"}
_MAX_PAGES = 200


class BybitProvider(BankProvider):
    @property
    def name(self) -> str:
        return "bybit"

    @property
    def flow_type(self) -> str:
        return "credentials"

    def get_oauth_url(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError("Bybit uses a read-only API key, not OAuth")

    def _client(self) -> httpx.AsyncClient:
        from app.core.config import get_settings

        return httpx.AsyncClient(base_url=get_settings().bybit_base_url, timeout=TIMEOUT)

    # ----- credentials ---------------------------------------------------

    @staticmethod
    def _keys(credentials: dict) -> tuple[str, str]:
        key_enc = (credentials or {}).get("api_key_enc") or ""
        secret_enc = (credentials or {}).get("api_secret_enc") or ""
        if not key_enc or not secret_enc:
            raise ProviderUserActionRequired(
                "The Bybit API key is missing. Reconnect with a read-only key.",
                code="bybit_credential_missing",
            )
        key, secret = decrypt(key_enc), decrypt(secret_enc)
        if not key or not secret:
            raise ProviderUserActionRequired(
                "The stored Bybit key could not be read. Reconnect with a read-only key.",
                code="bybit_credential_unreadable",
            )
        return key, secret

    def _bybit(self, http: httpx.AsyncClient, key: str, secret: str) -> BybitClient:
        return BybitClient(http, key, secret, sleep=asyncio.sleep, clock=time.time)

    def _now(self) -> int:
        return int(time.time())

    async def _validate(self, client: BybitClient, *, claim: bool) -> dict:
        try:
            info = await client.get(PATH_QUERY_API, {}, "query_api")
        except BybitError as exc:
            if exc.code in _EXPIRED_CODES:
                if claim:
                    raise ProviderUserActionRequired(
                        "Bybit rejected this API key. Check it and try again.",
                        code="bybit_key_rejected",
                    ) from None
                raise SessionExpiredError("Bybit API key expired or was revoked") from None
            raise
        if info.get("readOnly") != 1:
            raise ProviderUserActionRequired(
                "This Bybit key can trade or withdraw. Create a read-only key.",
                code="bybit_key_not_read_only",
            )
        permissions = info.get("permissions") or {}
        if not permissions.get("Earn"):
            raise ProviderUserActionRequired(
                "Tick the Earn permission on the Bybit key, then reconnect.",
                code="bybit_key_missing_earn",
            )
        return info

    @staticmethod
    def _expires_at(info: dict) -> Optional[str]:
        expired_at = info.get("expiredAt")
        deadline = info.get("deadlineDay")
        if not isinstance(expired_at, str) or expired_at.startswith("1970") or (isinstance(deadline, int) and deadline < 0):
            return None
        return expired_at

    def _decode_claim(self, code: str) -> tuple[str, str]:
        try:
            payload = json.loads(code)
        except (TypeError, ValueError):
            raise BybitError("connect", "schema") from None
        if not isinstance(payload, dict):
            raise BybitError("connect", "schema")
        key = str(payload.get("api_key") or "").strip()
        secret = str(payload.get("api_secret") or "").strip()
        if not key or not secret:
            raise ProviderUserActionRequired(
                "Enter both the Bybit API key and the API secret.",
                code="bybit_credential_missing",
            )
        return key, secret

    async def handle_oauth_callback(self, code: str) -> ConnectionData:
        key, secret = self._decode_claim(code)
        key_enc, secret_enc = encrypt(key), encrypt(secret)
        if not key_enc or not secret_enc:
            raise BybitError("connect", "crypto")
        try:
            async with self._client() as http:
                client = self._bybit(http, key, secret)
                info = await self._validate(client, claim=True)
                balance = await self._balance(client)
        except ProviderRateLimited:
            raise ProviderUserActionRequired(
                "Bybit is rate-limiting this key. Try again in a minute.",
                code="bybit_rate_limited",
            ) from None
        uid_hash = hashlib.sha256(str(info.get("userID") or "").encode()).hexdigest()[:16]
        return ConnectionData(
            external_id=f"bybit:{uid_hash}",
            institution_name="Bybit",
            credentials={"api_key_enc": key_enc, "api_secret_enc": secret_enc, "key_expires_at": self._expires_at(info)},
            accounts=[self._account(balance)],
        )

    async def refresh_credentials(self, credentials: dict) -> dict:
        """Re-checks the key on every sync (read-only, Earn) and records its
        expiry. Securo persists the returned dict as the new credentials."""
        key, secret = self._keys(credentials)
        async with self._client() as http:
            info = await self._validate(self._bybit(http, key, secret), claim=False)
        return {**credentials, "key_expires_at": self._expires_at(info)}

    # ----- balance --------------------------------------------------------

    @staticmethod
    def _account(balance: Decimal) -> AccountData:
        return AccountData(external_id=ACCOUNT_EXTERNAL_ID, name="Bybit", type="checking", balance=balance, currency="USD")

    async def _balance(self, client: BybitClient) -> Decimal:
        total = Decimal("0")
        fund = await client.get(PATH_FUND_BALANCE, {"accountType": "FUND"}, "balance_fund")
        for row in fund.get("balance") or []:
            if isinstance(row, dict) and row.get("coin") in STABLE:
                total += _money(row.get("walletBalance"), "balance_fund")
        uta = await client.get(PATH_UTA_BALANCE, {"accountType": "UNIFIED", "coin": "USDT,USDC,USD"}, "balance_uta")
        for account in uta.get("list") or []:
            for row in (account or {}).get("coin") or []:
                if isinstance(row, dict) and row.get("coin") in STABLE:
                    total += _money(row.get("walletBalance"), "balance_uta")
        earn = await client.get(PATH_EARN_POSITION, {"category": "FlexibleSaving"}, "balance_earn")
        for row in earn.get("list") or []:
            if isinstance(row, dict) and row.get("coin") in STABLE:
                total += _money(row.get("amount"), "balance_earn") + _money(row.get("claimableYield") or "0", "balance_earn")
        return quantize(total)

    async def get_accounts(self, credentials: dict) -> list[AccountData]:
        key, secret = self._keys(credentials)
        async with self._client() as http:
            return [self._account(await self._balance(self._bybit(http, key, secret)))]

    # ----- transactions ---------------------------------------------------

    async def _ledger(self, client: BybitClient, start: int, end: int) -> list:
        rows: dict[str, object] = {}
        window = start
        while window <= end:
            to = min(window + WEEK_S - 1, end)
            cursor = ""
            for _ in range(_MAX_PAGES):
                params = {"createTimeFrom": str(window), "createTimeTo": str(to), "limit": "100"}
                if cursor:
                    params["cursor"] = cursor
                result = await client.get(PATH_FUNDING_HISTORY, params, "ledger")
                for raw in result.get("list") or []:
                    row = parse_ledger_row(raw)
                    if row is not None:
                        rows[row.cursor] = row
                cursor = result.get("nextPageCursor") or ""
                if not cursor:
                    break
            window = to + 1
        return list(rows.values())

    async def _paged_card(self, client: BybitClient, card_type: str, start: int, end: int) -> list[dict]:
        out: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            result = await client.post(
                PATH_CARD_RECORDS,
                {"type": card_type, "limit": 500, "page": page, "createBeginTime": start * 1000, "createEndTime": end * 1000},
                "card",
            )
            data = result.get("data") or []
            out.extend(data)
            if not data or len(out) >= int(result.get("totalCount") or 0):
                break
        return out

    async def _points(self, client: BybitClient) -> list[dict]:
        out: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            result = await client.post(PATH_POINTS_RECORDS, {"pageNo": page, "pageSize": 50}, "points")
            data = result.get("data") or []
            out.extend(data)
            if not data or len(out) >= int(result.get("totalCount") or 0):
                break
        return out

    async def _internal_transfer_cursors(self, client: BybitClient, rows: list, unknown_cursors: set[str]) -> set[str]:
        """Unknown rows that mirror a FUND<->UNIFIED transfer (same coin and
        amount within 10 s) are moves inside the account. Only called when an
        unknown row exists."""
        found: set[str] = set()
        candidates = [r for r in rows if r.cursor in unknown_cursors]
        for row in candidates:
            result = await client.get(
                PATH_INTER_TRANSFER,
                {"startTime": str((row.ts - 10) * 1000), "endTime": str((row.ts + 10) * 1000), "coin": row.currency},
                "inter_transfer",
            )
            for t in result.get("list") or []:
                accounts = {t.get("fromAccountType"), t.get("toAccountType")}
                if accounts == {"FUND", "UNIFIED"} and str(t.get("status")) == "SUCCESS" and _money(t.get("amount"), "inter_transfer") == row.amount:
                    found.add(row.cursor)
        return found

    async def get_transactions(
        self,
        credentials: dict,
        account_external_id: str,
        since: Optional[date] = None,
        payee_source: str = "auto",
    ) -> list[TransactionData]:
        key, secret = self._keys(credentials)
        now = self._now()
        if since is None:
            since_ts = now - FIRST_SYNC_WEEKS * WEEK_S
        else:
            since_ts = int(datetime(since.year, since.month, since.day, tzinfo=timezone.utc).timestamp())
        start = since_ts - SINCE_MARGIN_S

        async with self._client() as http:
            client = self._bybit(http, key, secret)
            rows = await self._ledger(client, start, now)

            purchases, refunds, transient = [], [], False
            try:
                purchases = [r for r in (parse_card_record(x) for x in await self._paged_card(client, "SIDE_QUERY_AUTH_ALL", start, now)) if r]
                refunds = [r for r in (parse_card_record(x, refund=True) for x in await self._paged_card(client, "SIDE_QUERY_FINANCIAL_REFUND", start - 30 * 86400, now)) if r]
            except ProviderRateLimited:
                transient = True
            except BybitError as exc:
                transient = exc.code in _TRANSIENT_CODES or exc.code.startswith("http_5")
                if not transient and exc.code != "10005":
                    raise

            redemptions: list[int] = []
            try:
                redemptions = [t for t in (parse_point_redemption_ts(x) for x in await self._points(client)) if t is not None]
            except (ProviderRateLimited, BybitError):
                logger.info("bybit points unavailable; cashback rows keep the generic label")

            unknown_cursors = {r.cursor for r in rows if classify(r)[0] == "unknown" and r.ts >= since_ts}
            internal = await self._internal_transfer_cursors(client, rows, unknown_cursors) if unknown_cursors else set()

        result = build_transactions(
            rows,
            since_ts=since_ts,
            now_ts=now,
            purchases=purchases,
            refunds=refunds,
            redemption_ts=redemptions,
            card_transient_failure=transient,
            internal_cursors=internal,
        )
        for kind in sorted(result.unknown_types):
            logger.warning("bybit: unrecognised ledger row type %s imported as-is", kind)
        for warning in result.warnings:
            logger.warning("%s", warning)
        return result.transactions


def _money(value: object, stage: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except Exception:
        raise BybitError(stage, "schema") from None
    if not out.is_finite():
        raise BybitError(stage, "schema")
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_providers_bybit.py tests/test_providers_bybit_client.py tests/test_providers_bybit_ledger.py -q`
Expected: all pass.

- [ ] **Step 5: Lint, types, full suite**

Run: `cd backend && uv run ruff check . && uv run ty check && uv run pytest -q -n auto -p no:cacheprovider`
Expected: ruff "All checks passed!", ty clean, full suite green.

- [ ] **Step 6: Commit**

```bash
git add backend/app/providers/bybit.py backend/tests/test_providers_bybit.py
git commit -m "feat(bybit): provider with read-only key checks, balance and ledger sync"
```

---

### Task 6: Field-driven credentials dialog

**Files:**
- Modify: `frontend/src/components/credentials-connect-dialog.tsx`
- Modify: `frontend/src/lib/api.ts:359` (the `getProviders` return type)
- Modify: the `Provider` type used in `frontend/src/pages/accounts.tsx` (find it: `grep -rn "type Provider\b\|interface Provider\b" frontend/src`)
- Modify: `frontend/src/pages/accounts.tsx` (both `<CredentialsConnectDialog>` usages, ~lines 619 and 641)
- Create: `frontend/src/components/credentials-connect-dialog.test.tsx`

**Interfaces:**
- Consumes: backend `credential_fields` (Task 1): `{name, label_key, placeholder_key, secret}`.
- Produces: `export interface CredentialField { name: string; label_key: string; placeholder_key?: string; secret: boolean }`; `CredentialsConnectDialog` gains optional prop `fields?: CredentialField[]`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/credentials-connect-dialog.test.tsx`:

```tsx
import { screen } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { CredentialsConnectDialog } from './credentials-connect-dialog'

const api = vi.hoisted(() => ({ handleCallback: vi.fn(), toastError: vi.fn(), toastSuccess: vi.fn() }))
vi.mock('@/lib/api', () => ({ connections: api }))
vi.mock('sonner', () => ({ toast: { error: api.toastError, success: api.toastSuccess } }))

beforeEach(() => { vi.resetAllMocks() })

const BYBIT_FIELDS = [
  { name: 'api_key', label_key: 'accounts.credentialsConnect.bybit.apiKeyLabel', placeholder_key: 'accounts.credentialsConnect.bybit.apiKeyPlaceholder', secret: false },
  { name: 'api_secret', label_key: 'accounts.credentialsConnect.bybit.apiSecretLabel', placeholder_key: 'accounts.credentialsConnect.bybit.apiSecretPlaceholder', secret: true },
]

it('keeps the user ID + password form when a provider declares no fields', async () => {
  api.handleCallback.mockResolvedValue({})
  const { user } = renderWithProviders(<CredentialsConnectDialog open provider="accessbank" onClose={vi.fn()} />)
  const userId = screen.getByLabelText('User ID')
  const password = screen.getByLabelText('Password')
  expect(password).toHaveAttribute('type', 'password')
  await user.type(userId, ' someone ')
  await user.type(password, 'pw')
  await user.click(screen.getByRole('button', { name: 'Connect' }))
  expect(api.handleCallback).toHaveBeenCalledWith(JSON.stringify({ user_id: 'someone', password: 'pw' }), 'accessbank', undefined, undefined, undefined)
})

it('renders the provider fields, masks secrets, and sends them by name', async () => {
  api.handleCallback.mockResolvedValue({})
  const { user } = renderWithProviders(<CredentialsConnectDialog open provider="bybit" fields={BYBIT_FIELDS} onClose={vi.fn()} />)
  const key = screen.getByLabelText('API key')
  const secret = screen.getByLabelText('API secret')
  expect(key).toHaveAttribute('type', 'text')
  expect(secret).toHaveAttribute('type', 'password')
  expect(secret).toHaveAttribute('autocomplete', 'new-password')
  expect(screen.getByText(/read-only API key/i)).toBeInTheDocument()
  const connect = screen.getByRole('button', { name: 'Connect' })
  await user.type(key, ' k-123 ')
  expect(connect).toBeDisabled()
  await user.type(secret, 's-456')
  await user.click(connect)
  expect(api.handleCallback).toHaveBeenCalledWith(JSON.stringify({ api_key: 'k-123', api_secret: 's-456' }), 'bybit', undefined, undefined, undefined)
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx -y node@22 node_modules/.bin/vitest run src/components/credentials-connect-dialog.test.tsx`
Expected: FAIL — no `API key` label (the dialog ignores `fields`, and the strings do not exist yet; Task 7 adds them, so this test stays red until Task 7 Step 3).

- [ ] **Step 3: Implement the dialog**

Replace the state, payload and inputs in `frontend/src/components/credentials-connect-dialog.tsx`:

```tsx
export interface CredentialField {
  name: string
  label_key: string
  placeholder_key?: string
  secret: boolean
}

const DEFAULT_FIELDS: CredentialField[] = [
  { name: 'user_id', label_key: 'accounts.credentialsConnect.userIdLabel', placeholder_key: 'accounts.credentialsConnect.userIdPlaceholder', secret: false },
  { name: 'password', label_key: 'accounts.credentialsConnect.passwordLabel', placeholder_key: 'accounts.credentialsConnect.passwordPlaceholder', secret: true },
]

interface CredentialsConnectDialogProps {
  open: boolean
  onClose: () => void
  provider: string
  reconnectConnectionId?: string
  fields?: CredentialField[]
}
```

Inside the component, replace `userId`/`password` state with:

```tsx
  const formFields = fields && fields.length > 0 ? fields : DEFAULT_FIELDS
  const [values, setValues] = useState<Record<string, string>>({})
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (!open) {
      setValues({})
      setSubmitting(false)
    }
  }, [open])

  // Secrets are sent exactly as typed; everything else is trimmed.
  const cleaned = (f: CredentialField) => (f.secret ? values[f.name] ?? '' : (values[f.name] ?? '').trim())
  const complete = formFields.every((f) => cleaned(f) !== '')
```

`handleSubmit` starts with `if (!complete) return` and sends:

```tsx
      await connections.handleCallback(
        JSON.stringify(Object.fromEntries(formFields.map((f) => [f.name, cleaned(f)]))),
        provider,
        undefined,
        undefined,
        reconnectConnectionId,
      )
```

The privacy note becomes per-provider with the generic fallback:

```tsx
        <p className="text-xs text-muted-foreground">
          {t(`${i18nKey}.privacyNote`, t('accounts.credentialsConnect.privacyNote'))}
        </p>
```

Replace the two hard-coded input blocks with:

```tsx
        {formFields.map((f) => {
          const id = `securo-credentials-${f.name}`
          return (
            <div key={f.name} className="space-y-1.5">
              <label className="text-sm font-medium" htmlFor={id}>
                {t(f.label_key)}
              </label>
              <input
                id={id}
                type={f.secret ? 'password' : 'text'}
                className="w-full rounded-md border border-input bg-card px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-0"
                placeholder={f.placeholder_key ? t(f.placeholder_key) : undefined}
                value={values[f.name] ?? ''}
                onChange={(e) => setValues((v) => ({ ...v, [f.name]: e.target.value }))}
                spellCheck={false}
                autoComplete={f.secret ? 'new-password' : 'off'}
                disabled={submitting}
              />
            </div>
          )
        })}
```

The Connect button's `disabled` becomes `disabled={!complete || submitting}`.

Note: the default `password` field now renders `autoComplete="new-password"` instead of `"off"`. Both stop the browser autofilling a stored password; `new-password` is the one browsers honour. Accessbank's payload is unchanged.

- [ ] **Step 4: Pass the fields through**

In `frontend/src/lib/api.ts:359`, add to the inline return type: `credential_fields?: { name: string; label_key: string; placeholder_key?: string; secret: boolean }[]`.
Add the same optional property to the `Provider` type found in Files above.

In `frontend/src/pages/accounts.tsx`, connect dialog:

```tsx
      <CredentialsConnectDialog
        open={!!selectedProvider && selectedProvider.flow_type === 'credentials'}
        onClose={() => setSelectedProvider(null)}
        provider={selectedProvider?.name ?? ''}
        fields={selectedProvider?.credential_fields}
      />
```

Reconnect dialog:

```tsx
      <CredentialsConnectDialog
        open={!!credentialsReconnectConnection}
        onClose={() => setCredentialsReconnectConnection(null)}
        provider={credentialsReconnectConnection?.provider ?? ''}
        reconnectConnectionId={credentialsReconnectConnection?.id}
        fields={credentialsReconnectConnection ? providersByName.get(credentialsReconnectConnection.provider)?.credential_fields : undefined}
      />
```

- [ ] **Step 5: Commit (the test goes green in Task 7)**

```bash
git add frontend/src/components/credentials-connect-dialog.tsx frontend/src/components/credentials-connect-dialog.test.tsx frontend/src/lib/api.ts frontend/src/pages/accounts.tsx $(grep -rln "interface Provider\b\|type Provider\b" frontend/src)
git commit -m "feat(connect): credentials dialog renders provider-declared fields"
```

---

### Task 7: Bybit strings in all 15 locales

**Files:**
- Modify: `frontend/src/locales/{de,el,en,es,fr,hi,it,ja,nl,pl,pt-BR,pt-PT,ru,sk,uk}.json`

**Interfaces:**
- Produces keys under `accounts.credentialsConnect.bybit`: `title`, `description`, `apiKeyLabel`, `apiKeyPlaceholder`, `apiSecretLabel`, `apiSecretPlaceholder`, `privacyNote`, `reconnectTitle`, `reconnectDescription`.

- [ ] **Step 1: Add the English block**

In `frontend/src/locales/en.json`, directly after the `"accessbank": {…}` block inside `credentialsConnect`:

```json
      "bybit": {
        "title": "Connect Bybit",
        "description": "Paste a read-only Bybit API key. Create it in Bybit under Profile → API → Create New Key, with Read-Only, and tick Wallet, Earn and Bybit Card.",
        "apiKeyLabel": "API key",
        "apiKeyPlaceholder": "Your Bybit API key",
        "apiSecretLabel": "API secret",
        "apiSecretPlaceholder": "Your Bybit API secret",
        "privacyNote": "Securo only accepts a read-only API key. It can see your balance and transactions but cannot trade or withdraw. The secret is stored encrypted.",
        "reconnectTitle": "Replace the Bybit key",
        "reconnectDescription": "Paste a new read-only key. Existing transactions, categories and rules are kept."
      },
```

- [ ] **Step 2: Add the same nine keys to the other 14 locales**

Each locale's `accessbank` block is translated, so translate these nine strings into that language, keeping the same keys, the product names "Bybit", "Bybit Card", "API" untranslated, and the menu path `Profile → API → Create New Key` in English (it is Bybit's own UI text). Place the block after that locale's `"accessbank"` block.

- [ ] **Step 3: Run the dialog test and the locale parity test**

Run: `cd frontend && npx -y node@22 node_modules/.bin/vitest run src/components/credentials-connect-dialog.test.tsx src/locales/i18n.test.ts`
Expected: all pass.

- [ ] **Step 4: Full frontend checks**

Run: `cd frontend && npx -y node@22 node_modules/.bin/vitest run && npx -y node@22 node_modules/.bin/tsc -b && npm run lint`
Expected: all test files pass; `tsc` silent; lint 0 errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/locales/*.json
git commit -m "feat(i18n): Bybit connect strings in all locales"
```

---

### Task 8: Deployment wiring

**Files:**
- Modify: `docker-compose.yml` (after line 48), `docker-compose.prod.yml` (after line 44)
- Modify: `deploy/values.yaml` (after the `accessbankImportCurrencies` line, ~67)

**Interfaces:**
- Produces env `BYBIT_ENABLED`, `BYBIT_BASE_URL` for backend, worker and beat (the chart's configmap turns every `config.*` key into an env var; the release pipeline renders `deploy/manifests.yaml` from `deploy/values.yaml`).

- [ ] **Step 1: Compose files**

In both `docker-compose.yml` and `docker-compose.prod.yml`, after the `ACCESSBANK_IMPORT_CURRENCIES` line:

```yaml
  BYBIT_ENABLED: ${BYBIT_ENABLED:-false}
  BYBIT_BASE_URL: ${BYBIT_BASE_URL:-https://api.bybit.com}
```

- [ ] **Step 2: Deployment values**

In `deploy/values.yaml`, after `accessbankImportCurrencies: "USD"`:

```yaml
  # Bybit: read-only API key per connection, entered in the app. Only
  # bybit.com accounts (api.bybit.com); EEA bybit.eu keys are not supported
  # by Bybit itself. See backend/app/providers/bybit.py.
  bybitEnabled: "true"
```

- [ ] **Step 3: Verify the render**

Run: `helm template securo charts/securo -f deploy/values.yaml -n securo | grep -n 'BYBIT_ENABLED'`
Expected: one line in the `securo-config` ConfigMap, value `"true"`.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml docker-compose.prod.yml deploy/values.yaml
git commit -m "deploy: enable the Bybit provider"
```

---

### Task 9: Whole-branch verification

- [ ] **Step 1: Backend**

Run: `cd backend && uv run ruff check . && uv run ty check && uv run pytest -q -n auto -p no:cacheprovider`
Expected: clean, all pass.

- [ ] **Step 2: Frontend**

Run: `cd frontend && npx -y node@22 node_modules/.bin/vitest run && npx -y node@22 node_modules/.bin/tsc -b && npm run lint`
Expected: all pass.

- [ ] **Step 3: No real data in the diff**

Run: `git diff main --stat && git diff main | grep -nE '[0-9]{6,}' | grep -v -E 'synthetic|1_7[0-9]{2}_000_000|NOW_S|T0|411111|777000|424242|1700000000000' || echo clean`
Expected: `clean`, or only numbers that are visibly synthetic constants. Any real-looking id, address or amount is a stop.

- [ ] **Step 4: Push the branch and open a PR to the fork's main**

Only with the owner's go-ahead. No `Co-Authored-By`/`Claude-Session` trailers and no generated-with footer in the PR body.
