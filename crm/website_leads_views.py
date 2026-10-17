"""
Website Leads: contact-form submissions and quotation-PDF requests captured
on the public site (the `pages` app), surfaced here for CRM admins to triage.
Read/unread state lives on the `pages` models themselves
(ContactSubmission.is_read / QuoteRequest.is_read) — this module is a view
layer over that data, not a separate copy of it.
"""
from __future__ import annotations

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from pages.models import ContactSubmission, QuoteRequest

from .rbac import is_crm_admin
from .views import _leads_url_query

WEBSITE_LEADS_PER_PAGE = 20

_MODELS_BY_TYPE = {'contact': ContactSubmission, 'quote': QuoteRequest}


def admin_required(view_fn):
    """Website Leads is admin-only — these are the business's own inbound
    sales leads, not something every CRM role needs visibility into."""

    @wraps(view_fn)
    def _wrapped(request, *args, **kwargs):
        if not is_crm_admin(request.user):
            return HttpResponseForbidden('This area is not available for your account role.')
        return view_fn(request, *args, **kwargs)

    return _wrapped


def _tag(items, lead_type):
    for item in items:
        item.lead_type = lead_type
    return items


def _get_lead_or_404(lead_type, pk):
    model = _MODELS_BY_TYPE.get(lead_type)
    if model is None:
        return None
    obj = get_object_or_404(model, pk=pk)
    _tag([obj], lead_type)
    return obj


@login_required
@admin_required
def website_leads(request):
    lead_type = (request.GET.get('type') or '').strip()
    if lead_type not in _MODELS_BY_TYPE:
        lead_type = ''
    seen = (request.GET.get('seen') or '').strip()
    if seen not in ('unseen', 'seen'):
        seen = ''

    contacts = ContactSubmission.objects.all()
    quotes = QuoteRequest.objects.all()
    if seen == 'unseen':
        contacts = contacts.filter(is_read=False)
        quotes = quotes.filter(is_read=False)
    elif seen == 'seen':
        contacts = contacts.filter(is_read=True)
        quotes = quotes.filter(is_read=True)

    items = []
    if lead_type in ('', 'contact'):
        items += _tag(list(contacts), 'contact')
    if lead_type in ('', 'quote'):
        items += _tag(list(quotes), 'quote')
    items.sort(key=lambda x: x.created_at, reverse=True)

    paginator = Paginator(items, WEBSITE_LEADS_PER_PAGE)
    page_raw = (request.GET.get('page') or '1').strip()
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)

    filters_ctx = {'type': lead_type, 'seen': seen}
    _lq = lambda **kw: _leads_url_query(filters_ctx, **kw)
    base_url = reverse('crm:website_leads')

    page_items = []
    for n in paginator.get_elided_page_range(page_obj.number, on_each_side=2, on_ends=1):
        if isinstance(n, int):
            page_items.append({
                'num': n,
                'qs': _lq(page=n) if n != 1 else _lq(page=''),
                'active': n == page_obj.number,
            })
        else:
            page_items.append(None)  # ellipsis

    prev_qs = _lq(page=page_obj.previous_page_number()) if page_obj.has_previous() else ''
    next_qs = _lq(page=page_obj.next_page_number()) if page_obj.has_next() else ''

    total_contacts = ContactSubmission.objects.count()
    total_quotes = QuoteRequest.objects.count()
    unread_contacts = ContactSubmission.objects.filter(is_read=False).count()
    unread_quotes = QuoteRequest.objects.filter(is_read=False).count()

    return render(request, 'crm/website_leads.html', {
        'leads': page_obj.object_list,
        'paginator': paginator,
        'page_obj': page_obj,
        'page_items': page_items,
        'prev_qs': prev_qs,
        'next_qs': next_qs,
        'base_url': base_url,
        'type_links': {
            'all': _lq(type='', page=''),
            'contact': _lq(type='contact', page=''),
            'quote': _lq(type='quote', page=''),
        },
        'seen_links': {
            'all': _lq(seen='', page=''),
            'unseen': _lq(seen='unseen', page=''),
            'seen': _lq(seen='seen', page=''),
        },
        'current_type': lead_type,
        'current_seen': seen,
        'counts': {
            'total': total_contacts + total_quotes,
            'contact': total_contacts,
            'quote': total_quotes,
            'unread': unread_contacts + unread_quotes,
        },
    })


@login_required
@admin_required
@require_POST
def website_lead_toggle_seen(request, lead_type, pk):
    obj = _get_lead_or_404(lead_type, pk)
    if obj is None:
        return HttpResponse(status=404)
    obj.is_read = not obj.is_read
    obj.read_at = timezone.now() if obj.is_read else None
    obj.save(update_fields=['is_read', 'read_at'])

    if request.headers.get('HX-Request'):
        return render(request, 'crm/partials/website_lead_row.html', {'lead': obj})
    return HttpResponse(status=204)


@login_required
@admin_required
def website_lead_detail(request, lead_type, pk):
    """Work modal body — opening a lead marks it seen, same as a messaging app."""
    obj = _get_lead_or_404(lead_type, pk)
    if obj is None:
        return HttpResponse(status=404)
    if not obj.is_read:
        obj.is_read = True
        obj.read_at = timezone.now()
        obj.save(update_fields=['is_read', 'read_at'])

    body = render_to_string('crm/partials/website_lead_detail.html', {'lead': obj}, request)
    # A bare <tr hx-swap-oob> is silently dropped by the browser's HTML parser
    # (a <tr> outside <table><tbody> isn't valid markup) — wrapping it keeps
    # the parser happy; htmx still only touches the row via its own id/oob
    # attribute, the wrapper itself is discarded.
    row = render_to_string('crm/partials/website_lead_row.html', {'lead': obj, 'oob': True}, request)
    body += f'<table><tbody>{row}</tbody></table>'
    return HttpResponse(body)
