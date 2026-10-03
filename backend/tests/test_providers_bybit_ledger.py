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
