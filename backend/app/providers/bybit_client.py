"""The only code in Securo that talks to Bybit.

Read-only by construction: every request goes through `_request`, which
refuses any (method, path) outside ALLOWED_ENDPOINTS before touching the
network. Adding an endpoint is a deliberate edit to that set, and the tests
fail on any write prefix or any undeclared API path literal in this module.
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

# Bybit throttles the card API harder than the rest; these are paced.
_CARD_PATHS = frozenset({PATH_CARD_RECORDS, PATH_POINTS_RECORDS})
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
            if path in _CARD_PATHS:
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
