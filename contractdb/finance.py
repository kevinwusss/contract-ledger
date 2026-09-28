from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


CURRENCIES = {"CNY": "人民币 CNY", "USD": "美元 USD", "EUR": "欧元 EUR", "HKD": "港币 HKD", "GBP": "英镑 GBP", "JPY": "日元 JPY", "VND": "越南盾 VND"}

CONFIRMATION_LABELS = {"confirmed": "已确认", "stale": "已变动，需重新确认", "unconfirmed": "未确认"}
CONFIRMATION_KEYS = ("currency", "amount_minor", "received_minor", "unreceived_minor", "payable_minor",
                     "paid_minor", "unpaid_minor", "invoiced_minor", "uninvoiced_minor")


def _value(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def balance_snapshot(row) -> dict:
    """Current receivable/payable balances for a document row; None means the direction was not entered."""
    amount = _value(row, "amount_minor")
    payable = _value(row, "payable_minor")
    received = _value(row, "received_minor", 0) or 0
    paid = _value(row, "paid_minor", 0) or 0
    invoiced = _value(row, "invoiced_minor", 0) or 0
    return {
        "currency": _value(row, "currency", "CNY"),
        "amount_minor": amount,
        "received_minor": received,
        "unreceived_minor": None if amount is None else max(amount - received, 0),
        "payable_minor": payable,
        "paid_minor": paid,
        "unpaid_minor": None if payable is None else max(payable - paid, 0),
        "invoiced_minor": invoiced,
        "uninvoiced_minor": None if amount is None else max(amount - invoiced, 0),
    }


def confirmation_status(row) -> str:
    """Return confirmed / stale / unconfirmed for the latest payment confirmation snapshot."""
    if not _value(row, "confirmation_id"):
        return "unconfirmed"
    current = balance_snapshot(row)
    for key in CONFIRMATION_KEYS:
        if _value(row, f"confirmed_{key}") != current[key]:
            return "stale"
    return "confirmed"


def parse_money(value: str, *, optional=False, positive=False) -> int | None:
    value = value.strip()
    if not value and optional:
        return None
    if not re.fullmatch(r"\d{1,13}(?:\.\d{1,2})?", value):
        raise ValueError("金额请输入非负数字，最多两位小数，不要输入逗号或货币符号")
    amount = int(Decimal(value) * 100)
    if positive and amount <= 0:
        raise ValueError("收款或开票金额必须大于零")
    return amount


def amount_summary(rows):
    """Currency-separated totals for every matching document, including unassigned companies."""
    groups = {}
    for row in rows:
        group = groups.setdefault(row['currency'], dict(currency=row['currency'], count=0,
            receivable=0, received=0, unreceived=0, payable=0, paid=0, unpaid=0,
            invoiced=0, uninvoiced=0, excess_receipt=0, excess_payment=0, excess_invoice=0,
            unknown_receivable=0, unknown_payable=0, confirmed_count=0, stale_count=0,
            unconfirmed_count=0, confirmed_received=0, confirmed_unreceived=0))
        group['count'] += 1
        status = confirmation_status(row)
        group[status + '_count'] += 1
        if status == 'confirmed':
            group['confirmed_received'] += row['received_minor']
            if row['amount_minor'] is not None:
                group['confirmed_unreceived'] += max(row['amount_minor'] - row['received_minor'], 0)
        for field, column in (('received', 'received_minor'), ('paid', 'paid_minor'), ('invoiced', 'invoiced_minor')):
            group[field] += row[column]
        for total, actual, balance, excess, unknown, column in (
            ('receivable', 'received_minor', 'unreceived', 'excess_receipt', 'unknown_receivable', 'amount_minor'),
            ('payable', 'paid_minor', 'unpaid', 'excess_payment', 'unknown_payable', 'payable_minor')):
            if row[column] is None:
                group[unknown] += 1
            else:
                group[total] += row[column]
                group[balance] += max(row[column] - row[actual], 0)
                group[excess] += max(row[actual] - row[column], 0)
        if row['amount_minor'] is not None:
            group['uninvoiced'] += max(row['amount_minor'] - row['invoiced_minor'], 0)
            group['excess_invoice'] += max(row['invoiced_minor'] - row['amount_minor'], 0)
    return sorted(groups.values(), key=lambda item: item['currency'])


def company_summary(rows):
    groups = {}
    for row in rows:
        if not row["company_id"]:
            continue
        key = (row["company_id"], row["currency"])
        group = groups.setdefault(key, {"company_id": row["company_id"], "company_name": row["company_name"], "currency": row["currency"], "contract_count": 0, "unknown_count": 0, "amount_minor": 0, "received_minor": 0, "invoiced_minor": 0, "unpaid_minor": 0, "uninvoiced_minor": 0, "overpaid_minor": 0, "overinvoiced_minor": 0, "confirmed_count": 0, "stale_count": 0, "unconfirmed_count": 0, "confirmed_received_minor": 0, "confirmed_unreceived_minor": 0})
        for field in ('payable_minor', 'paid_minor', 'outstanding_payable_minor', 'excess_payment_minor', 'payable_unknown_count'):
            group.setdefault(field, 0)
        group['paid_minor'] += row['paid_minor']
        if row['payable_minor'] is None:
            group['payable_unknown_count'] += 1
        else:
            group['payable_minor'] += row['payable_minor']
            group['outstanding_payable_minor'] += max(row['payable_minor'] - row['paid_minor'], 0)
            group['excess_payment_minor'] += max(row['paid_minor'] - row['payable_minor'], 0)
        group["contract_count"] += 1
        status = confirmation_status(row)
        group[status + '_count'] += 1
        if status == 'confirmed':
            group['confirmed_received_minor'] += row['received_minor']
            if row['amount_minor'] is not None:
                group['confirmed_unreceived_minor'] += max(row['amount_minor'] - row['received_minor'], 0)
        if row["amount_minor"] is None:
            group["unknown_count"] += 1
            continue
        total = row["amount_minor"]
        paid = row["received_minor"]
        invoiced = row["invoiced_minor"]
        group["amount_minor"] += total
        group["received_minor"] += paid
        group["invoiced_minor"] += invoiced
        group["unpaid_minor"] += max(total - paid, 0)
        group["uninvoiced_minor"] += max(total - invoiced, 0)
        group["overpaid_minor"] += max(paid - total, 0)
        group["overinvoiced_minor"] += max(invoiced - total, 0)
    return sorted(groups.values(), key=lambda item: (item["company_name"], item["currency"]))
