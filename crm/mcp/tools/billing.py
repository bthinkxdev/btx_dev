"""Invoice tools - same direct-line-creation pattern as quotations.py."""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError

from crm.core import permissions
from crm.mcp import context, helpers, serialize
from crm.modules.billing import services as svc

FIELDS = ('number', 'client', 'owner', 'status', 'invoice_date', 'due_date', 'grand_total', 'balance', 'created_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_invoices(status: str = '', limit: int = 50) -> list[dict]:
        """List invoices, optionally filtered by status (draft/issued/partially_paid/paid/
        credit_noted/cancelled/written_off)."""
        from crm.models import Invoice
        user = context.current_user()
        qs = permissions.scoped(Invoice.objects.all(), user, 'invoices', 'view')
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('-id'), FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def get_invoice(invoice_id: int) -> dict:
        """Fetch one invoice with its line items."""
        from crm.models import Invoice
        user = context.current_user()
        inv = helpers.get_scoped(user, Invoice, invoice_id, 'invoices', 'view')
        out = serialize.row(inv, FIELDS)
        out['lines'] = serialize.rows(inv.lines.all(), ('service', 'description', 'quantity', 'unit_price',
                                                         'tax_rate', 'total'))
        return out

    @helpers.sync_tool(mcp)
    def create_invoice(client_id: int, lines: list[dict], quotation_id: int = 0) -> dict:
        """Create a draft invoice for a client (optionally linked to an accepted quotation).
        `lines` same shape as create_quotation's."""
        from crm.models import Client, Invoice, InvoiceLine, Quotation, Service
        user = context.current_user()
        permissions.require(user, 'invoices', 'create')
        client = helpers.get_scoped(user, Client, client_id, 'clients', 'view')
        quotation = helpers.get_scoped(user, Quotation, quotation_id, 'quotations', 'view') if quotation_id else None
        if not lines:
            raise ValueError('Provide at least one line item.')
        inv = svc.prefill(client=client, owner=user, quotation=quotation)
        inv.save()
        for ln in lines:
            service = Service.objects.get(pk=ln['service_id']) if ln.get('service_id') else None
            InvoiceLine.objects.create(
                invoice=inv, service=service, description=ln.get('description') or (service.name if service else ''),
                sac_code=ln.get('sac_code') or (service.sac_code if service else ''),
                quantity=Decimal(str(ln.get('quantity', '1'))), unit_price=Decimal(str(ln['unit_price'])),
                tax_rate=Decimal(str(ln.get('tax_rate', '18'))))
        svc.recalculate(inv)
        inv.refresh_from_db()
        return serialize.row(inv, FIELDS)

    @helpers.sync_tool(mcp)
    def issue_invoice(invoice_id: int) -> dict:
        """Finalize a draft invoice (allocates its number, posts GST, makes it payable)."""
        from crm.models import Invoice
        user = context.current_user()
        inv = helpers.get_scoped(user, Invoice, invoice_id, 'invoices', 'edit')
        try:
            inv = svc.issue(inv, user)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        return serialize.row(inv, FIELDS)
