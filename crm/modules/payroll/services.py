"""Payroll and commissions service layer (Part 12.4-12.5).

Commission hooks are called from leads, billing and payments. They look up active
commission rules; with no rules configured they do nothing.
"""
from __future__ import annotations


def accrue_commission_for_won(lead):
    from .commissions import accrue
    return accrue('won', lead=lead)


def accrue_commission_for_invoice(invoice):
    from .commissions import accrue
    return accrue('invoiced', invoice=invoice)


def accrue_commission_for_payment(payment):
    from .commissions import accrue
    return accrue('collected', payment=payment)


def clawback_for_credit_note(credit_note):
    from .commissions import clawback
    return clawback(credit_note)
