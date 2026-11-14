"""Webinars in the CRM: registrants and their follow-up (own section, separate from the Leads list), the webinar itself
(date, joining link, price, seats), and moving a qualified registrant on to a Lead or a Client."""
from __future__ import annotations

from datetime import datetime, timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

import re

from crm.core import audit, crypto, permissions
from crm.core.forms import CrmModelForm
from crm.core.views import Filter, ListPage, staff_required
from crm.models import WebinarLead
from crm.models.webinars import WEBINAR_STAGES
from pages import meta, webinar_mail
from pages.models import MetaEvent, MetaTracking, Webinar, WebinarRegistration

from . import services

MODULE = 'leads'


def _list_url(w) -> str:
    return f"{reverse('crm:webinar_registrations')}?webinar={w.pk}"


RESERVED_SLUGS = {'return', 'webhook', 'register', 'calendar', 'new', 'status'}      # addresses the public site already uses


def public_url(request, w) -> str:
    """The customer-facing registration and payment page for one webinar, as a full address."""
    base = settings.SITE_BASE_URL
    if '127.0.0.1' in base or 'localhost' in base:           # not configured: use the address the CRM is opened on
        base = f'{request.scheme}://{request.get_host()}'
    return base + reverse('pages:webinar', args=[w.slug])


def campaign_links(request, w) -> list[tuple[str, str]]:
    """The page address plus tagged copies for each place it is advertised. The tags are stored with every
    registration, so the CRM shows which ad brought each person."""
    url = public_url(request, w)
    tag = w.slug[:40]
    return [('Page link', url)] + [
        (label, f'{url}?utm_source={src}&utm_medium={medium}&utm_campaign={tag}')
        for label, src, medium in (('Facebook ad', 'facebook', 'paid_social'), ('Instagram ad', 'instagram', 'paid_social'),
                                   ('Google ad', 'google', 'cpc'), ('WhatsApp share', 'whatsapp', 'social'),
                                   ('Email', 'email', 'email'))]


