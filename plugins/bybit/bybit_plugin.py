"""Bybit as a Securo bank provider plugin: one USD account from a read-only API key.

Money comes only from the Funding ledger; card records and points only add
names. A port of the fork's native provider (`app.providers.bybit`): the same
endpoints, the same rules and the same external ids, so the accounts and
transactions the native provider wrote are matched, not duplicated, once they
move onto a connection of this plugin.

Securo calls `plugin.sync(params, context)` in the plugin runner. Standard
library only: `python -I -S`, no lockfile.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from bybit_api import (
    PATH_CARD_RECORDS,
    PATH_EARN_POSITION,
    PATH_FUND_BALANCE,
    PATH_FUNDING_HISTORY,
    PATH_INTER_TRANSFER,
    PATH_POINTS_RECORDS,
    PATH_QUERY_API,
    PATH_UTA_BALANCE,
    BybitClient,
    BybitError,
    RateLimited,
)
from bybit_ledger import (
    STABLE,
    LedgerRow,
    build_transactions,
    classify,
    parse_card_record,
    parse_ledger_row,
    parse_point_redemption_ts,
    quantize,
)

logger = logging.getLogger("bybit")

CONTRACT = 1
ACCOUNT_EXTERNAL_ID = "bybit-usd"
FIRST_SYNC_WEEKS = 52
WEEK_S = 7 * 86400
SINCE_MARGIN_S = 60
_EXPIRED_CODES = {"10003", "33004", "10005"}
_TRANSIENT_CODES = {"network", "http_500", "http_502", "http_503", "http_504"}
_MAX_PAGES = 200


class NeedsAuth(Exception):
    """The key is missing, expired or revoked. Securo goes by the class name and asks for a reconnect."""


class KeyNotAllowed(Exception):
    """The key works but has the wrong permissions. An error, not a reconnect: fixing the key's
    permissions in Bybit is enough, and the next scheduled sync picks it up."""


# The only exception types that may leave the plugin. Anything else (a
# KeyError, TypeError or ValueError from a response shape Bybit changed) is
# turned into BybitError(stage, "schema") with no message text, so neither
# response values nor credentials reach the sync's error or the runner's logs.
_ESCAPABLE = (BybitError, NeedsAuth, KeyNotAllowed, RateLimited)


def _staged(stage: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except _ESCAPABLE:
        raise
    except Exception:
        raise BybitError(stage, "schema") from None


def _keys(secrets: Any) -> tuple[str, str]:
    key = str(secrets.get("api_key") or "").strip()
    secret = str(secrets.get("api_secret") or "").strip()
    if not key or not secret:
        raise NeedsAuth("The Bybit API key is missing. Reconnect with a read-only key.")
    return key, secret


def _money(value: object, stage: str) -> Decimal:
    try:
        out = Decimal(str(value))
    except Exception:
        raise BybitError(stage, "schema") from None
    if not out.is_finite():
        raise BybitError(stage, "schema")
    return out


def validate(client: BybitClient) -> dict:
    """Re-checks the key on every sync: it must be read-only and carry Earn."""
    try:
        info = client.get(PATH_QUERY_API, {}, "query_api")
    except BybitError as exc:
        if exc.code in _EXPIRED_CODES:
            raise NeedsAuth("Bybit API key expired or was revoked") from None
        raise
    if info.get("readOnly") != 1:
        raise KeyNotAllowed("This Bybit key can trade or withdraw. Create a read-only key.")
    permissions = info.get("permissions")
    if not isinstance(permissions, dict) or not permissions.get("Earn"):
        raise KeyNotAllowed("Tick the Earn permission on the Bybit key, then reconnect.")
    return info


def expires_at(info: dict) -> Optional[str]:
    expired_at = info.get("expiredAt")
    deadline = info.get("deadlineDay")
    if not isinstance(expired_at, str) or expired_at.startswith("1970") or (isinstance(deadline, int) and deadline < 0):
        return None
    return expired_at


def balance(client: BybitClient) -> Decimal:
    total = Decimal("0")
    fund = client.get(PATH_FUND_BALANCE, {"accountType": "FUND"}, "balance_fund")
    for row in fund.get("balance") or []:
        if isinstance(row, dict) and row.get("coin") in STABLE:
            total += _money(row.get("walletBalance") or "0", "balance_fund")
    uta = client.get(PATH_UTA_BALANCE, {"accountType": "UNIFIED", "coin": "USDT,USDC,USD"}, "balance_uta")
    for account in uta.get("list") or []:
        for row in (account or {}).get("coin") or []:
            if isinstance(row, dict) and row.get("coin") in STABLE:
                total += _money(row.get("walletBalance") or "0", "balance_uta")
    earn = client.get(PATH_EARN_POSITION, {"category": "FlexibleSaving"}, "balance_earn")
    for row in earn.get("list") or []:
        if isinstance(row, dict) and row.get("coin") in STABLE:
            total += _money(row.get("amount") or "0", "balance_earn") + _money(row.get("claimableYield") or "0", "balance_earn")
    return quantize(total)


def _ledger(client: BybitClient, start: int, end: int) -> list[LedgerRow]:
    rows: dict[str, LedgerRow] = {}
    window = start
    while window <= end:
        to = min(window + WEEK_S - 1, end)
        cursor = ""
        for _ in range(_MAX_PAGES):
            params = {"createTimeFrom": str(window), "createTimeTo": str(to), "limit": "100"}
            if cursor:
                params["cursor"] = cursor
            result = client.get(PATH_FUNDING_HISTORY, params, "ledger")
            for raw in result.get("list") or []:
                row = parse_ledger_row(raw)
                if row is not None:
                    rows[row.cursor] = row
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        window = to + 1
    return list(rows.values())


def _paged_card(client: BybitClient, card_type: str, start: int, end: int) -> list[dict]:
    out: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        result = client.post(
            PATH_CARD_RECORDS,
            {"type": card_type, "limit": 500, "page": page, "createBeginTime": start * 1000, "createEndTime": end * 1000},
            "card",
        )
        data = result.get("data") or []
        out.extend(data)
        if not data or len(out) >= int(result.get("totalCount") or 0):
            break
    return out


def _points(client: BybitClient) -> list[dict]:
    out: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        result = client.post(PATH_POINTS_RECORDS, {"pageNo": page, "pageSize": 50}, "points")
        data = result.get("data") or []
        out.extend(data)
        if not data or len(out) >= int(result.get("totalCount") or 0):
            break
    return out


def _internal_transfer_cursors(client: BybitClient, rows: list[LedgerRow], unknown_cursors: set[str]) -> set[str]:
    """Unknown rows that mirror a FUND<->UNIFIED transfer (same coin and
    amount within 10 s) are moves inside the account. Only called when an
    unknown row exists."""
    found: set[str] = set()
    for row in [r for r in rows if r.cursor in unknown_cursors]:
        result = client.get(
            PATH_INTER_TRANSFER,
            {"startTime": str((row.ts - 10) * 1000), "endTime": str((row.ts + 10) * 1000), "coin": row.currency},
            "inter_transfer",
        )
        for t in result.get("list") or []:
            accounts = {t.get("fromAccountType"), t.get("toAccountType")}
            if accounts == {"FUND", "UNIFIED"} and str(t.get("status")) == "SUCCESS" and _money(t.get("amount"), "inter_transfer") == row.amount:
                found.add(row.cursor)
    return found


def transactions(client: BybitClient, since: Optional[date], now: int) -> list[dict]:
    if since is None:
        since_ts = now - FIRST_SYNC_WEEKS * WEEK_S
    else:
        since_ts = int(datetime(since.year, since.month, since.day, tzinfo=timezone.utc).timestamp())
    start = since_ts - SINCE_MARGIN_S

    rows = _ledger(client, start, now)

    purchases, refunds, transient = [], [], False
    try:
        purchases = [r for r in (parse_card_record(x) for x in _paged_card(client, "SIDE_QUERY_AUTH_ALL", start, now)) if r]
        refunds = [r for r in (parse_card_record(x, refund=True) for x in _paged_card(client, "SIDE_QUERY_FINANCIAL_REFUND", start - 30 * 86400, now)) if r]
    except RateLimited:
        transient = True
    except BybitError as exc:
        transient = exc.code in _TRANSIENT_CODES or exc.code.startswith("http_5")
        if not transient and exc.code != "10005":
            raise

    redemptions: list[int] = []
    try:
        redemptions = [t for t in (parse_point_redemption_ts(x) for x in _points(client)) if t is not None]
    except (RateLimited, BybitError):
        logger.info("bybit points unavailable; cashback rows keep the generic label")

    unknown_cursors = {r.cursor for r in rows if classify(r)[0] == "unknown" and r.ts >= since_ts}
    internal = _internal_transfer_cursors(client, rows, unknown_cursors) if unknown_cursors else set()

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
    return [tx.to_payload() for tx in result.transactions]


class Plugin:
    def __init__(self, client_factory: Optional[Callable[[str, str], BybitClient]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._client_factory = client_factory or (lambda key, secret: BybitClient(key, secret))
        self._clock = clock

    def sync(self, params: dict, context: Any) -> dict:
        """One sync: check the key, read the balance, read the ledger from `since`.

        `params["since"]` is the first day Securo wants again (`YYYY-MM-DD`),
        or None on the first sync, which reads the last 52 weeks.

        The connection's very first sync (`state` still empty) only links:
        it calls nothing and writes nothing, so accounts the native provider
        wrote can be moved onto this connection before records arrive.
        """
        state = dict(params.get("state") or {})
        if not state:
            return {"state": {"started": True}, "data": None}
        key, secret = _keys(context.secrets)
        since = _staged("transactions", lambda: date.fromisoformat(params["since"]) if params.get("since") else None)
        client = self._client_factory(key, secret)
        info = _staged("refresh", lambda: validate(client))
        usd = _staged("accounts", lambda: balance(client))
        now = int(self._clock())
        txs = _staged("transactions", lambda: transactions(client, since, now))
        account = {
            "external_id": ACCOUNT_EXTERNAL_ID,
            "name": "Bybit",
            "type": "checking",
            "balance": str(usd),
            "currency": "USD",
            "institution_name": "Bybit",
            "institution_logo": "bybit",
            "transactions": txs,
        }
        return {
            "state": {**state, "started": True, "key_expires_at": expires_at(info)},
            "data": {
                "contract": CONTRACT,
                # Every external_id is a Funding-ledger currcCursor (or the smallest one of
                # a card cluster): one movement each, never re-reported under another id.
                "movement_ids_are_unique": True,
                "accounts": [account],
            },
        }


plugin = Plugin()
