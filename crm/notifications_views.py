"""
In-app notification history — so an executive can review what they were
pushed (and what they missed) without digging through their phone's
notification shade. Same NotificationEvent rows the push system already
logs; this is just a user-facing read/mark-read view over them.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import NotificationEvent

PER_PAGE = 30


@login_required
def notifications_list(request):
    qs = NotificationEvent.objects.filter(recipient=request.user).select_related('lead')
    paginator = Paginator(qs, PER_PAGE)
    page_obj = paginator.page(request.GET.get('page') or 1)

    # Opening the list is the "I've seen these" moment — mark this page read.
    unread_ids = [n.pk for n in page_obj.object_list if not n.read_at]
    if unread_ids:
        NotificationEvent.objects.filter(pk__in=unread_ids).update(read_at=timezone.now())

    return render(request, 'crm/notifications.html', {
        'page_obj': page_obj,
        'paginator': paginator,
    })


@login_required
@require_POST
def notifications_mark_all_read(request):
    NotificationEvent.objects.filter(recipient=request.user, read_at__isnull=True).update(
        read_at=timezone.now(),
    )
    if request.headers.get('HX-Request'):
        return HttpResponse(status=204)
    return redirect('crm:notifications')
