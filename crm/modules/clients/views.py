"""Client directory, Client 360 profile, contacts (Part 9.1-9.2) and service catalogue (Part 9.3)."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from crm.core import audit, permissions
from crm.core.views import Filter, ListPage, form_error_text, get_scoped, staff_required
from crm.models import (AuditLog, Attachment, Client, Contact, LeadActivity, Service)
from crm.models.clients import CLIENT_STATUSES

from . import services as svc
from .forms import ClientForm, ContactForm, ServiceForm, TaxRuleForm


def _owner_choices(request):
    from crm.modules.leads.forms import assignable_users
    return [(u.pk, u.get_full_name() or u.username) for u in assignable_users(request.user)]


class ClientList(ListPage):
    model = Client
    module = 'clients'
    template_name = 'crm/clients/list.html'
    title = 'Clients'
    search_fields = ('display_name__icontains', 'legal_name__icontains', 'gstin__iexact', 'email__icontains', 'phone__contains')
    select_related = ('owner',)
    default_sort = 'display_name'
    sort_fields = {'name': 'display_name', 'created': 'created_at'}
    filters = [
        Filter('status', 'Status', CLIENT_STATUSES),
        Filter('owner', 'Owner', _owner_choices, lookup='owner_id'),
        Filter('service', 'Service', lambda r: Service.objects.values_list('pk', 'name'),
               apply=lambda qs, v, r: qs.filter(Q(contracts__lines__service_id=int(v)) | Q(invoices__lines__service_id=int(v))).distinct()),
    ]
    quick_filters = {
        'outstanding': ('Has outstanding', lambda qs, r: qs.filter(invoices__status__in=['issued', 'partially_paid'], invoices__balance__gt=0).distinct()),
        'renewal': ('Renewal due', lambda qs, r: qs.filter(contracts__status='active', contracts__end_date__lte=_soon(60)).distinct()),
        'restricted': ('Restricted or on hold', lambda qs, r: qs.filter(status__in=['restricted', 'on_hold'])),
        'archived': ('Archived', lambda qs, r: qs.filter(archived_at__isnull=False)),
    }
    export_name = 'clients'
    export_columns = [('Name', 'display_name'), ('Legal name', 'legal_name'), ('GSTIN', 'gstin'), ('Phone', 'phone'),
                      ('Email', 'email'), ('City', 'billing_city'), ('State', 'billing_state'), ('Owner', 'owner'),
                      ('Status', lambda c: c.get_status_display()), ('Created', 'created_at')]

    def base_queryset(self):
        qs = super().base_queryset()
        if self.request.GET.get('quick') != 'archived':
            qs = qs.filter(archived_at__isnull=True)
        return qs.annotate(outstanding=Sum('invoices__balance', filter=Q(invoices__status__in=['issued', 'partially_paid'])))

    def extra_context(self, qs):
        return {'module_code': 'clients', 'can_create': permissions.has_perm(self.request.user, 'clients', 'create'),
                'show_tax': permissions.can_see(self.request.user, 'tax_ids'),
                'search_placeholder': 'Search name, GSTIN, phone, email'}


def _soon(days):
    from datetime import timedelta
    from crm.core.india import today
    return today() + timedelta(days=days)


@staff_required
def client_form(request, pk=None):
    client = get_scoped(request, Client, pk, 'clients', 'edit') if pk else None
    if client is None:
        permissions.require(request.user, 'clients', 'create', request)
    can_tax = permissions.can_see(request.user, 'tax_ids')
    form = ClientForm(request.POST or None, instance=client, user=request.user, can_see_tax=can_tax)
    dupes = []
    if request.method == 'POST' and form.is_valid():
        try:
            obj = svc.save_client(form, request.user, confirm_duplicate=bool(request.POST.get('confirm_duplicate')))
            messages.success(request, f'Client {obj} saved.')
            return redirect('crm:client_detail', pk=obj.pk)
        except svc.DuplicateClientError as exc:
            dupes = exc.dupes
            messages.warning(request, 'Possible duplicate client found. Open it, or tick "This is a different client" to save.')
        except svc.ClientError as exc:
            form.add_error('gstin', str(exc.message if hasattr(exc, 'message') else exc))
    return render(request, 'crm/clients/form.html', {'form': form, 'client': client, 'dupes': dupes})


TABS = ['overview', 'deals', 'quotations', 'projects', 'contracts', 'invoices', 'payments', 'profitability', 'tickets',
        'documents', 'communication', 'activity']


@staff_required
def client_detail(request, pk):
    client = get_scoped(request, Client, pk, 'clients', 'view', queryset=Client.objects.select_related('owner'))
    if client.merged_into_id:
        messages.info(request, 'This client was merged. Showing the surviving record.')
        return redirect('crm:client_detail', pk=client.merged_into_id)
    tab = request.GET.get('tab', 'overview')
    tab = tab if tab in TABS else 'overview'
    u = request.user
    ctx = {'client': client, 'tab': tab, 'tabs': TABS, 'summary': svc.financial_summary(client),
           'contacts': client.contacts.filter(archived_at__isnull=True), 'contact_form': ContactForm(prefix='contact'),
           'can_edit': permissions.in_scope(u, 'clients', 'edit', client),
           'can_archive': permissions.has_perm(u, 'clients', 'archive'),
           'can_merge': permissions.has_perm(u, 'clients', 'approve'),
           'show_tax': permissions.can_see(u, 'tax_ids'), 'show_notes': permissions.can_see(u, 'internal_notes'),
           'archive_blockers': svc.archive_blockers(client),
           'next_renewal': client.contracts.filter(status='active', end_date__isnull=False).order_by('end_date').first()}
    counts = {
        'deals': client.leads.count(), 'quotations': client.quotations.count(), 'projects': client.projects.count(),
        'contracts': client.contracts.count(), 'invoices': client.invoices.count(), 'payments': client.payments.count(),
        'tickets': client.tickets.count(),
    }
    ctx['counts'] = counts
    if tab == 'overview':
        ctx['recent_invoices'] = client.invoices.order_by('-invoice_date')[:5]
        ctx['open_quotes'] = client.quotations.filter(status__in=['draft', 'awaiting_approval', 'approved', 'sent', 'viewed'])[:5]
    elif tab == 'deals':
        ctx['rows'] = client.leads.select_related('owner').order_by('-id')
    elif tab == 'quotations':
        ctx['rows'] = client.quotations.select_related('owner').order_by('-id')
    elif tab == 'projects':
        ctx['rows'] = client.projects.select_related('manager').annotate(open_tasks=Count('tasks', filter=~Q(tasks__status__in=['done', 'cancelled'])))
    elif tab == 'contracts':
        ctx['rows'] = client.contracts.order_by('-id')
    elif tab == 'invoices':
        ctx['rows'] = client.invoices.order_by('-invoice_date')
    elif tab == 'payments':
        ctx['rows'] = client.payments.order_by('-payment_date')
        ctx['links'] = client.payment_links.order_by('-id')
    elif tab == 'profitability':
        from crm.modules.reports.services import client_profitability
        ctx['profit'] = client_profitability(client)
    elif tab == 'tickets':
        ctx['rows'] = client.tickets.select_related('assignee').order_by('-id')
    elif tab == 'documents':
        ct = ContentType.objects.get_for_model(Client)
        ctx['attachments'] = Attachment.objects.filter(content_type=ct, object_id=str(client.pk), archived_at__isnull=True)
    elif tab == 'communication':
        ctx['rows'] = LeadActivity.objects.filter(lead__client=client).exclude(kind__in=['system', 'assignment']).select_related('actor', 'lead')[:200]
        from crm.models import DeliveryLog
        ctx['emails'] = DeliveryLog.objects.filter(recipient__in=[c.email for c in ctx['contacts'] if c.email] + ([client.email] if client.email else []))[:50]
    elif tab == 'activity':
        ctx['rows'] = AuditLog.objects.filter(object_type='client', object_id=str(client.pk))[:200]
    return render(request, 'crm/clients/detail.html', ctx)


@staff_required
@require_POST
def contact_save(request, pk, contact_id=None):
    client = get_scoped(request, Client, pk, 'clients', 'edit')
    contact = get_object_or_404(Contact, pk=contact_id, client=client) if contact_id else None
    form = ContactForm(request.POST, instance=contact, prefix='contact')
    if form.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            obj.client = client
            obj.created_by = obj.created_by or request.user
            obj.save()
            if obj.is_primary:
                client.contacts.exclude(pk=obj.pk).update(is_primary=False)
            audit.log('client.contact_saved', client, changes={'contact': obj.name})
        messages.success(request, f'Contact {obj.name} saved.')
    else:
        messages.error(request, form_error_text(form))
    return redirect(reverse('crm:client_detail', args=[client.pk]) + '?tab=overview')


@staff_required
@require_POST
def contact_action(request, pk, contact_id, action):
    client = get_scoped(request, Client, pk, 'clients', 'edit')
    contact = get_object_or_404(Contact, pk=contact_id, client=client)
    if action == 'primary':
        client.contacts.update(is_primary=False)
        contact.is_primary = True
        contact.save(update_fields=['is_primary'])
        messages.success(request, f'{contact.name} is now the primary contact.')
    elif action == 'archive':
        from django.utils import timezone
        contact.archived_at = timezone.now()
        contact.archived_by = request.user
        contact.save(update_fields=['archived_at', 'archived_by'])
        messages.success(request, f'{contact.name} removed.')
    audit.log(f'client.contact_{action}', client, changes={'contact': contact.name})
    return redirect('crm:client_detail', pk=client.pk)


@staff_required
@require_POST
def client_status(request, pk):
    client = get_scoped(request, Client, pk, 'clients', 'edit')
    try:
        svc.set_status(client, request.user, request.POST.get('status', ''), request.POST.get('reason', ''))
        messages.success(request, 'Client status updated.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:client_detail', pk=client.pk)


@staff_required
@require_POST
def client_archive(request, pk):
    client = get_scoped(request, Client, pk, 'clients', 'view')
    permissions.require(request.user, 'clients', 'archive', request)
    try:
        svc.archive(client, request.user, request.POST.get('reason', ''))
        messages.success(request, f'{client} archived.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:client_detail', pk=client.pk)


@staff_required
def client_merge(request, pk):
    permissions.require(request.user, 'clients', 'approve', request)
    client = get_scoped(request, Client, pk, 'clients', 'view')
    candidates = svc.possible_duplicates(legal_name=client.legal_name, gstin=client.gstin, pan=client.pan,
                                         phone=client.phone, email=client.email, exclude_id=client.pk)
    if request.method == 'POST':
        other = get_object_or_404(Client, pk=request.POST.get('other'), archived_at__isnull=True)
        try:
            svc.merge(client, other, request.user)
            messages.success(request, f'Merged {other} into {client}. All records moved; the old record redirects here.')
            return redirect('crm:client_detail', pk=client.pk)
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
    q = request.GET.get('q', '').strip()
    search = Client.objects.filter(archived_at__isnull=True, display_name__icontains=q).exclude(pk=client.pk)[:20] if q else []
    return render(request, 'crm/clients/merge.html', {'client': client, 'candidates': candidates, 'search': search, 'q': q})


# ---------------------------------------------------------------- service catalogue (9.3)

class ServiceList(ListPage):
    model = Service
    module = 'services'
    template_name = 'crm/clients/service_list.html'
    title = 'Service catalogue'
    search_fields = ('name__icontains', 'category__icontains', 'sac_code__icontains')
    default_sort = 'category'
    filters = [Filter('active', 'Status', [('1', 'Active'), ('0', 'Inactive')], apply=lambda qs, v, r: qs.filter(is_active=v == '1'))]
    export_name = 'services'
    export_columns = [('Name', 'name'), ('Code', 'code'), ('Category', 'category'), ('SAC/HSN', 'sac_code'),
                      ('Tax rate', 'tax_rate'), ('Price', 'default_price'), ('Billing', lambda s: s.get_billing_model_display())]

    def base_queryset(self):
        permissions.require(self.request.user, 'services', 'view', self.request)
        return Service.objects.all()

    def extra_context(self, qs):
        return {'module_code': 'services', 'can_configure': permissions.has_perm(self.request.user, 'services', 'configure'),
                'show_cost': permissions.can_see(self.request.user, 'cost_price')}


@staff_required
def service_form(request, pk=None):
    permissions.require(request.user, 'services', 'configure', request)
    obj = get_object_or_404(Service, pk=pk) if pk else None
    form = ServiceForm(request.POST or None, instance=obj, can_see_cost=permissions.can_see(request.user, 'cost_price'))
    rule_form = TaxRuleForm(prefix='rule')
    if request.method == 'POST' and form.is_valid():
        s = svc.save_service(form, request.user)
        messages.success(request, f'Service {s} saved.')
        return redirect('crm:service_edit', pk=s.pk)
    return render(request, 'crm/clients/service_form.html', {'form': form, 'obj': obj, 'rule_form': rule_form,
                                                              'rules': obj.tax_rules.all() if obj else []})


@staff_required
@require_POST
def service_tax_rule(request, pk):
    permissions.require(request.user, 'services', 'configure', request)
    service = get_object_or_404(Service, pk=pk)
    form = TaxRuleForm(request.POST, prefix='rule')
    if form.is_valid():
        rule = form.save(commit=False)
        rule.service = service
        overlap = service.tax_rules.filter(effective_from__lte=rule.effective_to or '9999-12-31').filter(
            Q(effective_to__isnull=True) | Q(effective_to__gte=rule.effective_from))
        if overlap.exists():
            messages.error(request, 'This period overlaps an existing rule. End the current rule first (set its end date).')
        else:
            rule.save()
            audit.log('tax.rule_added', service, changes={'rate': str(rule.rate), 'from': rule.effective_from,
                                                          'to': rule.effective_to}, reason=rule.reason)
            messages.success(request, 'Effective-dated tax rule added.')
    else:
        messages.error(request, form_error_text(form))
    return redirect('crm:service_edit', pk=service.pk)


@staff_required
@require_POST
def service_tax_rule_end(request, pk, rule_id):
    permissions.require(request.user, 'services', 'configure', request)
    from datetime import date
    service = get_object_or_404(Service, pk=pk)
    rule = get_object_or_404(service.tax_rules, pk=rule_id)
    try:
        end = date.fromisoformat(request.POST.get('end', ''))
    except ValueError:
        messages.error(request, 'Choose a valid end date.')
        return redirect('crm:service_edit', pk=service.pk)
    if end < rule.effective_from:
        messages.error(request, 'The end date cannot be before the start date.')
    else:
        rule.effective_to = end
        rule.save(update_fields=['effective_to'])
        audit.log('tax.rule_retired', service, changes={'rule': rule.pk, 'to': end}, reason=request.POST.get('reason', ''))
        messages.success(request, 'Rule end date set.')
    return redirect('crm:service_edit', pk=service.pk)
