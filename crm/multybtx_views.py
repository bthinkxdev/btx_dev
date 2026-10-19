"""
MultyBTX: admin-only CRM view of the Navaratri-offer onboarding page's
signups (pages.models.OfferSignup) — who paid, who's pending, who dropped
off, and how many of the 50 seats are taken. Read-only; the actual payment
state is only ever changed by pages.offer_views (Cashfree return/webhook).
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.db.models import Q, Sum
from django.shortcuts import render

from pages.models import OfferSignup
from pages.offer_views import NAVRATRI_SEAT_LIMIT

from .rbac import admin_required

PER_PAGE = 25


@login_required
@admin_required
def multybtx_list(request):
    qs = OfferSignup.objects.all().order_by('-created_at')

    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(phone__icontains=q)
            | Q(business_name__icontains=q) | Q(place__icontains=q)
        )

    status = (request.GET.get('status') or '').strip()
    if status not in dict(OfferSignup.Status.choices):
        status = ''
    if status:
        qs = qs.filter(status=status)

    plan = (request.GET.get('plan') or '').strip()
    if plan not in dict(OfferSignup.Plan.choices):
        plan = ''
    if plan:
        qs = qs.filter(plan=plan)

    paginator = Paginator(qs, PER_PAGE)
    page_raw = (request.GET.get('page') or '1').strip()
    try:
        page_obj = paginator.page(page_raw)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)

    all_signups = OfferSignup.objects.all()
    paid_qs = all_signups.filter(status=OfferSignup.Status.PAID)
    stats = {
        'total': all_signups.count(),
        'paid': paid_qs.count(),
        'pending': all_signups.filter(status=OfferSignup.Status.PENDING).count(),
        'failed': all_signups.filter(status=OfferSignup.Status.FAILED).count(),
        'revenue': paid_qs.aggregate(s=Sum('amount'))['s'] or 0,
        'seat_limit': NAVRATRI_SEAT_LIMIT,
        'seats_left': max(0, NAVRATRI_SEAT_LIMIT - paid_qs.count()),
    }

    return render(request, 'crm/multybtx.html', {
        'page_obj': page_obj,
        'paginator': paginator,
        'status_choices': OfferSignup.Status.choices,
        'plan_choices': OfferSignup.Plan.choices,
        'current_q': q,
        'current_status': status,
        'current_plan': plan,
        'stats': stats,
    })
