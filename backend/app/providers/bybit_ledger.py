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
