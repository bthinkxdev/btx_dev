"""
PWA plumbing for the CRM: the manifest, the service worker (served from a
view — not a static file — so its default scope is /crm/, not /static/crm/),
and the subscribe/unsubscribe/test endpoints the browser-side JS talks to.
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST

from .models import NotificationEvent, PushSubscription
from .push import send_push_to_user


def manifest_json(request):
    data = {
        'name': 'BThinkX CRM',
        'short_name': 'BTX CRM',
        'description': 'BThinkX internal CRM — leads, follow-ups and pipeline.',
        'start_url': '/crm/',
        'scope': '/crm/',
        'display': 'standalone',
        'background_color': '#000000',
        'theme_color': '#000000',
        'orientation': 'portrait-primary',
        'icons': [
            {'src': '/static/assets/images/pwa/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/assets/images/pwa/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/static/assets/images/pwa/icon-maskable-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'maskable'},
            {'src': '/static/assets/images/pwa/icon-maskable-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    }
    return JsonResponse(data)


_SERVICE_WORKER_JS = """
// BThinkX CRM service worker — push delivery + notification click handling.
// Deliberately no offline/asset caching: the CRM always needs fresh data,
// this worker's only job is to keep receiving push while the site is closed.

self.addEventListener('install', (event) => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    data = { title: 'BThinkX CRM', body: event.data ? event.data.text() : '' };
  }
  const title = data.title || 'BThinkX CRM';
  const options = {
    body: data.body || '',
    icon: '/static/assets/images/pwa/icon-192.png',
    badge: '/static/assets/images/pwa/icon-192.png',
    tag: data.tag || 'btx-crm',
    data: { url: data.url || '/crm/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/crm/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes(url) && 'focus' in client) return client.focus();
      }
      for (const client of clients) {
        if ('focus' in client) {
          client.focus();
          if ('navigate' in client) client.navigate(url);
          return;
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});
""".strip()


def service_worker_js(request):
    return HttpResponse(_SERVICE_WORKER_JS, content_type='application/javascript')


@login_required
@require_POST
def push_subscribe(request):
    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON'}, status=400)

    endpoint = (payload.get('endpoint') or '').strip()
    keys = payload.get('keys') or {}
    p256dh = (keys.get('p256dh') or '').strip()
    auth = (keys.get('auth') or '').strip()
    if not (endpoint and p256dh and auth):
        return JsonResponse({'ok': False, 'error': 'Missing subscription fields'}, status=400)

    sub, _created = PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            'user': request.user,
            'p256dh': p256dh,
            'auth': auth,
            'user_agent': (request.META.get('HTTP_USER_AGENT') or '')[:255],
        },
    )
    return JsonResponse({'ok': True})


@login_required
@require_POST
def push_unsubscribe(request):
    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        payload = {}
    endpoint = (payload.get('endpoint') or '').strip()
    if endpoint:
        PushSubscription.objects.filter(endpoint=endpoint, user=request.user).delete()
    return JsonResponse({'ok': True})


@login_required
@require_POST
def push_test(request):
    """'this will be test' — a way for anyone to confirm push actually reaches
    their own device before relying on the real triggers. Reports whether
    delivery actually succeeded, not just whether a subscription exists."""
    if not PushSubscription.objects.filter(user=request.user).exists():
        return JsonResponse({'ok': False, 'error': 'No subscription on this device yet — enable notifications first.'}, status=400)

    send_push_to_user(
        request.user,
        kind=NotificationEvent.Kind.TEST,
        title='Test notification',
        body='If you can see this, push notifications are working.',
        url='/crm/',
    )
    last = NotificationEvent.objects.filter(
        recipient=request.user, kind=NotificationEvent.Kind.TEST,
    ).order_by('-created_at').first()
    if last and not last.is_success:
        return JsonResponse({'ok': False, 'error': last.error or 'Delivery failed.'}, status=502)
    return JsonResponse({'ok': True})
