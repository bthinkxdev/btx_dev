"""Make CSRF failures recoverable instead of a dead-end 403 page.

A form can post a stale or missing token when a page was restored from the browser's back/forward
cache, the cookie expired or was cleared while the tab stayed open, or an in-app browser (WhatsApp,
Instagram) dropped it. We cannot stop every cause, so:

* HTML pages under /crm/ are sent `no-store`, so a browser never replays a page whose token no
  longer matches its cookie (`NoStoreHtmlMiddleware`).
* when a token still fails, `failure_view` sends the person to a fresh page with a plain
  message and a newly issued cookie, instead of Django's "Forbidden (403)" screen.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.http import HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import resolve_url
from django.urls import reverse
from django.utils.cache import patch_cache_control
from django.utils.http import url_has_allowed_host_and_scheme

log = logging.getLogger('crm.csrf')

RETRY_MESSAGE = 'Your page had expired, so nothing was saved. Please try again.'


def _back_url(request):
    """Where to land after a failed post: the page they were on, else sign-in or home."""
    referer = request.META.get('HTTP_REFERER', '')
    if referer and url_has_allowed_host_and_scheme(
            referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return referer
    return resolve_url('crm:home' if request.user.is_authenticated else 'crm:login')


def failure_view(request, reason=''):
    log.warning('csrf failure path=%s reason=%s', request.path, reason)
    if not request.path.startswith('/crm/'):
        return HttpResponseForbidden('Session expired. Reload the page and try again.')
    if request.headers.get('X-Requested-With') or 'json' in request.headers.get('Content-Type', ''):
        return JsonResponse({'ok': False, 'error': RETRY_MESSAGE}, status=403)   # fetch() callers show it themselves
    get_token(request)  # asks CsrfViewMiddleware to issue a fresh cookie on this response
    try:
        messages.warning(request, RETRY_MESSAGE)
    except messages.MessageFailure:
        pass
    target = _back_url(request)
    if target.rstrip('/').endswith(reverse('crm:login').rstrip('/')) or request.path == reverse('crm:login'):
        target = reverse('crm:login')
    resp = HttpResponseRedirect(target)
    patch_cache_control(resp, no_store=True)
    return resp


class NoStoreHtmlMiddleware:
    """Never let a browser or proxy cache a CRM HTML page (it embeds a per-session CSRF token)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (request.method in ('GET', 'HEAD') and request.path.startswith('/crm/')
                and response.get('Content-Type', '').startswith('text/html')
                and 'Cache-Control' not in response):
            patch_cache_control(response, no_cache=True, no_store=True, must_revalidate=True, private=True)
        return response
