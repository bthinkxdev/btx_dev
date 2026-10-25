"""Quotation tools. Lines are created directly on the model (mirroring the pattern already
used by crm/tests/base.py's make_quotation() and crm/management/commands/crm_demo_data.py),
then run through the real qsvc.after_save/recalculate so GST/tax computation is the same
code path the web builder uses - deliberately not the inline formset logic in
crm.modules.quotations.views._builder, which stays untouched (see plan notes)."""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError

from crm.core import permissions
from crm.mcp import context, helpers, serialize
from crm.modules.quotations import services as svc

FIELDS = ('number', 'title', 'client', 'lead', 'owner', 'status', 'quotation_date', 'valid_until', 'grand_total',
          'created_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_quotations(status: str = '', limit: int = 50) -> list[dict]:
        """List quotations, optionally filtered by status (draft/sent/viewed/accepted/rejected/
        converted/void/expired)."""
        from crm.models import Quotation
        user = context.current_user()
        qs = permissions.scoped(Quotation.objects.all(), user, 'quotations', 'view')
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('-id'), FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def get_quotation(quotation_id: int) -> dict:
        """Fetch one quotation with its line items."""
        from crm.models import Quotation
        user = context.current_user()
        q = helpers.get_scoped(user, Quotation, quotation_id, 'quotations', 'view')
        out = serialize.row(q, FIELDS)
        out['lines'] = serialize.rows(q.lines.all(), ('service', 'description', 'quantity', 'unit_price', 'tax_rate',
                                                       'total'))
        return out

    @helpers.sync_tool(mcp)
    def create_quotation(client_id: int, lines: list[dict], title: str = '') -> dict:
        """Create a draft quotation for a client. `lines` is a list of
        {"service_id": int, "description": str, "quantity": "1", "unit_price": "10000", "tax_rate": "18"}
        (service_id optional, the rest default sensibly if omitted)."""
        from crm.models import Client, Quotation, QuotationLine, Service
        user = context.current_user()
        permissions.require(user, 'quotations', 'create')
        client = helpers.get_scoped(user, Client, client_id, 'clients', 'view')
        if not lines:
            raise ValueError('Provide at least one line item.')
        q = svc.prefill(client=client, owner=user)
        q.title = title
        q.save()
        for ln in lines:
            service = Service.objects.get(pk=ln['service_id']) if ln.get('service_id') else None
            QuotationLine.objects.create(
                quotation=q, service=service, description=ln.get('description') or (service.name if service else ''),
                sac_code=ln.get('sac_code') or (service.sac_code if service else ''),
                quantity=Decimal(str(ln.get('quantity', '1'))), unit_price=Decimal(str(ln['unit_price'])),
                tax_rate=Decimal(str(ln.get('tax_rate', '18'))))
        svc.after_save(q, user, created=True)
        svc.recalculate(q)
        q.refresh_from_db()
        return serialize.row(q, FIELDS)

    @helpers.sync_tool(mcp)
    def send_quotation(quotation_id: int) -> dict:
        """Mark a draft quotation as sent to the customer (records it as shared, does not
        actually email - use for quotations shared outside the system)."""
        from crm.models import Quotation
        user = context.current_user()
        q = helpers.get_scoped(user, Quotation, quotation_id, 'quotations', 'edit')
        try:
            svc.send(q, user, channel='link')
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        q.refresh_from_db()
        return serialize.row(q, FIELDS)

    @helpers.sync_tool(mcp)
    def accept_quotation(quotation_id: int, accepted_by: str, evidence: str = 'email', note: str = '') -> dict:
        """Record customer acceptance of a sent quotation. evidence: email/signed_pdf/verbal/purchase_order
        (verbal needs a note of 10+ characters)."""
        from crm.models import Quotation
        user = context.current_user()
        q = helpers.get_scoped(user, Quotation, quotation_id, 'quotations', 'edit')
        try:
            svc.accept(q, user, accepted_by=accepted_by, evidence=evidence, note=note)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        q.refresh_from_db()
        return serialize.row(q, FIELDS)
