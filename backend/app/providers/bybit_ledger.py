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
