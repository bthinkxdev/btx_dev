"""Quotation pages (Part 5.5-5.7): list, builder, detail with popups, PDF, compare, public share page."""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import approvals, audit, gst, india, pdf, permissions, ratelimit
from crm.core.middleware import client_ip
from crm.core.money import D
from crm.core.views import Filter, ListPage, get_scoped, staff_required
from crm.models import Client, CompanySettings, Lead, Quotation, QuotationLine, Service
from crm.models.quotations import QUOTATION_STATUSES

from . import services as svc
from .forms import (AcceptForm, ApprovalRequestForm, GstOverrideForm, LineFormSet, MilestoneFormSet, QuotationForm,
                    RejectForm, SendForm)


class QuotationList(ListPage):
    model = Quotation
    module = 'quotations'
    template_name = 'crm/quotations/list.html'
    title = 'Quotations'
    search_fields = ('number__icontains', 'title__icontains', 'customer_name__icontains', 'client__display_name__icontains',
                     'lead__name__icontains', 'lead__company__icontains')
    select_related = ('client', 'lead', 'owner')
    default_sort = '-id'
    sort_fields = {'date': 'quotation_date', 'total': 'grand_total', 'valid': 'valid_until', 'sent': 'sent_at'}
    filters = [Filter('status', 'Status', QUOTATION_STATUSES)]
    quick_filters = {
        'mine': ('Mine', lambda qs, r: qs.filter(owner=r.user)),
        'drafts': ('Drafts', lambda qs, r: qs.filter(status='draft')),
        'expiring': ('Expiring in 7 days', lambda qs, r: qs.filter(status__in=['sent', 'viewed', 'approved'],
                                                                 valid_until__lte=india.today() + timedelta(days=7),
                                                                 valid_until__gte=india.today())),
        'viewed': ('Viewed, not accepted', lambda qs, r: qs.filter(status='viewed')),
        'awaiting_me': ('Awaiting my approval', lambda qs, r: qs.filter(pk__in=_awaiting_ids(r.user))),
        'accepted': ('Accepted', lambda qs, r: qs.filter(status='accepted')),
    }
    export_name = 'quotations'
    export_columns = [('Number', 'display_number'), ('Customer', 'customer_display'), ('Date', 'quotation_date'),
                      ('Valid until', 'valid_until'), ('Version', 'version'), ('Taxable', 'taxable_total'),
                      ('Tax', 'tax_total'), ('Total', 'grand_total'), ('Status', lambda q: q.get_status_display()),
                      ('Owner', 'owner'), ('Sent', 'sent_at'), ('Views', 'view_count')]

    def extra_context(self, qs):
        return {'module_code': 'quotations', 'can_create': permissions.has_perm(self.request.user, 'quotations', 'create'),
                'today': india.today(), 'soon': india.today() + timedelta(days=7),
                'search_placeholder': 'Search number, customer, title'}


def _awaiting_ids(user):
    from crm.models import ApprovalRequest
    ids = []
    for r in ApprovalRequest.objects.filter(kind='quotation', status='pending'):
        if approvals.can_decide(user, r)[0]:
            ids.append(int(r.object_id))
    return ids


def get_quotation(request, pk, action='view'):
    return get_scoped(request, Quotation, pk, 'quotations', action,
                      queryset=Quotation.objects.select_related('client', 'lead', 'owner', 'family'))


# ---------------------------------------------------------------- builder (5.6)

def _service_catalog(on_date):
    data = {}
    for s in Service.objects.filter(is_active=True):
        taxability, rate = s.tax_for(on_date)
        data[s.pk] = {'description': s.name, 'sac': s.sac_code, 'unit': s.unit, 'price': str(s.default_price),
                      'taxability': taxability, 'rate': f'{rate.normalize():f}', 'floor': str(s.floor_price)}
    return data


