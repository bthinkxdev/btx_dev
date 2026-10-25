"""Accounting/banking tools - read-only. Posting journal entries, creating bank accounts
and importing statements are accountant/owner-level, system-of-record actions; the
Business Manager role only has 'view' on accounting/banking (see crm/core/roles.py), so no
write tool exists here at all - a "business manager" AI should see financial reports, not
post entries."""
from __future__ import annotations

from crm.core import permissions
from crm.mcp import context, helpers, serialize


def register(mcp):
    @helpers.sync_tool(mcp)
    def get_profit_loss(start_iso: str, end_iso: str) -> dict:
        """Revenue, direct cost, expenses, gross and operating profit for a date range."""
        from datetime import date
        from crm.modules.accounting import reports
        permissions.require(context.current_user(), 'accounting', 'view')
        return serialize.deep(reports.profit_loss(date.fromisoformat(start_iso), date.fromisoformat(end_iso)))

    @helpers.sync_tool(mcp)
    def get_cash_flow(start_iso: str, end_iso: str) -> dict:
        """Cash inflow/outflow summary for a date range."""
        from datetime import date
        from crm.modules.accounting import reports
        permissions.require(context.current_user(), 'accounting', 'view')
        return serialize.deep(reports.cash_flow(date.fromisoformat(start_iso), date.fromisoformat(end_iso)))

    @helpers.sync_tool(mcp)
    def get_balance_sheet(as_of_iso: str) -> dict:
        """Assets/liabilities/equity snapshot as of a date."""
        from datetime import date
        from crm.modules.accounting import reports
        permissions.require(context.current_user(), 'accounting', 'view')
        return serialize.deep(reports.balance_sheet(date.fromisoformat(as_of_iso)))

    @helpers.sync_tool(mcp)
    def list_bank_accounts() -> list[dict]:
        """List the company's bank/cash/gateway accounts and their current balance."""
        from crm.core import posting
        from crm.models import BankAccount
        permissions.require(context.current_user(), 'banking', 'view')
        out = []
        for b in BankAccount.objects.filter(is_active=True):
            out.append({'id': b.pk, 'name': b.name, 'kind': b.kind,
                       'balance': str(posting.balance(b.ledger_account)) if b.ledger_account_id else None})
        return out
