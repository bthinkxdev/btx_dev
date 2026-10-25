"""The "know the whole business" tool."""
from __future__ import annotations

from crm.core import permissions
from crm.mcp import context, helpers, serialize


def register(mcp):
    @helpers.sync_tool(mcp)
    def business_overview(start_iso: str = '', end_iso: str = '') -> dict:
        """A consolidated snapshot of the business: open pipeline value, profit & loss, cash
        flow, cash position, MRR, receivables ageing, and delivery health (active projects,
        overdue tickets/invoices). Defaults to month-to-date if no dates are given."""
        from datetime import date

        from crm.modules.reports.overview import business_overview as compute
        user = context.current_user()
        for module in ('leads', 'accounting', 'projects', 'tickets', 'invoices'):
            permissions.require(user, module, 'view')
        start = date.fromisoformat(start_iso) if start_iso else None
        end = date.fromisoformat(end_iso) if end_iso else None
        return serialize.deep(compute(start, end))
