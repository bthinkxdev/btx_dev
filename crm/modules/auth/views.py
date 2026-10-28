"""Sign-in with lockout, optional TOTP 2FA, sessions, profile and security (Part 3.6)."""
from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.sessions.models import Session
from django.db.models import F, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from crm.core import audit, notify, permissions, totp
from crm.core.forms import StyledFormMixin
from crm.core.middleware import client_ip
from crm.core.views import ensure_profile, staff_required

LOCK_AFTER = 5
LOCK_MINUTES = 15
NOTIFY_ADMIN_AFTER = 10


def _safe_next(request, nxt):
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return nxt
    return reverse('crm:home')


def _finish_login(request, user, nxt):
    from crm.models import UserSession
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session['crm_last_activity'] = timezone.now().timestamp()
    if not request.session.session_key:
        request.session.save()
    UserSession.objects.update_or_create(
        session_key=request.session.session_key,
        defaults={'user': user, 'ip': client_ip(request), 'user_agent': request.META.get('HTTP_USER_AGENT', '')[:300],
                  'last_seen_at': timezone.now(), 'ended_at': None})
    audit.security_event('login_success', user=user, request=request)
    audit.log('auth.login', user, actor=user)
    return redirect(_safe_next(request, nxt))


@never_cache
@ensure_csrf_cookie
def login_view(request):
    from crm.models import UserProfile
    if request.user.is_authenticated:
        return redirect('crm:home')
    ctx = {'next': request.GET.get('next', ''), 'username': ''}
    if request.method == 'POST':
        identifier = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        ctx.update(next=request.POST.get('next', ''), username=identifier)
        User = get_user_model()
        account = User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier)).order_by('id').first() \
            if identifier else None
        profile = ensure_profile(account) if account else None
        now = timezone.now()
        if profile and profile.locked_until and profile.locked_until > now:
            audit.security_event('login_failed', user=account, request=request, detail='attempt while locked')
            ctx['error'] = (f'This account is locked after too many failed attempts. Try again after '
                            f'{timezone.localtime(profile.locked_until):%H:%M}.')
            return render(request, 'crm/auth/login.html', ctx, status=403)
        user = authenticate(request, username=account.get_username(), password=password) if account else None
        if user is None:
            if profile:
                UserProfile.objects.filter(pk=profile.pk).update(failed_logins=F('failed_logins') + 1)
                profile.refresh_from_db(fields=['failed_logins'])
                if profile.failed_logins % LOCK_AFTER == 0:
                    profile.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    profile.save(update_fields=['locked_until'])
                    audit.security_event('lockout', user=account, request=request,
                                         detail=f'{profile.failed_logins} failed attempts')
                if profile.failed_logins == NOTIFY_ADMIN_AFTER:
                    notify.notify(notify.owners(), f'Repeated failed sign-ins for {account.get_username()}',
                                  body=f'{profile.failed_logins} failed attempts from {client_ip(request)}.',
                                  url=reverse('crm:security_events'), event_type='security', level='critical', email=True)
            audit.security_event('login_failed', user=account, username=identifier, request=request)
            ctx['error'] = 'That email or password is not right. Check both and try again.'
            return render(request, 'crm/auth/login.html', ctx, status=401)
        UserProfile.objects.filter(pk=profile.pk).update(failed_logins=0, locked_until=None)
        if profile.totp_enabled and profile.totp_secret:
            request.session['crm_2fa_user'] = user.pk
            request.session['crm_2fa_next'] = ctx['next']
            request.session['crm_2fa_at'] = now.timestamp()
            return redirect('crm:two_factor')
        return _finish_login(request, user, ctx['next'])
    return render(request, 'crm/auth/login.html', ctx)


@never_cache
@ensure_csrf_cookie
def two_factor_view(request):
    User = get_user_model()
    uid = request.session.get('crm_2fa_user')
    started = request.session.get('crm_2fa_at', 0)
    if not uid or timezone.now().timestamp() - started > 300:
        request.session.pop('crm_2fa_user', None)
        messages.warning(request, 'Your sign-in expired. Sign in again.')
        return redirect('crm:login')
    user = User.objects.filter(pk=uid, is_active=True).first()
    if user is None:
        return redirect('crm:login')
    ctx = {}
    if request.method == 'POST':
        profile = ensure_profile(user)
        if totp.verify(profile.totp_secret, request.POST.get('code', '').strip()):
            nxt = request.session.pop('crm_2fa_next', '')
            request.session.pop('crm_2fa_user', None)
            audit.security_event('two_factor', user=user, request=request, detail='verified')
            return _finish_login(request, user, nxt)
        audit.security_event('login_failed', user=user, request=request, detail='wrong 2FA code')
        ctx['error'] = 'That code is not right or has expired. Enter the current 6-digit code.'
    return render(request, 'crm/auth/two_factor.html', ctx)


