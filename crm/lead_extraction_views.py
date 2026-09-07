"""
Admin-only Google Places lead-extraction page: start/stop a run, and a
polling status endpoint the page's JS uses to update counters + live table
without a page reload.
"""

from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from .models import EmployeeProfile, ExtractionLead, ExtractionRun
from .rbac import ROLE_SALES, can_access_lead_extraction
from .services import lead_extraction


def _json_body(request) -> dict:
    try:
        return json.loads(request.body.decode('utf-8'))
    except Exception:
        return {}


def _forbidden():
    return HttpResponse(status=403)


def _executive_label(user) -> str:
    if not user:
        return ''
    return user.get_full_name() or user.get_username()


def _item_json(item: ExtractionLead) -> dict:
    return {
        'id': item.id,
        'business': item.business_name,
        'category': item.run.category,
        'phone': item.phone,
        'website': item.website,
        'websiteStatus': item.website_status,
        'instagram': item.instagram_url,
        'facebook': item.facebook_url,
        'rating': float(item.rating) if item.rating is not None else None,
        'reviews': item.review_count,
        'businessStatus': item.business_status,
        'websiteOpportunity': item.website_opportunity_label,
        'metaOpportunity': item.meta_opportunity_label,
        'overallScore': item.overall_score,
        'qualification': item.qualification_status,
        'reason': item.reason,
        'executive': _executive_label(item.assigned_to),
    }


def _run_json(run: ExtractionRun) -> dict:
    executives = (run.total_target_count // run.target_count) if run.target_count else 0
    return {
        'id': run.pk,
        'status': run.status,
        'location': run.location,
        'category': run.category,
        'discovered': run.discovered_count,
        'duplicates': run.duplicate_count,
        'invalid': run.invalid_count,
        'qualified': run.qualified_count,
        'assigned': run.assigned_count,
        'perExecutiveTarget': run.target_count,
        'eligibleExecutives': executives,
        'target': run.total_target_count,
        'remaining': run.remaining_target,
        'errorMessage': run.error_message,
    }


@login_required
def lead_extraction_page(request):
    if not can_access_lead_extraction(request.user):
        return _forbidden()

    eligible_count = EmployeeProfile.objects.filter(
        eligible_for_leads=True, crm_role=ROLE_SALES, user__is_active=True,
    ).count()
    recent_runs = ExtractionRun.objects.select_related('created_by').all()[:10]

    return render(
        request,
        'crm/lead_extraction.html',
        {
            'eligible_count': eligible_count,
            'recent_runs': recent_runs,
            'default_target': lead_extraction.DEFAULT_TARGET_COUNT,
            'max_target': lead_extraction.MAX_TARGET_COUNT,
            'default_total_target': lead_extraction.DEFAULT_TARGET_COUNT * eligible_count,
        },
    )


@login_required
@require_http_methods(['POST'])
def lead_extraction_start(request):
    if not can_access_lead_extraction(request.user):
        return _forbidden()

    body = _json_body(request)
    location = str(body.get('location') or '').strip()
    category = str(body.get('category') or '').strip()
    if not location or not category:
        return JsonResponse({'error': 'location and category are required'}, status=400)

    target = body.get('target')
    try:
        target = int(target) if target not in (None, '') else None
    except (TypeError, ValueError):
        return JsonResponse({'error': 'target must be a number'}, status=400)

    if not lead_extraction.eligible_executive_ids():
        return JsonResponse({'error': 'No eligible sales executives available.'}, status=400)

    if ExtractionRun.objects.filter(
        status__in=(ExtractionRun.Status.PENDING, ExtractionRun.Status.RUNNING, ExtractionRun.Status.STOPPING)
    ).exists():
        return JsonResponse({'error': 'An extraction run is already in progress.'}, status=409)

    run = lead_extraction.start_extraction(
        location=location, category=category, created_by=request.user, target_count=target,
    )
    return JsonResponse(_run_json(run))


@login_required
@require_http_methods(['POST'])
def lead_extraction_stop(request, run_id):
    if not can_access_lead_extraction(request.user):
        return _forbidden()

    run = get_object_or_404(ExtractionRun, pk=run_id)
    lead_extraction.request_stop(run.pk)
    run.refresh_from_db()
    return JsonResponse(_run_json(run))


@login_required
@require_http_methods(['GET'])
def lead_extraction_status(request, run_id):
    if not can_access_lead_extraction(request.user):
        return _forbidden()

    run = get_object_or_404(ExtractionRun, pk=run_id)
    after_id = request.GET.get('after') or 0
    try:
        after_id = int(after_id)
    except (TypeError, ValueError):
        after_id = 0

    items = list(
        run.items.select_related('assigned_to', 'run').filter(id__gt=after_id).order_by('id')[:200]
    )
    payload = _run_json(run)
    payload['new_leads'] = [_item_json(item) for item in items]
    return JsonResponse(payload)
