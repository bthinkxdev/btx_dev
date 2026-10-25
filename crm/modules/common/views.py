"""Cross-module pages: notifications, global search, approvals inbox, attachments."""
from __future__ import annotations

import mimetypes
from datetime import timedelta

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from crm.core import approvals, audit, crypto, permissions
from crm.core.forms import validate_upload
from crm.core.views import back, staff_required


# ---------------------------------------------------------------- notifications (13.3)

@staff_required
def notification_list(request):
    from crm.models import Notification
    qs = Notification.objects.filter(user=request.user).order_by('read_at', '-id')
    page = Paginator(qs, 30).get_page(request.GET.get('page'))
    return render(request, 'crm/common/notifications.html', {'page_obj': page})


@staff_required
def notification_open(request, pk):
    from crm.models import Notification
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    if not n.read_at:
        n.read_at = timezone.now()
        n.save(update_fields=['read_at'])
    if n.url and n.url.startswith('/'):
        return redirect(n.url)
    return redirect('crm:notification_list')


@staff_required
@require_POST
def notification_snooze(request, pk):
    from crm.models import Notification
    n = get_object_or_404(Notification, pk=pk, user=request.user)
    if n.level == 'critical':
        messages.error(request, 'Critical alerts cannot be snoozed.')
    else:
        n.read_at = timezone.now()
        n.snoozed_until = timezone.now() + timedelta(hours=1)
        n.save(update_fields=['read_at', 'snoozed_until'])
        messages.success(request, 'Snoozed for 1 hour.')
    return back(request, reverse('crm:notification_list'))


@staff_required
@require_POST
def notifications_read_all(request):
    from crm.core.notify import mark_all_read
    n = mark_all_read(request.user)
    messages.success(request, f'Marked {n} notification{"s" if n != 1 else ""} as read.')
    return back(request, reverse('crm:notification_list'))


# ---------------------------------------------------------------- global search

@staff_required
def search(request):
    from crm.models import Client, Invoice, Lead, Quotation
    term = request.GET.get('q', '').strip()
    results = []
    if len(term) >= 2:
        digits = ''.join(c for c in term if c.isdigit())
        u = request.user
        if permissions.has_perm(u, 'leads', 'view'):
            q = Q(name__icontains=term) | Q(company__icontains=term) | Q(email__icontains=term)
            if len(digits) >= 5:
                q |= Q(phone__contains=digits[-10:])
            for l in permissions.scoped(Lead.objects.filter(q), u, 'leads', 'view',
                                        extra_q=Q(owner__isnull=True) if permissions.has_perm(u, 'leads', 'assign') else None)[:10]:
                results.append(('Lead', str(l), l.get_stage_display(), reverse('crm:lead_detail', args=[l.pk])))
        if permissions.has_perm(u, 'clients', 'view'):
            q = Q(display_name__icontains=term) | Q(legal_name__icontains=term) | Q(gstin__iexact=term) | Q(email__icontains=term)
            if len(digits) >= 5:
                q |= Q(phone__contains=digits[-10:])
            for c in permissions.scoped(Client.objects.filter(q), u, 'clients', 'view')[:10]:
                results.append(('Client', str(c), c.get_status_display(), reverse('crm:client_detail', args=[c.pk])))
        if permissions.has_perm(u, 'quotations', 'view'):
            q = Q(number__icontains=term) | Q(title__icontains=term) | Q(client__display_name__icontains=term) | Q(lead__name__icontains=term)
            for x in permissions.scoped(Quotation.objects.filter(q), u, 'quotations', 'view').select_related('client', 'lead')[:10]:
                results.append(('Quotation', x.display_number, x.get_status_display(), reverse('crm:quotation_detail', args=[x.pk])))
        if permissions.has_perm(u, 'invoices', 'view'):
            q = Q(number__icontains=term) | Q(client__display_name__icontains=term)
            for x in permissions.scoped(Invoice.objects.filter(q), u, 'invoices', 'view').select_related('client')[:10]:
                results.append(('Invoice', f'{x.display_number} · {x.client}', x.get_status_display(), reverse('crm:invoice_detail', args=[x.pk])))
    return render(request, 'crm/common/search.html', {'term': term, 'results': results})


# ---------------------------------------------------------------- approvals (3.4)

@staff_required
def approval_list(request):
    from crm.models import ApprovalRequest
    tab = request.GET.get('tab', 'mine')
    qs = ApprovalRequest.objects.select_related('requested_by', 'decided_by').order_by('-id')
    if tab == 'requested':
        qs = qs.filter(requested_by=request.user)
    elif tab == 'history':
        qs = qs.filter(decided_by=request.user)
    else:
        from crm.modules.reports.dashboards import pending_approvals_for
        overdue = 24 if request.GET.get('overdue') == '1' else None
        qs = qs.filter(pk__in=[r.pk for r in pending_approvals_for(request.user, overdue_hours=overdue)])
    page = Paginator(qs, 25).get_page(request.GET.get('page'))
    return render(request, 'crm/common/approvals.html', {'page_obj': page, 'tab': tab, 'overdue': request.GET.get('overdue') == '1'})