@require_POST
def logout_view(request):
    from crm.models import UserSession
    if request.user.is_authenticated:
        audit.security_event('logout', user=request.user, request=request)
        if request.session.session_key:
            UserSession.objects.filter(session_key=request.session.session_key).update(ended_at=timezone.now())
    logout(request)
    messages.success(request, 'You have signed out.')
    return redirect('crm:login')


@staff_required
def home(request):
    """Default home per role (Part 3.2)."""
    profile = ensure_profile(request.user)
    if request.user.is_superuser:
        return redirect('crm:dashboard_owner')
    for role in profile.roles.all():
        try:
            return redirect(role.home)
        except Exception:
            continue
    return redirect('crm:my_day')


class CrmPasswordChangeForm(StyledFormMixin, PasswordChangeForm):
    pass


class TotpForm(StyledFormMixin, forms.Form):
    code = forms.CharField(max_length=6, min_length=6, label='6-digit code from your app')


def end_sessions(user, keep_key: str | None = None) -> int:
    from crm.models import UserSession
    keys = list(UserSession.objects.filter(user=user, ended_at__isnull=True).exclude(session_key=keep_key or '')
                .values_list('session_key', flat=True))
    Session.objects.filter(session_key__in=keys).delete()
    UserSession.objects.filter(session_key__in=keys).update(ended_at=timezone.now())
    return len(keys)


@staff_required
def profile_view(request):
    from crm.models import UserSession
    profile = ensure_profile(request.user)
    pw_form = CrmPasswordChangeForm(request.user)
    totp_form = TotpForm()
    action = request.POST.get('action') if request.method == 'POST' else None
    if action == 'password':
        pw_form = CrmPasswordChangeForm(request.user, request.POST)
        if pw_form.is_valid():
            user = pw_form.save()
            update_session_auth_hash(request, user)
            if profile.must_change_password:
                profile.must_change_password = False
                profile.save(update_fields=['must_change_password'])
            end_sessions(user, keep_key=request.session.session_key)
            audit.security_event('password_changed', user=user, request=request)
            messages.success(request, 'Password changed. Other devices were signed out.')
            return redirect('crm:profile')
    elif action == 'totp_start':
        request.session['crm_totp_pending'] = totp.new_secret()
        return redirect('crm:profile')
    elif action == 'totp_confirm':
        secret = request.session.get('crm_totp_pending')
        totp_form = TotpForm(request.POST)
        if secret and totp_form.is_valid() and totp.verify(secret, totp_form.cleaned_data['code']):
            profile.totp_secret = secret
            profile.totp_enabled = True
            profile.save(update_fields=['totp_secret', 'totp_enabled'])
            request.session.pop('crm_totp_pending', None)
            audit.security_event('two_factor', user=request.user, request=request, detail='enabled')
            messages.success(request, 'Two-factor sign-in is on.')
            return redirect('crm:profile')
        totp_form.add_error('code', 'That code is not right. Check your app and try again.')
    elif action == 'totp_disable':
        profile.totp_enabled = False
        profile.totp_secret = ''
        profile.save(update_fields=['totp_secret', 'totp_enabled'])
        audit.security_event('two_factor', user=request.user, request=request, detail='disabled')
        messages.success(request, 'Two-factor sign-in is off.')
        return redirect('crm:profile')
    elif action == 'signout_all':
        n = end_sessions(request.user, keep_key=request.session.session_key)
        audit.security_event('sessions_revoked', user=request.user, request=request, detail=f'{n} sessions')
        messages.success(request, f'Signed out of {n} other device{"s" if n != 1 else ""}.')
        return redirect('crm:profile')
    pending = request.session.get('crm_totp_pending')
    return render(request, 'crm/auth/profile.html', {
        'profile': profile, 'pw_form': pw_form, 'totp_form': totp_form, 'pending_secret': pending,
        'pending_uri': totp.provisioning_uri(pending, request.user.email or request.user.username) if pending else '',
        'sessions': UserSession.objects.filter(user=request.user, ended_at__isnull=True)[:20],
        'current_key': request.session.session_key,
        'roles': profile.roles.all(),
        'needs_2fa': permissions.has_perm(request.user, 'accounting', 'approve') or
        permissions.has_perm(request.user, 'payroll', 'view') or request.user.is_superuser,
    })
