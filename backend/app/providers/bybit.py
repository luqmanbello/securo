"""Bybit provider: one USD account from a read-only API key.

Money comes only from the Funding ledger; card records and points only add
names. See docs/superpowers/specs/2026-10-03-bybit-provider-design.md.
"""
from __future__ import annotations

import asyncio
import functools
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


# The only exception types that may leave this provider. Anything else (a
# KeyError, TypeError or ValueError from a response shape Bybit changed) is
# turned into BybitError(stage, "schema") with no message text, so neither
# response values nor credentials reach logs, the API or Celery results.
_ESCAPABLE = (BybitError, SessionExpiredError, ProviderRateLimited, ProviderUserActionRequired)


def _typed_errors(stage: str):
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except _ESCAPABLE:
                raise
            except Exception:
                raise BybitError(stage, "schema") from None

        return wrapper

    return deco


class BybitProvider(BankProvider):
    # Every external_id is a Funding-ledger currcCursor (or the smallest one of
    # a card cluster): one movement each, never re-reported under another id.
    movement_ids_are_unique = True

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
            if claim and exc.code == "10004":
                raise ProviderUserActionRequired(
                    "The API secret does not match this key. Paste both again.",
                    code="bybit_secret_mismatch",
                ) from None
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
        permissions = info.get("permissions")
        if not isinstance(permissions, dict) or not permissions.get("Earn"):
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

    @_typed_errors("connect")
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

    @_typed_errors("refresh")
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
                total += _money(row.get("walletBalance") or "0", "balance_fund")
        uta = await client.get(PATH_UTA_BALANCE, {"accountType": "UNIFIED", "coin": "USDT,USDC,USD"}, "balance_uta")
        for account in uta.get("list") or []:
            for row in (account or {}).get("coin") or []:
                if isinstance(row, dict) and row.get("coin") in STABLE:
                    total += _money(row.get("walletBalance") or "0", "balance_uta")
        earn = await client.get(PATH_EARN_POSITION, {"category": "FlexibleSaving"}, "balance_earn")
        for row in earn.get("list") or []:
            if isinstance(row, dict) and row.get("coin") in STABLE:
                total += _money(row.get("amount") or "0", "balance_earn") + _money(row.get("claimableYield") or "0", "balance_earn")
        return quantize(total)

    @_typed_errors("accounts")
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

    @_typed_errors("transactions")
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