def _builder(request, q: Quotation, is_new: bool, initial_lines=None):
    user = request.user
    can_override = permissions.has_perm(user, 'quotations', 'override')
    can_cost = permissions.can_see(user, 'cost_price')
    company = CompanySettings.get()
    form = QuotationForm(request.POST or None, instance=q, user=user, can_see_cost=can_cost,
                         can_change_owner=permissions.has_perm(user, 'quotations', 'approve'), standard_terms=company.quotation_terms)
    on_date = q.quotation_date
    if request.method == 'GET' and initial_lines:
        from django.forms import inlineformset_factory
        from .forms import BaseLineFormSet, LineForm
        factory = inlineformset_factory(Quotation, QuotationLine, form=LineForm, formset=BaseLineFormSet,
                                        extra=len(initial_lines), can_delete=True)
        lines = factory(instance=q, prefix='lines', can_override=can_override, on_date=on_date, initial=initial_lines)
    else:
        lines = LineFormSet(request.POST or None, instance=q, prefix='lines', can_override=can_override, on_date=on_date)
    milestones = MilestoneFormSet(request.POST or None, instance=q, prefix='ms')
    if request.method == 'POST':
        if form.is_valid() and lines.is_valid() and milestones.is_valid():
            try:
                with transaction.atomic():
                    obj = form.save(commit=False)
                    if obj.pk:
                        locked = Quotation.objects.select_for_update().get(pk=obj.pk)
                        if locked.status != 'draft':
                            raise ValidationError('This quotation is no longer a draft, so it cannot be edited.')
                    else:
                        obj.created_by = user
                    obj.updated_by = user
                    obj.terms_modified = obj.terms.strip() != (company.quotation_terms or '').strip()
                    obj.save()
                    lines.instance = obj
                    milestones.instance = obj
                    for i, f in enumerate(lines.forms):
                        if f.cleaned_data and not f.cleaned_data.get('DELETE'):
                            f.instance.sort = i
                    lines.save()
                    milestones.save()
                    svc.after_save(obj, user, created=is_new)
                    audit.log('quotation.saved', obj, changes={'fields': form.changed_data})
                messages.success(request, f'Saved {obj.display_number} at {timezone.localtime():%H:%M}.')
                if request.POST.get('next') == 'detail':
                    return redirect('crm:quotation_detail', pk=obj.pk)
                return redirect('crm:quotation_edit', pk=obj.pk)
            except gst.GSTError as exc:
                form.add_error(None, str(exc))
            except ValidationError as exc:
                form.add_error(None, ' '.join(exc.messages))
        else:
            messages.error(request, 'Please fix the highlighted problems. Nothing was saved.')
    return render(request, 'crm/quotations/builder.html', {
        'form': form, 'lines': lines, 'milestones': milestones, 'q': q, 'is_new': is_new,
        'catalog': _service_catalog(on_date), 'can_override': can_override, 'can_cost': can_cost,
        'company': company, 'reasons': svc.approval_reasons(q, user)[0] if q.pk else [],
    })


@staff_required
def quotation_create(request):
    permissions.require(request.user, 'quotations', 'create', request)
    lead = client = None
    src_lead = request.GET.get('lead') or request.POST.get('src_lead')
    src_client = request.GET.get('client') or request.POST.get('src_client')
    from crm.modules.leads.views import lead_scope_extra
    if src_lead:
        lead = get_scoped(request, Lead, src_lead, 'leads', 'view', extra_q=lead_scope_extra(request.user))
        if lead.stage not in ('qualified', 'quotation_prepared', 'quotation_sent', 'negotiation'):
            messages.error(request, 'Qualify the lead first. A quotation needs service, budget, decision-maker and closing date.')
            return redirect('crm:lead_detail', pk=lead.pk)
    elif src_client:
        client = get_scoped(request, Client, src_client, 'clients', 'view')
        if client.is_restricted:
            messages.error(request, 'This client is restricted. New quotations are blocked.')
            return redirect('crm:client_detail', pk=client.pk)
    else:
        messages.info(request, 'Start a quotation from a qualified lead or from a client.')
        return redirect('crm:lead_list')
    q = svc.prefill(lead=lead, client=client, owner=(lead.owner if lead and lead.owner else request.user))
    if not permissions.has_perm(request.user, 'quotations', 'approve'):
        q.owner = request.user
    initial = []
    if lead is not None:
        for s_ in lead.services.filter(is_active=True):
            taxability, rate = s_.tax_for(q.quotation_date)
            initial.append({'service': s_.pk, 'description': s_.name, 'sac_code': s_.sac_code, 'quantity': 1,
                            'unit': s_.unit, 'unit_price': s_.default_price, 'taxability': taxability,
                            'tax_rate': f'{rate.normalize():f}'})
    return _builder(request, q, is_new=True, initial_lines=initial)


@staff_required
def quotation_edit(request, pk):
    q = get_quotation(request, pk, 'edit')
    if q.status == 'awaiting_approval':
        messages.warning(request, 'Awaiting approval. Cancel the approval request to edit; the approval restarts.')
        return redirect('crm:quotation_detail', pk=q.pk)
    if q.status != 'draft':
        messages.info(request, 'A sent or approved version cannot change. Create a new version instead.')
        return redirect('crm:quotation_detail', pk=q.pk)
    return _builder(request, q, is_new=False)


