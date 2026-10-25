"""
Request middleware for the CRM:

* request id + actor context for audit rows and logs
* idle timeout for staff sessions (Part 3.6)
* one-time form tokens: the first submission wins, a repeat gets the same
  result instead of a duplicate record (Part 2.2 rule 1)
"""
from __future__ import annotations

import contextvars
import logging
import uuid

from django.contrib import messages
from django.contrib.auth import logout
from django.db import IntegrityError, transaction
from django.http import HttpResponseRedirect
from django.utils import timezone

logger = logging.getLogger('crm')

_request_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar('crm_request_ctx', default={})

CRM_PREFIX = '/crm/'
PUBLIC_PREFIXES = ('/crm/pay/', '/crm/q/', '/crm/api/', '/crm/webhooks/', '/crm/login', '/crm/static/')
IDEMPOTENCY_FIELD = '_idem'


def current_context() -> dict:
    return _request_ctx.get()


def client_ip(request) -> str | None:
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    ip = forwarded.split(',')[0].strip() if forwarded else request.META.get('REMOTE_ADDR')
    return ip or None


class RequestIdLogFilter(logging.Filter):
    def filter(self, record):
        record.request_id = current_context().get('request_id', '-')
        return True


class CrmRequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get('HTTP_X_REQUEST_ID') or uuid.uuid4().hex[:16]
        request.request_id = request_id
        user = getattr(request, 'user', None)
        token = _request_ctx.set({
            'request_id': request_id,
            'ip': client_ip(request),
            'user': user if (user is not None and user.is_authenticated) else None,
            'channel': 'web',
        })
        try:
            if request.path.startswith(CRM_PREFIX):
                early = self._idle_timeout(request) or self._force_password_change(request)
                if early is not None:
                    return early
                idem = self._claim_idempotency(request)
                if isinstance(idem, HttpResponseRedirect):
                    return idem
                try:
                    response = self.get_response(request)
                except Exception:
                    self._release_idempotency(idem)
                    raise
                self._finish_idempotency(idem, response)
            else:
                response = self.get_response(request)
            response['X-Request-ID'] = request_id
            return response
        finally:
            _request_ctx.reset(token)

    # ---------------------------------------------------------- errors
    def process_exception(self, request, exception):
        if not request.path.startswith(CRM_PREFIX):
            return None
        from django.core.exceptions import PermissionDenied
        from django.http import Http404, JsonResponse
        from django.shortcuts import render
        is_json = request.headers.get('x-requested-with') == 'XMLHttpRequest'
        if isinstance(exception, PermissionDenied):
            message = str(exception) or 'You do not have access to this page.'
            if is_json:
                return JsonResponse({'ok': False, 'error': message}, status=403)
            return render(request, 'crm/errors/403.html', {'message': message}, status=403)
        if isinstance(exception, Http404):
            if is_json:
                return JsonResponse({'ok': False, 'error': 'Not found.'}, status=404)
            public = request.path.startswith(PUBLIC_PREFIXES)
            return render(request, 'crm/errors/404.html', {'public': public}, status=404)
        return None

    # ---------------------------------------------------------- idle timeout
    def _idle_timeout(self, request):
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated) or request.path.startswith(PUBLIC_PREFIXES):
            return None
        from crm.models import CompanySettings
        now = timezone.now()
        last = request.session.get('crm_last_activity')
        try:
            minutes = CompanySettings.get().idle_timeout_minutes or 30
        except Exception:  # database not migrated yet
            minutes = 30
        if last and now.timestamp() - float(last) > minutes * 60:
            logout(request)
            messages.warning(request, 'You were signed out after a period of inactivity. Sign in again to continue.')
            from django.urls import reverse
            return HttpResponseRedirect(reverse('crm:login') + '?next=' + request.path)
        # Write at most once a minute to avoid a session save on every request.
        if not last or now.timestamp() - float(last) > 60:
            request.session['crm_last_activity'] = now.timestamp()
        return None

    # ---------------------------------------------------------- forced password change
    def _force_password_change(self, request):
        """After an administrator resets access, the user must set their own password first."""
        user = getattr(request, 'user', None)
        if not (user and user.is_authenticated) or request.path.startswith(PUBLIC_PREFIXES):
            return None
        profile = getattr(user, 'crm_profile', None)
        if profile is None or not profile.must_change_password:
            return None
        from django.urls import reverse
        allowed = (reverse('crm:profile'), reverse('crm:logout'))
        if request.path in allowed:
            return None
        messages.warning(request, 'Set a new password before you continue.')
        return HttpResponseRedirect(reverse('crm:profile') + '#password')

    # ---------------------------------------------------------- idempotency
    def _claim_idempotency(self, request):
        if request.method != 'POST' or request.path.startswith(PUBLIC_PREFIXES):
            return None
        key = request.POST.get(IDEMPOTENCY_FIELD, '').strip()
        if not key or len(key) > 64:
            return None
        from crm.models import IdempotencyKey
        user = request.user if request.user.is_authenticated else None
        try:
            with transaction.atomic():
                return IdempotencyKey.objects.create(key=key, user=user, path=request.path[:300])
        except IntegrityError:
            existing = IdempotencyKey.objects.filter(key=key).first()
            if existing and existing.user_id == (user.pk if user else None):
                if existing.status == 'done' and existing.response_location:
                    messages.info(request, 'That was already submitted, so it was not repeated. Here is the result.')
                    return HttpResponseRedirect(existing.response_location)
                messages.warning(request, 'This is taking longer than usual. Check Recent activity before trying again.')
                return HttpResponseRedirect(request.META.get('HTTP_REFERER') or request.path)
            return None

    @staticmethod
    def _release_idempotency(idem):
        if idem is not None:
            try:
                idem.delete()
            except Exception:  # pragma: no cover
                logger.exception('Could not release idempotency key')

    def _finish_idempotency(self, idem, response):
        if idem is None:
            return
        if 300 <= response.status_code < 400 and response.get('Location'):
            idem.status = 'done'
            idem.response_location = response['Location'][:500]
            idem.save(update_fields=['status', 'response_location'])
        else:
            # Form re-rendered with errors (or a download): allow a corrected resubmission.
            self._release_idempotency(idem)