@staff_required
def approval_detail(request, pk):
    from crm.models import ApprovalRequest
    req = get_object_or_404(ApprovalRequest.objects.select_related('requested_by', 'decided_by'), pk=pk)
    can, why = approvals.can_decide(request.user, req)
    if not (can or req.requested_by_id == request.user.pk or req.decided_by_id == request.user.pk
            or permissions.has_perm(request.user, 'audit', 'view') or permissions.has_perm(request.user, req.module, 'approve')):
        raise Http404
    target = req.target
    target_url = target.get_crm_url() if target is not None and hasattr(target, 'get_crm_url') else ''
    rows = []
    keys = list(dict.fromkeys(list(req.before.keys()) + list(req.after.keys())))
    for k in keys:
        rows.append((k.replace('_', ' ').capitalize(), req.before.get(k, '—'), req.after.get(k, '—')))
    return render(request, 'crm/common/approval_detail.html', {
        'req': req, 'can_decide': can, 'why': why, 'target_url': target_url, 'rows': rows,
        'history': approvals.history_for(target) if target is not None else [],
    })


@staff_required
@require_POST
def approval_decide(request, pk):
    from crm.models import ApprovalRequest
    req = get_object_or_404(ApprovalRequest, pk=pk)
    decision = request.POST.get('decision')
    comment = request.POST.get('comment', '').strip()
    try:
        if decision == 'approve':
            approvals.approve(req, request.user, comment)
            messages.success(request, f'Approved: {req.title}')
        elif decision == 'reject':
            approvals.reject(req, request.user, comment)
            messages.success(request, f'Rejected: {req.title}')
        elif decision == 'cancel' and req.requested_by_id == request.user.pk:
            approvals.cancel(req, request.user, comment or 'Cancelled by requester')
            messages.success(request, 'Approval request cancelled.')
        else:
            raise PermissionDenied('You cannot make that decision.')
    except ValidationError as exc:
        messages.error(request, ' '.join(exc.messages))
    return redirect('crm:approval_detail', pk=req.pk)


# ---------------------------------------------------------------- attachments (15.1)

ATTACHABLE = {
    'lead': ('leads', 'edit'), 'client': ('clients', 'edit'), 'quotation': ('quotations', 'edit'),
    'invoice': ('invoices', 'edit'), 'payment': ('payments', 'create'), 'expense': ('expenses', 'create'),
    'project': ('projects', 'edit'), 'task': ('tasks', 'edit'), 'employee': ('employees', 'edit'),
    'contract': ('contracts', 'edit'), 'ticket': ('tickets', 'edit'), 'journalentry': ('accounting', 'create'),
    'leaverequest': ('leave', 'create'), 'refund': ('refunds', 'create'),
}
VIEW_PERM = {k: (v[0], 'view') for k, v in ATTACHABLE.items()}


def _target(model_name, pk):
    try:
        ct = ContentType.objects.get(app_label='crm', model=model_name)
    except ContentType.DoesNotExist:
        raise Http404
    obj = ct.get_object_for_this_type(pk=pk)
    return ct, obj


def save_attachment(obj, upload, user, purpose='', visibility='internal'):
    from crm.models import Attachment
    validate_upload(upload)
    digest = crypto.sha256_file(upload)
    att = Attachment.objects.create(
        content_type=ContentType.objects.get_for_model(type(obj)), object_id=str(obj.pk), file=upload,
        original_name=upload.name[:255], mime_type=(mimetypes.guess_type(upload.name)[0] or 'application/octet-stream'),
        size=upload.size, sha256=digest, purpose=purpose, visibility=visibility, uploaded_by=user)
    audit.log('attachment.uploaded', obj, actor=user, changes={'file': att.original_name, 'sha256': digest})
    return att


@staff_required
@require_POST
def attachment_upload(request, model_name, pk):
    if model_name not in ATTACHABLE:
        raise Http404
    module, action = ATTACHABLE[model_name]
    ct, obj = _target(model_name, pk)
    permissions.require_object(request.user, module, 'view', obj, request)
    permissions.require(request.user, module, action, request)
    f = request.FILES.get('file')
    if not f:
        messages.error(request, 'Choose a file to upload.')
    else:
        try:
            save_attachment(obj, f, request.user, purpose=request.POST.get('purpose', '')[:40],
                            visibility='shareable' if request.POST.get('visibility') == 'shareable' else 'internal')
            messages.success(request, f'Uploaded {f.name}.')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
    return back(request, reverse('crm:home'))


def signed_file_url(attachment, user) -> str:
    token = crypto.sign_value(f'{attachment.pk}:{user.pk}')
    return reverse('crm:file_download', args=[token])


@staff_required
def attachment_open(request, pk):
    """Permission-checked entry point that issues a short-lived signed URL."""
    from crm.models import Attachment
    att = get_object_or_404(Attachment, pk=pk, archived_at__isnull=True)
    model_name = att.content_type.model
    module = VIEW_PERM.get(model_name, ('settings', 'configure'))[0]
    target = att.target
    if target is None:
        raise Http404
    permissions.require_object(request.user, module, 'view', target, request)
    return redirect(signed_file_url(att, request.user))


