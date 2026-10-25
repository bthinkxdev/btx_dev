"""Expense tools."""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError

from crm.core import india, permissions
from crm.mcp import context, helpers, serialize
from crm.modules.expenses import services as svc

FIELDS = ('kind', 'vendor', 'category', 'description', 'bill_date', 'amount_before_tax', 'status', 'created_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_expenses(status: str = '', limit: int = 50) -> list[dict]:
        """List expenses, optionally filtered by status (draft/submitted/approved/partially_paid/
        paid/rejected/reversed)."""
        from crm.models import Expense
        user = context.current_user()
        qs = permissions.scoped(Expense.objects.all(), user, 'expenses', 'view')
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('-id'), FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def list_expense_categories() -> list[dict]:
        """List valid expense category names for create_expense()."""
        from crm.models import ExpenseCategory
        return serialize.rows(ExpenseCategory.objects.filter(is_active=True), ('name', 'tds_section'), limit=100)

    @helpers.sync_tool(mcp)
    def create_expense(vendor_id: int, category_name: str, description: str, amount_before_tax: str,
                       bill_date_iso: str = '', bill_number: str = '') -> dict:
        """Create a vendor-bill expense (draft). Call submit_expense() afterwards to send it for approval."""
        from crm.models import Expense, ExpenseCategory, Vendor
        user = context.current_user()
        permissions.require(user, 'expenses', 'create')
        vendor = helpers.get_scoped(user, Vendor, vendor_id, 'vendors', 'view')
        category = ExpenseCategory.objects.get(name=category_name)
        e = Expense(kind='vendor_bill', vendor=vendor, category=category, description=description,
                   bill_date=date_or_today(bill_date_iso), bill_number=bill_number,
                   amount_before_tax=Decimal(amount_before_tax), cgst=Decimal('0'), sgst=Decimal('0'),
                   itc_eligibility='na', created_by=user)
        svc.recompute(e)
        e.save()
        return serialize.row(e, FIELDS)

    @helpers.sync_tool(mcp)
    def submit_expense(expense_id: int, message: str = '') -> dict:
        """Submit a draft expense for approval."""
        from crm.models import Expense
        user = context.current_user()
        e = helpers.get_scoped(user, Expense, expense_id, 'expenses', 'edit')
        try:
            svc.submit(e, user, message)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        e.refresh_from_db()
        return serialize.row(e, FIELDS)


def date_or_today(iso: str):
    from datetime import date
    return date.fromisoformat(iso) if iso else india.today()
