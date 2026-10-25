"""Revenue account mapping for invoice lines."""
from __future__ import annotations

from .posting import RECURRING_REVENUE, SERVICE_REVENUE, account


def revenue_account_for_line(line):
    service = getattr(line, 'service', None)
    if service is not None and service.billing_model in ('monthly', 'quarterly', 'annual', 'amc'):
        return account(RECURRING_REVENUE)
    return account(SERVICE_REVENUE)
