"""Payment tools. record_manual only - no verify_manual tool: verifying a payment requires
the 'payments':'approve' permission, which the Business Manager role never holds (maker-checker),
so even if a tool existed here the call would be denied by crm.core.permissions itself."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from crm.core import india, permissions
from crm.mcp import context, helpers, serialize
from crm.modules.payments import services as svc

FIELDS = ('client', 'invoice', 'method', 'amount', 'status', 'payment_date', 'reference_number', 'created_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_payments(status: str = '', limit: int = 50) -> list[dict]:
        """List recorded payments, optionally filtered by status (pending/verified/rejected/
        partially_refunded/refunded)."""
        from crm.models import Payment
        user = context.current_user()
        qs = permissions.scoped(Payment.objects.all(), user, 'payments', 'view')
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('-id'), FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def record_payment(client_id: int, amount: str, method: str, payment_date_iso: str = '',
                       reference_number: str = '', invoice_id: int = 0, note: str = '') -> dict:
        """Record a manual payment received from a client (method: bank_transfer/upi/cheque/cash).
        Stays 'pending' until a Finance user verifies it - the Business Manager role cannot verify
        its own entries. Payments above the company's proof threshold need an attached proof, which
        this tool cannot provide - keep large payments to a smaller recorded amount or record them
        through the web app."""
        from crm.models import Client, Invoice
        user = context.current_user()
        permissions.require(user, 'payments', 'create')
        client = helpers.get_scoped(user, Client, client_id, 'clients', 'view')
        invoice = helpers.get_scoped(user, Invoice, invoice_id, 'invoices', 'view') if invoice_id else None
        try:
            p = svc.record_manual(user, client=client, invoice=invoice, amount=Decimal(amount),
                                  payment_date=india.today() if not payment_date_iso else
                                  __import__('datetime').date.fromisoformat(payment_date_iso),
                                  method=method, reference_number=reference_number, note=note)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        return serialize.row(p, FIELDS)