@staff_required
def file_download(request, token):
    from crm.models import Attachment
    value = crypto.unsign_value(token, max_age=300)
    if not value:
        raise Http404
    att_id, _, uid = value.partition(':')
    if str(request.user.pk) != uid:
        raise Http404
    att = get_object_or_404(Attachment, pk=att_id)
    try:
        fh = att.file.open('rb')
    except FileNotFoundError:
        raise Http404
    inline = att.mime_type in ('application/pdf', 'image/png', 'image/jpeg', 'image/webp')
    resp = FileResponse(fh, content_type=att.mime_type, as_attachment=not inline, filename=att.original_name)
    resp['X-Content-Type-Options'] = 'nosniff'
    resp['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'"
    return resp


@staff_required
@require_POST
def attachment_archive(request, pk):
    from crm.models import Attachment
    att = get_object_or_404(Attachment, pk=pk, archived_at__isnull=True)
    model_name = att.content_type.model
    module, action = ATTACHABLE.get(model_name, ('settings', 'configure'))
    target = att.target
    permissions.require_object(request.user, module, action, target, request)
    att.archived_at = timezone.now()
    att.save(update_fields=['archived_at'])
    audit.log('attachment.archived', target, changes={'file': att.original_name})
    messages.success(request, f'Removed {att.original_name}.')
    return back(request, reverse('crm:home'))


# ---------------------------------------------------------------- PWA: manifest, service worker, push

def manifest(request):
    """The web app manifest (Part 2 install surface). Served dynamically, not as a static
    file, so the name/short_name always match the company's current branding."""
    from crm.models import CompanySettings
    try:
        company = CompanySettings.get()
        name = company.display_name or 'BThinkX Business OS'
    except Exception:  # pragma: no cover - before migrate
        name = 'BThinkX Business OS'
    data = {
        'name': f'{name} — Business OS',
        'short_name': name[:30] or 'BThinkX',
        'description': 'Leads, quotations, billing, projects and more, in one app.',
        'id': '/crm/',
        'start_url': '/crm/?source=pwa',
        'scope': '/crm/',
        'display': 'standalone',
        'display_override': ['window-controls-overlay', 'standalone'],
        'background_color': '#ffffff',
        'theme_color': '#465fff',
        'orientation': 'portrait-primary',
        'lang': 'en',
        'categories': ['business', 'productivity'],
        'icons': [
            {'src': '/static/crm/images/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/crm/images/icon-256.png', 'sizes': '256x256', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/crm/images/icon-384.png', 'sizes': '384x384', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/crm/images/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/crm/images/icon-512-maskable.png', 'sizes': '512x512', 'type': 'image/png',
             'purpose': 'maskable'},
        ],
    }
    return JsonResponse(data, content_type='application/manifest+json')


def service_worker(request):
    """Served at /crm/service-worker.js (not /static/) so its default scope covers the
    whole app (/crm/) with no extra Service-Worker-Allowed header needed. The real source
    lives at crm/static/crm/js/sw.js (kept as a plain static file for editing/linting);
    this view just serves its content from the one URL a service worker's scope allows."""
    from django.contrib.staticfiles import finders
    path = finders.find('crm/js/sw.js')
    if not path:
        raise Http404
    with open(path, 'r', encoding='utf-8') as f:
        js = f.read()
    resp = HttpResponse(js, content_type='application/javascript')
    resp['Service-Worker-Allowed'] = '/crm/'
    resp['Cache-Control'] = 'no-cache'
    return resp


@staff_required
@require_POST
def push_subscribe(request):
    """Save (or refresh) the browser's push subscription for this user and device."""
    import json
    from crm.models import PushSubscription
    try:
        payload = json.loads(request.body.decode('utf-8'))
        endpoint = payload['endpoint']
        keys = payload['keys']
        p256dh, auth = keys['p256dh'], keys['auth']
    except (KeyError, ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Invalid subscription payload.'}, status=400)
    PushSubscription.objects.update_or_create(
        endpoint=endpoint[:500],
        defaults={'user': request.user, 'p256dh': p256dh[:180], 'auth': auth[:60],
                  'user_agent': request.META.get('HTTP_USER_AGENT', '')[:300],
                  'last_seen_at': timezone.now()},
    )
    return JsonResponse({'ok': True, 'message': 'Notifications enabled on this device.'})


@staff_required
@require_POST
def push_unsubscribe(request):
    """Remove this device's push subscription (user turned notifications off, or the
    browser reports the old subscription is no longer valid)."""
    import json
    from crm.models import PushSubscription
    try:
        endpoint = json.loads(request.body.decode('utf-8'))['endpoint']
    except (KeyError, ValueError, TypeError):
        return JsonResponse({'ok': False, 'error': 'Invalid request.'}, status=400)
    PushSubscription.objects.filter(user=request.user, endpoint=endpoint[:500]).delete()
    return JsonResponse({'ok': True, 'message': 'Notifications turned off on this device.'})