@staff_required
@require_POST
def quotation_preview_totals(request):
    """Live summary panel: server computes with the real engine (no float maths in the browser)."""
    permissions.require(request.user, 'quotations', 'create', request)
    return preview_totals(request, round_off=CompanySettings.get().round_off_quotation, module='quotations')


def preview_totals(request, *, round_off: bool, module: str):
    """Shared by the quotation and invoice builders."""
    p = request.POST
    try:
        n = int(p.get('lines-TOTAL_FORMS', 0))
    except ValueError:
        n = 0
    lines = []
    for i in range(min(n, 200)):
        if p.get(f'lines-{i}-DELETE'):
            continue
        qty, price = p.get(f'lines-{i}-quantity', ''), p.get(f'lines-{i}-unit_price', '')
        if not qty or not price:
            continue
        try:
            svc_id = p.get(f'lines-{i}-service')
            taxability, rate = p.get(f'lines-{i}-taxability', 'taxable'), D(p.get(f'lines-{i}-tax_rate', '18'))
            if svc_id and not permissions.has_perm(request.user, module, 'override'):
                s = Service.objects.filter(pk=svc_id).first()
                if s:
                    taxability, rate = s.tax_for(india.today())
            lines.append(gst.LineInput(quantity=D(qty), unit_price=D(price), tax_rate=rate, taxability=taxability,
                                       discount_mode=p.get(f'lines-{i}-discount_mode', ''),
                                       discount_value=D(p.get(f'lines-{i}-discount_value') or 0), key=str(i)))
        except ValueError:
            return JsonResponse({'ok': False, 'error': f'Line {i + 1}: enter numbers only.'})
    company = CompanySettings.get()
    ctx = gst.TaxContext(
        supplier_state=company.state_code, place_of_supply=p.get('place_of_supply') or p.get('customer_state') or
        ('96' if p.get('customer_type') == 'overseas' else ''), customer_type=p.get('customer_type', 'unregistered'),
        price_mode=p.get('price_mode', 'exclusive'), supplier_registered=company.gst_registered, lut=company.lut_enabled,
        round_off=round_off, discount_mode=p.get('discount_mode', ''),
        discount_value=D(p.get('discount_value') or 0))
    try:
        r = gst.compute(lines, ctx)
    except (gst.GSTError, ValueError) as exc:
        return JsonResponse({'ok': False, 'error': str(exc)})
    from crm.core.money import format_inr
    adv = D(p.get('advance_percent') or 0)
    advance = (r.grand_total * adv / 100).quantize(D('0.01')) if adv else D(0)
    disc_pct = (r.discount_total * 100 / r.subtotal).quantize(D('0.01')) if r.subtotal else D(0)
    cost = p.get('cost_estimate')
    margin = None
    if cost and permissions.can_see(request.user, 'cost_price') and r.taxable_total:
        try:
            margin = ((r.taxable_total - D(cost)) * 100 / r.taxable_total).quantize(D('0.1'))
        except ValueError:
            margin = None
    return JsonResponse({
        'ok': True, 'supply': gst.SUPPLY_LABELS.get(r.supply_type, ''),
        'why': gst.explain(r.supply_type, india.state_name(r.place_of_supply), india.state_name(company.state_code)),
        'subtotal': format_inr(r.subtotal), 'discount': format_inr(r.discount_total), 'taxable': format_inr(r.taxable_total),
        'cgst': format_inr(r.cgst), 'sgst': format_inr(r.sgst), 'igst': format_inr(r.igst), 'round_off': format_inr(r.round_off),
        'total': format_inr(r.grand_total), 'advance': format_inr(advance), 'balance': format_inr(r.grand_total - advance),
        'intra': r.supply_type == 'intra', 'discount_pct': str(disc_pct),
        'over_limit': disc_pct > company.exec_discount_limit and not permissions.has_perm(request.user, module, 'approve'),
        'margin': str(margin) if margin is not None else None,
        'margin_low': margin is not None and margin < company.margin_floor_percent,
        'lines': [{'key': lr.key, 'total': format_inr(lr.total), 'tax': format_inr(lr.tax)} for lr in r.lines],
    })


# ---------------------------------------------------------------- detail and actions