class WebinarForm(CrmModelForm):
    slug = forms.CharField(
        required=False, max_length=80, label='Page address',
        help_text='The end of the registration link, e.g. "ecommerce-10x". Leave empty to create it from the title. '
                  'Changing it later breaks links you have already shared.')

    class Meta:
        model = Webinar
        fields = ['title', 'slug', 'subtitle', 'starts_at', 'duration_minutes', 'price', 'platform', 'join_url',
                  'host_name', 'host_title', 'seat_limit', 'learn_points', 'audience_points', 'is_active']
        widgets = {'subtitle': forms.Textarea(attrs={'rows': 2}), 'learn_points': forms.Textarea(attrs={'rows': 8}),
                   'audience_points': forms.Textarea(attrs={'rows': 4})}
        labels = {'starts_at': 'Date and time (IST)', 'duration_minutes': 'Duration (minutes)', 'price': 'Price per seat (INR)',
                  'join_url': 'Joining link', 'seat_limit': 'Seat limit', 'is_active': 'Page is live'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:                 # a new webinar never inherits the seed webinar's address
            self.initial['slug'] = ''

    def clean_slug(self):
        raw = (self.cleaned_data.get('slug') or '').strip()
        if not raw:
            return self.instance.slug if self.instance.pk else ''
        slug = slugify(raw)[:80]
        if not slug or slug in RESERVED_SLUGS:
            raise forms.ValidationError('Choose a different address. Use letters, numbers and hyphens.')
        if Webinar.objects.filter(slug=slug).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('Another webinar already uses this address.')
        return slug

    def clean_seat_limit(self):
        n = self.cleaned_data.get('seat_limit')
        if n is not None and self.instance.pk:
            paid = self.instance.paid_count()
            if n < paid:
                raise forms.ValidationError(f'{paid} seats are already paid. The limit cannot be lower.')
        return n

    def clean_price(self):
        price = self.cleaned_data['price']
        if price < 1:
            raise forms.ValidationError('The price must be at least 1 rupee.')
        return price

    def clean_duration_minutes(self):
        n = self.cleaned_data['duration_minutes']
        if not 15 <= n <= 600:
            raise forms.ValidationError('Choose a duration between 15 minutes and 10 hours.')
        return n

    def save(self, commit=True):
        w = super().save(commit=False)
        w.slug = self.cleaned_data.get('slug') or unique_slug(w.title, w.pk)
        if commit:
            w.save()
        return w


def unique_slug(title: str, pk=None) -> str:
    base = slugify(title)[:60] or 'webinar'
    if base in RESERVED_SLUGS:
        base = f'{base}-webinar'
    slug, n = base, 2
    while Webinar.objects.filter(slug=slug).exclude(pk=pk).exists():
        slug, n = f'{base}-{n}', n + 1
    return slug


def _selected_webinar(request):
    """The webinar the registrations page is narrowed to, or None when it lists every webinar."""
    pk = request.GET.get('webinar', '')
    return Webinar.objects.filter(pk=pk).first() if pk.isdigit() else None


def _users():
    return get_user_model().objects.filter(is_active=True).order_by('first_name', 'username')


def _user_label(u) -> str:
    return u.get_full_name() or u.get_username()


class RegistrationList(ListPage):
    """Webinar registrants (paid or not) with their follow-up stage. This is the only place they are listed."""
    model = WebinarLead
    module = MODULE
    template_name = 'crm/webinars/registrations.html'
    title = 'Webinar registrations'
    search_fields = ('registration__name__icontains', 'registration__email__icontains',
                     'registration__phone__icontains', 'registration__business_name__icontains')
    default_sort = '-created_at'
    quick_filters = {
        'paid': ('Paid seats', lambda qs, r: qs.filter(registration__status='paid')),
        'followup': ('Not paid', lambda qs, r: qs.exclude(registration__status='paid')),
        'mine': ('Mine', lambda qs, r: qs.filter(owner=r.user)),
        'due': ('Follow-up due', lambda qs, r: qs.filter(next_follow_up_at__lte=timezone.now())
                .exclude(stage__in=['converted', 'not_interested'])),
    }
    filters = [
        Filter('stage', 'Stage', WEBINAR_STAGES),
        Filter('status', 'Payment', WebinarRegistration.Status.choices,
               apply=lambda qs, v, r: qs.filter(registration__status=v)),
        Filter('owner', 'Owner', lambda r: [(u.pk, _user_label(u)) for u in _users()]),
        Filter('webinar', 'Webinar', lambda r: [(w.pk, w.title) for w in Webinar.objects.order_by('-starts_at')],
               apply=lambda qs, v, r: qs.filter(registration__webinar_id=v)),
    ]
    export_name = 'webinar-registrations'
    export_columns = [
        ('Registered', lambda o: timezone.localtime(o.registration.created_at).strftime('%Y-%m-%d %H:%M')),
        ('Name', lambda o: o.registration.name), ('Email', lambda o: o.registration.email),
        ('Phone', lambda o: o.registration.phone), ('Business', lambda o: o.registration.business_name),
        ('Payment', lambda o: o.registration.get_status_display()), ('Amount', lambda o: o.registration.amount),
        ('Stage', lambda o: o.get_stage_display()), ('Owner', lambda o: _user_label(o.owner) if o.owner else ''),
        ('Webinar', lambda o: o.registration.webinar.title),
        ('Campaign', lambda o: o.registration.utm_campaign), ('Source', lambda o: o.registration.utm_source),
        ('Attended', lambda o: 'Yes' if o.registration.attended else 'No'),
        ('In Leads', lambda o: 'Yes' if o.lead_id else 'No'), ('Client', lambda o: 'Yes' if o.client_id else 'No')]

    def base_queryset(self):
        return WebinarLead.objects.select_related('registration', 'registration__webinar', 'owner', 'lead', 'client')

    def extra_context(self, qs):
        w = _selected_webinar(self.request)
        stats = {}
        if w:
            regs = w.registrations.aggregate(
                paid=Count('pk', filter=Q(status='paid')), revenue=Sum('amount', filter=Q(status='paid')),
                open=Count('pk', filter=~Q(status='paid')), attended=Count('pk', filter=Q(status='paid', attended=True)),
                emailed=Count('pk', filter=Q(status='paid', joining_sent_at__isnull=False)))
            leads = WebinarLead.objects.filter(registration__webinar=w).aggregate(
                qualified=Count('pk', filter=Q(stage='qualified')), moved=Count('pk', filter=Q(lead__isnull=False)),
                clients=Count('pk', filter=Q(client__isnull=False)))
            stats = {**regs, **leads, 'revenue': regs['revenue'] or 0}
        return {'module_code': MODULE, 'webinar': w, 'stats': stats, 'search_placeholder': 'Search registrants…',
                'page_links': campaign_links(self.request, w) if w else [],
                'can_edit': permissions.has_perm(self.request.user, MODULE, 'edit'),
                'can_configure': permissions.has_perm(self.request.user, MODULE, 'configure'), 'now': timezone.now()}


def _status(w, now) -> tuple[str, str]:
    if not w.is_active:
        return 'Offline', 'grey'
    if now >= w.ends_at:
        return 'Completed', 'grey'
    if now >= w.starts_at:
        return 'Live now', 'green'
    seats = w.seats_left()
    if seats == 0:
        return 'Sold out', 'amber'
    return 'Open for registration', 'green'


@staff_required
def webinar_list(request):
    """Every webinar with its own numbers and its own registration link."""
    permissions.require(request.user, MODULE, 'view', request)
    now = timezone.now()
    show = request.GET.get('show', 'all')
    paid = Q(registrations__status='paid')
    qs = Webinar.objects.annotate(
        total=Count('registrations', distinct=True), paid=Count('registrations', filter=paid, distinct=True),
        revenue=Sum('registrations__amount', filter=paid),
        qualified=Count('registrations__crm', filter=Q(registrations__crm__stage='qualified'), distinct=True),
        moved=Count('registrations__crm', filter=Q(registrations__crm__lead__isnull=False), distinct=True),
        clients=Count('registrations__crm', filter=Q(registrations__crm__client__isnull=False), distinct=True))
    if show == 'upcoming':
        qs = qs.filter(is_active=True, starts_at__gt=now).order_by('starts_at')
    elif show == 'past':
        qs = qs.filter(starts_at__lte=now)
    elif show == 'offline':
        qs = qs.filter(is_active=False)
    else:
        show = 'all'
    rows = []
    for w in qs[:200]:
        label, tone = _status(w, now)
        rows.append({'w': w, 'status': label, 'tone': tone, 'links': campaign_links(request, w),
                     'seats_left': w.seats_left()})
    counts = Webinar.objects.aggregate(
        all=Count('pk'), upcoming=Count('pk', filter=Q(is_active=True, starts_at__gt=now)),
        past=Count('pk', filter=Q(starts_at__lte=now)), offline=Count('pk', filter=Q(is_active=False)))
    tabs = [(k, label, counts[k]) for k, label in (('all', 'All'), ('upcoming', 'Upcoming'), ('past', 'Past'),
                                                   ('offline', 'Offline'))]
    return render(request, 'crm/webinars/list.html', {
        'rows': rows, 'show': show, 'tabs': tabs,
        'can_configure': permissions.has_perm(request.user, MODULE, 'configure'),
        'is_admin': permissions.is_super_admin(request.user), 'tracking': meta.config()})


@staff_required
@require_POST
def webinar_duplicate(request, pk):
    """A copy for the next session: same content, price and length; offline, a week later, with no joining link."""
    permissions.require(request.user, MODULE, 'configure', request)
    src = get_object_or_404(Webinar, pk=pk)
    copy = Webinar.objects.create(
        title=src.title[:190] + ' (copy)' if not src.title.endswith('(copy)') else src.title, subtitle=src.subtitle,
        slug=unique_slug(src.title + ' copy'), starts_at=src.starts_at + timedelta(days=7),
        duration_minutes=src.duration_minutes, price=src.price, platform=src.platform, join_url='',
        host_name=src.host_name, host_title=src.host_title, seat_limit=src.seat_limit, learn_points=src.learn_points,
        audience_points=src.audience_points, is_active=False)
    audit.log('webinar.duplicated', copy, actor=request.user, changes={'from': src.pk})
    messages.success(request, 'Copied. Set the date and time, then make the page live.')
    return redirect('crm:webinar_edit', pk=copy.pk)


@staff_required
@require_POST
def webinar_toggle(request, pk):
    """Take a webinar's page offline, or put it live again. Webinars are never deleted: they hold payments."""
    permissions.require(request.user, MODULE, 'configure', request)
    w = get_object_or_404(Webinar, pk=pk)
    w.is_active = not w.is_active
    w.save(update_fields=['is_active'])
    audit.log('webinar.live' if w.is_active else 'webinar.offline', w, actor=request.user, changes={})
    messages.success(request, f'"{w.title}" is {"live" if w.is_active else "offline"}.')
    return redirect('crm:webinar_list')


@staff_required
def webinar_form(request, pk=None):
    permissions.require(request.user, MODULE, 'configure', request)
    w = get_object_or_404(Webinar, pk=pk) if pk else None
    form = WebinarForm(request.POST or None, instance=w)
    if request.method == 'POST' and form.is_valid():
        created = w is None
        w = form.save()
        audit.log('webinar.created' if created else 'webinar.updated', w, actor=request.user,
                  changes={'title': w.title, 'starts_at': w.starts_at.isoformat(), 'live': w.is_active})
        messages.success(request, f'"{w.title}" saved.')
        return redirect('crm:webinar_list')
    return render(request, 'crm/webinars/form.html', {'form': form, 'obj': w,
                                                      'page_url': public_url(request, w) if w else ''})


def _emails(reg):
    from crm.models import DeliveryLog
    return DeliveryLog.objects.filter(object_type='webinarregistration', object_id=str(reg.pk))[:15]


@staff_required
def lead_detail(request, pk):
    permissions.require(request.user, MODULE, 'view', request)
    wl = get_object_or_404(WebinarLead.objects.select_related('registration', 'registration__webinar', 'owner', 'lead',
                                                              'client'), pk=pk)
    return render(request, 'crm/webinars/detail.html', {
        'wl': wl, 'reg': wl.registration, 'w': wl.registration.webinar,
        'activities': wl.activities.select_related('actor')[:100],
        'stages': [s for s in WEBINAR_STAGES if s[0] != 'converted'],
        'users': [(u.pk, _user_label(u)) for u in _users()], 'emails': _emails(wl.registration),
        'meta_events': wl.registration.meta_events.all()[:10],
        'can_edit': permissions.has_perm(request.user, MODULE, 'edit'),
        'can_create_lead': permissions.has_perm(request.user, 'leads', 'create'),
        'can_create_client': (permissions.has_perm(request.user, 'clients', 'create')
                              and permissions.has_perm(request.user, 'leads', 'create')),
    })


def _back(wl):
    return redirect('crm:webinar_lead_detail', pk=wl.pk)


def _get(request, pk):
    permissions.require(request.user, MODULE, 'edit', request)
    return get_object_or_404(WebinarLead.objects.select_related('registration', 'registration__webinar'), pk=pk)


@staff_required
@require_POST
def lead_stage(request, pk):
    wl = _get(request, pk)
    if wl.is_converted:
        messages.error(request, 'This registrant has already been moved to the CRM. Continue in the lead or client.')
        return _back(wl)
    try:
        services.set_stage(wl, request.POST.get('stage', ''), request.user, request.POST.get('note', '').strip()[:500])
        messages.success(request, f'Stage updated to {wl.get_stage_display()}.')
    except ValueError:
        messages.error(request, 'Choose a valid stage.')
    return _back(wl)


@staff_required
@require_POST
def lead_assign(request, pk):
    wl = _get(request, pk)
    owner_id = request.POST.get('owner', '')
    user = _users().filter(pk=owner_id).first() if owner_id.isdigit() else None
    if owner_id and user is None:
        messages.error(request, 'Choose a valid person.')
    else:
        services.assign(wl, user, request.user)
        messages.success(request, f'Assigned to {_user_label(user)}.' if user else 'Unassigned.')
    return _back(wl)


@staff_required
@require_POST
def lead_note(request, pk):
    wl = _get(request, pk)
    text = request.POST.get('text', '').strip()
    if not text:
        messages.error(request, 'Write the note first.')
        return _back(wl)
    follow_up = None
    raw = request.POST.get('follow_up', '').strip()
    if raw:
        try:
            follow_up = timezone.make_aware(datetime.strptime(raw, '%Y-%m-%dT%H:%M'))
        except ValueError:
            messages.error(request, 'That follow-up time is not valid.')
            return _back(wl)
    services.add_note(wl, request.POST.get('kind', 'note'), text, request.user, follow_up)
    messages.success(request, 'Saved.' + (' Follow-up scheduled.' if follow_up else ''))
    return _back(wl)


@staff_required
@require_POST
def lead_convert(request, pk):
    wl = _get(request, pk)
    target = request.POST.get('to')
    if target == 'client':
        permissions.require(request.user, 'clients', 'create', request)
        permissions.require(request.user, 'leads', 'create', request)
        client = services.convert_to_client(wl, request.user)
        messages.success(request, f'{wl.registration.name} is now a client. Start their onboarding project from the client page.')
        return redirect('crm:client_detail', pk=client.pk)
    if target == 'lead':
        permissions.require(request.user, 'leads', 'create', request)
        lead = services.convert_to_lead(wl, request.user)
        messages.success(request, f'{wl.registration.name} is now in the Leads pipeline.')
        return redirect('crm:lead_detail', pk=lead.pk)
    raise Http404


@staff_required
@require_POST
def registration_action(request, pk):
    """Actions on the registration itself: email the confirmation again, mark attended, email the joining link."""
    wl = _get(request, pk)
    reg = wl.registration
    action = request.POST.get('action')
    if action == 'attended':
        reg.attended = not reg.attended
        reg.save(update_fields=['attended'])
        messages.success(request, f'{reg.name} marked as {"attended" if reg.attended else "not attended"}.')
    elif action == 'resend' and reg.status == 'paid':
        ok = webinar_mail.send_confirmation(reg)
        messages.success(request, f'Confirmation emailed again to {reg.email}.' if ok else 'The email could not be queued.')
    elif action == 'joining' and reg.status == 'paid':
        if not reg.webinar.join_url:
            messages.error(request, 'Add the joining link to the webinar first.')
        else:
            ok = webinar_mail.send_joining_link(reg, actor=request.user)
            messages.success(request, f'Joining link emailed to {reg.email}.' if ok else 'The email could not be queued.')
    else:
        messages.error(request, 'That action is not available.')
    audit.log(f'webinar.registration_{action}', reg, actor=request.user, changes={})
    return _back(wl) if request.POST.get('next') == 'detail' else redirect(_list_url(reg.webinar))


@staff_required
@require_POST
def send_joining_links(request, pk):
    """Email the joining link to every paid registrant who has not had it yet (or to all, when asked)."""
    permissions.require(request.user, MODULE, 'edit', request)
    w = get_object_or_404(Webinar, pk=pk)
    if not w.join_url:
        messages.error(request, 'Add the joining link to the webinar first, then send it.')
        return redirect(_list_url(w))
    qs = w.registrations.filter(status='paid')
    if request.POST.get('scope') != 'all':
        qs = qs.filter(joining_sent_at__isnull=True)
    sent = sum(1 for reg in qs.select_related('webinar') if webinar_mail.send_joining_link(reg, actor=request.user))
    audit.log('webinar.joining_links_sent', w, actor=request.user, changes={'count': sent})
    messages.success(request, f'Joining link emailed to {sent} registrant{"s" if sent != 1 else ""}.' if sent
                     else 'Everyone who paid already has the joining link.')
    return redirect(_list_url(w))


# ---------------------------------------------------------------- Meta tracking (administrators only)

def _require_admin(request):
    if not permissions.is_super_admin(request.user):
        permissions.denied(request.user, MODULE, 'configure', request, detail='meta tracking settings')


class TrackingForm(CrmModelForm):
    capi_token = forms.CharField(
        required=False, max_length=600, label='Access token',
        widget=forms.PasswordInput(render_value=False, attrs={'autocomplete': 'new-password'}),
        help_text='Write-only: stored encrypted and never shown again. Leave blank to keep the current token.')
    clear_token = forms.BooleanField(required=False, label='Remove the stored token')

    class Meta:
        model = MetaTracking
        fields = ['pixel_enabled', 'pixel_id', 'capi_enabled', 'test_event_code', 'graph_version']
        labels = {'pixel_enabled': 'Browser pixel is on', 'pixel_id': 'Pixel ID', 'capi_enabled': 'Server events are on',
                  'test_event_code': 'Test event code', 'graph_version': 'API version'}
        help_texts = {
            'pixel_id': 'Events Manager > Data sources > your pixel. Digits only, e.g. 123456789012345.',
            'test_event_code': 'Only while testing. Events then show in the Test events tab and are not used for ads. '
                               'Clear it for production.',
            'graph_version': 'Leave as is unless Meta retires this version, e.g. v21.0.',
            'pixel_enabled': 'Untick to stop the pixel on the public pages without losing the saved ID.',
            'capi_enabled': 'Sends Lead, InitiateCheckout and Purchase from the server. Needs the pixel ID and token.'}

    def clean_pixel_id(self):
        value = (self.cleaned_data.get('pixel_id') or '').strip()
        if value and not re.fullmatch(r'\d{8,20}', value):
            raise forms.ValidationError('The pixel ID is a number of 8 to 20 digits.')
        return value

    def clean_test_event_code(self):
        value = (self.cleaned_data.get('test_event_code') or '').strip()
        if value and not re.fullmatch(r'[A-Za-z0-9]{3,40}', value):
            raise forms.ValidationError('Use the code exactly as Events Manager shows it, e.g. TEST12345.')
        return value

    def clean_graph_version(self):
        value = (self.cleaned_data.get('graph_version') or '').strip() or 'v21.0'
        if not re.fullmatch(r'v\d{1,2}\.\d', value):
            raise forms.ValidationError('Write the version like v21.0.')
        return value

    def clean_capi_token(self):
        value = (self.cleaned_data.get('capi_token') or '').strip()
        if value and (len(value) < 30 or ' ' in value):
            raise forms.ValidationError('That does not look like an access token. Copy the whole token from Events Manager.')
        return value


@staff_required
def tracking_settings(request):
    """Meta pixel and Conversions API settings. Administrators only: they decide where ad conversions are reported."""
    _require_admin(request)
    row = MetaTracking.current()
    form = TrackingForm(request.POST or None, instance=row)
    if request.method == 'POST' and form.is_valid():
        before = {'pixel_id': row.pixel_id, 'pixel_enabled': row.pixel_enabled, 'capi_enabled': row.capi_enabled,
                  'test_event_code': row.test_event_code, 'graph_version': row.graph_version}
        row = form.save(commit=False)
        token = form.cleaned_data['capi_token']
        changes = {k: [v, getattr(row, k)] for k, v in before.items() if v != getattr(row, k)}
        if token:
            row.capi_token_enc = crypto.encrypt(token)
            row.token_updated_at = timezone.now()
            changes['token'] = ['(hidden)', 'replaced']
        elif form.cleaned_data['clear_token'] and row.capi_token_enc:
            row.capi_token_enc, row.token_updated_at = '', None
            changes['token'] = ['(hidden)', 'removed']
        row.updated_by = request.user
        row.save()
        if changes:
            audit.log('webinar.meta_settings', row, actor=request.user, changes=changes)
        messages.success(request, 'Meta tracking settings saved. They apply to the next visitor.')
        return redirect('crm:webinar_tracking')
    cfg = meta.config()
    week = timezone.now() - timedelta(days=7)
    counts = dict(MetaEvent.objects.filter(created_at__gte=week).values_list('status').annotate(n=Count('pk')))
    return render(request, 'crm/webinars/tracking.html', {
        'form': form, 'row': row, 'cfg': cfg, 'has_token': bool(row.capi_token_enc),
        'env_token': bool(getattr(settings, 'META_CAPI_TOKEN', '')), 'counts': counts,
        'problems': MetaEvent.objects.filter(status='failed').select_related('registration')[:8],
        'last_sent': MetaEvent.objects.filter(status='sent').order_by('-sent_at').values_list('sent_at', flat=True).first()})


@staff_required
@require_POST
def tracking_test(request):
    _require_admin(request)
    ok, message = meta.send_test(request)
    (messages.success if ok else messages.error)(request, message)
    audit.log('webinar.meta_test', MetaTracking.current(), actor=request.user, changes={'ok': ok})
    return redirect('crm:webinar_tracking')
