"""Client + service-catalogue + vendor tools, wrapping the save_client/save_service/
save_vendor functions extracted from the web views (crm.modules.clients.services,
crm.modules.expenses.services) so both code paths share one validated-save."""
from __future__ import annotations

from crm.core import permissions
from crm.mcp import context, helpers, serialize
from crm.modules.clients import services as client_svc
from crm.modules.clients.forms import ClientForm
from crm.modules.expenses import services as expense_svc
from crm.modules.expenses.forms import VendorForm

CLIENT_FIELDS = ('legal_name', 'display_name', 'client_type', 'phone', 'email', 'billing_city', 'billing_state',
                 'owner', 'status', 'customer_type', 'created_at')
SERVICE_FIELDS = ('name', 'code', 'category', 'default_price', 'billing_model', 'is_active')
VENDOR_FIELDS = ('name', 'state', 'status', 'created_at')


def register(mcp):
    @helpers.sync_tool(mcp)
    def list_clients(limit: int = 50) -> list[dict]:
        """List clients, company-wide."""
        from crm.models import Client
        user = context.current_user()
        qs = permissions.scoped(Client.objects.filter(archived_at__isnull=True), user, 'clients', 'view')
        return serialize.rows(qs.order_by('-id'), CLIENT_FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def get_client(client_id: int) -> dict:
        """Fetch one client by id, including the financial summary (billed/outstanding/collected)."""
        from crm.models import Client
        user = context.current_user()
        client = helpers.get_scoped(user, Client, client_id, 'clients', 'view')
        out = serialize.row(client, CLIENT_FIELDS)
        out['financial_summary'] = {k: str(v) if hasattr(v, 'quantize') else v
                                    for k, v in client_svc.financial_summary(client).items() if k != 'ageing'}
        return out

    @helpers.sync_tool(mcp)
    def create_client(legal_name: str, phone: str = '', email: str = '', billing_city: str = '',
                      billing_state: str = '', client_type: str = 'company') -> dict:
        """Create a client. Raises if a likely duplicate exists (same name/GSTIN/PAN/phone/email) -
        in that case re-check with list_clients before retrying."""
        user = context.current_user()
        permissions.require(user, 'clients', 'create')
        form = ClientForm(data={'legal_name': legal_name, 'phone': phone, 'email': email,
                                'billing_city': billing_city, 'billing_state': billing_state,
                                'client_type': client_type, 'customer_type': 'unregistered',
                                'billing_country': 'India', 'payment_terms_days': 30, 'owner': user.pk,
                                'shipping_same': True},
                          user=user, can_see_tax=False)
        if not form.is_valid():
            raise ValueError('; '.join(f'{k}: {v}' for k, v in form.errors.items()))
        try:
            client = client_svc.save_client(form, user)
        except client_svc.DuplicateClientError as exc:
            raise ValueError('Possible duplicate client(s): ' + ', '.join(str(d) for d in exc.dupes))
        return serialize.row(client, CLIENT_FIELDS)

    @helpers.sync_tool(mcp)
    def list_services() -> list[dict]:
        """List the service catalogue (what the business sells)."""
        from crm.models import Service
        permissions.require(context.current_user(), 'services', 'view')
        return serialize.rows(Service.objects.filter(is_active=True).order_by('category', 'name'), SERVICE_FIELDS,
                              limit=200)

    @helpers.sync_tool(mcp)
    def list_vendors(limit: int = 50) -> list[dict]:
        """List vendors (who the business buys from)."""
        from crm.models import Vendor
        user = context.current_user()
        qs = permissions.scoped(Vendor.objects.all(), user, 'vendors', 'view')
        return serialize.rows(qs.order_by('name'), VENDOR_FIELDS, limit=limit)

    @helpers.sync_tool(mcp)
    def create_vendor(name: str, state: str = '', email: str = '', phone: str = '') -> dict:
        """Create a vendor. Raises if a likely duplicate exists (same name/PAN/GSTIN)."""
        user = context.current_user()
        permissions.require(user, 'vendors', 'create')
        form = VendorForm(data={'name': name, 'state': state, 'email': email, 'phone': phone}, show_bank=False)
        if not form.is_valid():
            raise ValueError('; '.join(f'{k}: {v}' for k, v in form.errors.items()))
        try:
            vendor = expense_svc.save_vendor(form, user)
        except expense_svc.DuplicateVendorError as exc:
            raise ValueError('Possible duplicate vendor(s): ' + ', '.join(str(d) for d in exc.dupes))
        return serialize.row(vendor, VENDOR_FIELDS)