def _detail_ctx(request, q, forms=None, open_modal=''):
    user = request.user
    reasons, level = svc.approval_reasons(q, user) if q.status == 'draft' else ([], '')
    pending = approvals.pending_for(q, 'quotation')
    can_edit = permissions.in_scope(user, 'quotations', 'edit', q)
    forms = forms or {}
    to_default = q.contact_email or (q.client.email if q.client_id else '')
    defaults = {
        'send': SendForm(prefix='send', initial={'to': to_default, 'channel': 'email' if to_default else 'link'}),
        'accept': AcceptForm(prefix='accept', initial={'accepted_by': q.contact_name, 'accepted_on': india.today()}),
        'reject': RejectForm(prefix='reject'), 'approval': ApprovalRequestForm(prefix='approval'),
        'override': GstOverrideForm(prefix='override'),
    }
    defaults.update(forms)
    from crm.models import PaymentLink
    links = PaymentLink.objects.filter(quotation__in=q.all_versions()).order_by('-id')
    return {
        'q': q, 'lines': q.lines.select_related('service'), 'milestones': q.milestones.all(), 'versions': q.all_versions(),
        'reasons': reasons, 'approval_level': level, 'pending': pending,
        'can_decide': approvals.can_decide(user, pending)[0] if pending else False,
        'blockers': svc.send_blockers(q) if q.status in ('draft', 'approved') else [],
        'can_edit': can_edit, 'forms': defaults, 'open_modal': open_modal,
        'can_cancel': permissions.has_perm(user, 'quotations', 'cancel') and can_edit,
        'can_link': permissions.has_perm(user, 'payment_links', 'create'),
        'can_invoice': permissions.has_perm(user, 'invoices', 'create'),
        'can_override': permissions.has_perm(user, 'quotations', 'override'),
        'show_internal': permissions.can_see(user, 'internal_notes'), 'show_cost': permissions.can_see(user, 'cost_price'),
        'share_link': svc.share_url(q, request) if q.share_nonce else '',
        'links': links, 'company': CompanySettings.get(), 'history': approvals.history_for(q),
        'views': q.views.all()[:20], 'margin_floor': CompanySettings.get().margin_floor_percent,
    }


@staff_required
def quotation_detail(request, pk):
    q = get_quotation(request, pk)
    return render(request, 'crm/quotations/detail.html', _detail_ctx(request, q, open_modal=request.GET.get('modal', '')))


ACTION_FORMS = {'send': SendForm, 'accept': AcceptForm, 'reject': RejectForm, 'approval': ApprovalRequestForm,
                'override': GstOverrideForm}


@staff_required
@require_POST
def quotation_action(request, pk, action):
    q = get_quotation(request, pk, 'view')
    user = request.user
    perm = {'void': 'cancel', 'override': 'override'}.get(action, 'edit')
    permissions.require(user, 'quotations', perm, request)
    if perm == 'edit':
        permissions.require_object(user, 'quotations', 'edit', q, request)
    form = None
    if action in ACTION_FORMS:
        form = ACTION_FORMS[action](request.POST, prefix=action)
        if not form.is_valid():
            return render(request, 'crm/quotations/detail.html', _detail_ctx(request, q, {action: form}, action), status=400)
    c = form.cleaned_data if form else {}
    try:
        if action == 'approval':
            svc.submit_for_approval(q, user, c.get('message', ''))
            messages.success(request, 'Approval requested. The quotation is read-only until a decision.')
        elif action == 'cancel_approval':
            svc.cancel_approval(q, user)
            messages.success(request, 'Approval request cancelled. The quotation is a draft again.')
        elif action == 'send':
            channel = 'email' if c['channel'] == 'email' else 'link'
            svc.send(q, user, to=c.get('to', ''), cc=c.get('cc') or [], subject=c.get('subject', ''),
                     message=c.get('message', ''), channel=channel, request=request)
            q.refresh_from_db()
            if channel == 'email':
                messages.success(request, f'Quotation {q.display_number} sent to {q.contact_name or c["to"]} ({c["to"]}).')
            else:
                messages.success(request, f'Quotation {q.display_number} marked as sent. Copy the share link below.')
        elif action == 'accept':
            svc.accept(q, user, accepted_by=c['accepted_by'], evidence=c['evidence'], note=c.get('note', ''))
            messages.success(request, 'Acceptance recorded. Next: mark the lead won and collect the advance.')
        elif action == 'reject':
            svc.reject(q, user, reason=c['reason'], competitor=c.get('competitor', ''), notes=c.get('notes', ''))
            messages.success(request, 'Rejection recorded.')
        elif action == 'void':
            svc.void(q, user, request.POST.get('reason', ''))
            messages.success(request, 'Quotation voided.')
        elif action == 'new_version':
            nv = svc.new_version(q, user)
            messages.success(request, f'Version {nv.version} created as a draft. The sent version stays as it was.')
            return redirect('crm:quotation_edit', pk=nv.pk)
        elif action == 'duplicate':
            nq = svc.duplicate(q, user)
            messages.success(request, f'Duplicated as {nq.display_number}.')
            return redirect('crm:quotation_edit', pk=nq.pk)
        elif action == 'resend':
            svc.resend(q, user, to=request.POST.get('to', q.contact_email), message=request.POST.get('message', ''))
            messages.success(request, 'Quotation resent with the stored PDF.')
        elif action == 'override':
            svc.request_gst_override(q, user, taxability=c['taxability'], rate=c['rate'], reason=c['reason'])
            messages.warning(request, 'Tax treatment overridden. It needs Accountant or Owner approval before sending.')
        else:
            raise Http404
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:quotation_detail', pk=q.pk)


