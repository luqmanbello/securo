"""Pure ledger logic. Synthetic rows only."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.providers.bybit_client import BybitError
from app.providers.bybit_ledger import (
    build_transactions,
    parse_card_record,
    parse_point_redemption_ts,
)
from app.providers.bybit_ledger import (
    LedgerRow,
    classify,
    ledger_raw,
    parse_ledger_row,
    quantize,
    single_transaction,
)

T0 = 1_760_000_000  # synthetic epoch seconds


def _lr(row: dict) -> LedgerRow:
    out = parse_ledger_row(row)
    assert out is not None
    return out


def raw(cursor="c1", busi="Deposit", desc="Deposit", currency="USDT", io="I", amt="10", after="10", ts=T0, **extra):
    row = {
        "memberId": "999", "currency": currency, "ioDirection": io, "txnAmt": amt, "afterAmt": after,
        "createTime": str(ts), "showBusiType": "x", "showBusiTypeEn": busi, "description": "x",
        "descriptionEn": desc, "currcCursor": cursor,
    }
    row.update(extra)
    return row


def test_parse_reads_seconds_and_decimals():
    row = _lr(raw(amt="12.345678", after="100.5"))
    assert row == LedgerRow(cursor="c1", busi="Deposit", desc="Deposit", currency="USDT", direction="I",
                            amount=Decimal("12.345678"), after=Decimal("100.5"), ts=T0)
    assert row.signed == Decimal("12.345678")
    assert _lr(raw(io="O")).signed == Decimal("-10")


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
    assert classify(_lr(raw(busi=busi, desc=desc))) == expected


def test_quantize_and_sub_cent_rows_are_skipped():
    assert quantize(Decimal("1.005")) == Decimal("1.01")
    row = _lr(raw(busi="Earn", desc="Easy Earn | Flexible Interest Distribution", amt="0.00000412"))
    assert single_transaction(row, "Bybit Earn interest") is None


def test_single_transaction_shape():
    row = _lr(raw(cursor="abc", io="O", amt="25.5", busi="Withdraw", desc="Withdrawal"))
    tx = single_transaction(row, "Bybit withdrawal")
    assert tx is not None
    assert tx.external_id == "bybit:abc"
    assert tx.amount == Decimal("25.50")
    assert tx.type == "debit"
    assert tx.currency == "USD"
    assert tx.date == date(2025, 10, 9)
    assert tx.description == "Bybit withdrawal"
    assert tx.payee is None
    assert tx.status == "posted"


def test_raw_data_is_an_allowlist():
    row = _lr(raw(toAddress="T-synthetic-address", txID="0xsynthetic", memberId="123"))
    stored = ledger_raw(row)
    assert set(stored) == {"currcCursor", "showBusiTypeEn", "descriptionEn", "currency", "ioDirection", "txnAmt", "createTime"}
    assert stored["txnAmt"] == "10"


NOW = T0 + 30 * 86400
SINCE = T0 - 86400


def L(cursor, busi, desc, currency, io, amt, ts, after="0"):
    return _lr(raw(cursor=cursor, busi=busi, desc=desc, currency=currency, io=io, amt=amt, ts=ts, after=after))


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


def build(rows, *, purchases=(), refunds=(), redemption_ts=(), card_transient_failure=False, internal_cursors=frozenset()):
    return build_transactions(
        rows,
        since_ts=SINCE,
        now_ts=NOW,
        purchases=[r for r in purchases if r is not None],
        refunds=[r for r in refunds if r is not None],
        redemption_ts=list(redemption_ts),
        card_transient_failure=card_transient_failure,
        internal_cursors=set(internal_cursors),
    )


def test_parse_card_record_ignores_verification_auths_and_strips_personal_fields():
    assert parse_card_record(card_raw(sold=False)) is None
    assert parse_card_record(card_raw(basic="0")) is None
    rec = parse_card_record(card_raw())
    assert rec is not None
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
