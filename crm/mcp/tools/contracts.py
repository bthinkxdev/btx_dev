"""Contract tools. activate()/renew()/amend() are not approve-gated in the service layer,
so they're safe to expose at the role's plain create/edit grant."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError

from crm.core import india, permissions
from crm.mcp import context, helpers, serialize
from crm.modules.contracts import services as svc

FIELDS = ('number', 'client', 'title', 'owner', 'status', 'start_date', 'end_date', 'created_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_contracts(status: str = '', limit: int = 50) -> list[dict]:
        """List contracts, optionally filtered by status (draft/pending_approval/active/
        suspended/terminated/completed/superseded)."""
        from crm.models import Contract
        user = context.current_user()
        qs = permissions.scoped(Contract.objects.all(), user, 'contracts', 'view')
        if status:
            qs = qs.filter(status=status)
        return serialize.rows(qs.order_by('-id'), FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def get_contract(contract_id: int) -> dict:
        """Fetch one contract with its line items."""
        from crm.models import Contract
        user = context.current_user()
        c = helpers.get_scoped(user, Contract, contract_id, 'contracts', 'view')
        out = serialize.row(c, FIELDS)
        out['lines'] = serialize.rows(c.lines.all(), ('service', 'description', 'unit_price', 'billing_model',
                                                       'start_date'))
        return out

    @helpers.sync_tool(mcp)
    def create_contract(client_id: int, title: str, lines: list[dict], end_date_iso: str = '') -> dict:
        """Create a draft contract (retainer/AMC) for a client. `lines` is a list of
        {"service_id": int (optional), "description": str, "unit_price": "12000",
        "billing_model": "monthly"}."""
        from crm.models import Client, ContractLine, Service
        user = context.current_user()
        permissions.require(user, 'contracts', 'create')
        client = helpers.get_scoped(user, Client, client_id, 'clients', 'view')
        if not lines:
            raise ValueError('Provide at least one line item.')
        c = svc.prefill(client=client, owner=user)
        c.title = title
        if end_date_iso:
            c.end_date = date.fromisoformat(end_date_iso)
        c.save()
        for ln in lines:
            service = Service.objects.get(pk=ln['service_id']) if ln.get('service_id') else None
            ContractLine.objects.create(
                contract=c, service=service, description=ln.get('description') or (service.name if service else ''),
                unit_price=Decimal(str(ln['unit_price'])), billing_model=ln.get('billing_model', 'monthly'),
                start_date=india.today(), sac_code=ln.get('sac_code') or (service.sac_code if service else ''))
        return serialize.row(c, FIELDS)

    @helpers.sync_tool(mcp)
    def activate_contract(contract_id: int) -> dict:
        """Activate a draft contract, generating its billing schedule."""
        from crm.models import Contract
        user = context.current_user()
        c = helpers.get_scoped(user, Contract, contract_id, 'contracts', 'edit')
        try:
            c = svc.activate(c, user)
        except ValidationError as exc:
            raise ValueError(' '.join(exc.messages))
        return serialize.row(c, FIELDS)