@staff_required
def quotation_pdf_view(request, pk):
    q = get_quotation(request, pk)
    if q.pdf_file and q.status not in ('draft', 'awaiting_approval', 'approved'):
        q.pdf_file.open('rb')
        data = q.pdf_file.read()
        q.pdf_file.close()
    else:
        data = pdf.quotation_pdf(q)
    resp = HttpResponse(data, content_type='application/pdf')
    disp = 'attachment' if request.GET.get('download') else 'inline'
    resp['Content-Disposition'] = f'{disp}; filename="{(q.number or "draft-" + str(q.pk)).replace("/", "-")}-v{q.version}.pdf"'
    return resp


@staff_required
def quotation_compare(request, pk):
    q = get_quotation(request, pk)
    versions = list(q.all_versions())
    if len(versions) < 2:
        messages.info(request, 'There is only one version.')
        return redirect('crm:quotation_detail', pk=q.pk)
    try:
        a = next(v for v in versions if v.pk == int(request.GET.get('a', versions[-2].pk)))
        b = next(v for v in versions if v.pk == int(request.GET.get('b', versions[-1].pk)))
    except (StopIteration, ValueError):
        a, b = versions[-2], versions[-1]
    return render(request, 'crm/quotations/compare.html', {'q': q, 'a': a, 'b': b, 'versions': versions, 'diff': svc.compare(a, b)})


# ---------------------------------------------------------------- public share page (no login)

def public_quotation(request, token):
    ip = client_ip(request) or ''
    if not ratelimit.hit('public-quote', ip, limit=60, window_seconds=60):
        return HttpResponse('Too many requests. Try again in a minute.', status=429)
    q = svc.by_token(token)
    if q is None or q.status in ('draft', 'awaiting_approval', 'void'):
        raise Http404
    accepted_now = False
    error = ''
    if request.method == 'POST' and request.POST.get('action') == 'accept':
        name = request.POST.get('name', '').strip()
        if not name or not request.POST.get('agree'):
            error = 'Enter your name and tick the box to accept.'
        else:
            try:
                svc.accept(q, None, accepted_by=name, evidence='online', note=f'Accepted online from {ip}', online=True)
                accepted_now = True
                q.refresh_from_db()
            except ValidationError as exc:
                error = ' '.join(exc.messages)
    elif request.method == 'GET':
        svc.record_view(q, ip, request.META.get('HTTP_USER_AGENT', ''))
        q.refresh_from_db()
    return render(request, 'crm/quotations/public.html', {
        'q': q, 'lines': q.lines.all(), 'milestones': q.milestones.all(), 'company': CompanySettings.get(),
        'token': token, 'accepted_now': accepted_now, 'error': error,
        'superseded': q.status == 'superseded', 'expired': q.status == 'expired' or q.is_expired_by_date,
    })


def public_quotation_pdf(request, token):
    q = svc.by_token(token)
    if q is None or q.status in ('draft', 'awaiting_approval', 'void') or not q.pdf_file:
        raise Http404
    q.pdf_file.open('rb')
    data = q.pdf_file.read()
    q.pdf_file.close()
    resp = HttpResponse(data, content_type='application/pdf')
    resp['Content-Disposition'] = f'inline; filename="{q.number.replace("/", "-")}.pdf"'
    return resp
