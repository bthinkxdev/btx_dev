"""
Lead Finder — admin-only. Start/stop a Google Places search, poll live results, browse history,
export to Excel or PDF. Never creates leads or touches any other app's data.
"""
from __future__ import annotations

import json
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods

from crm.core.permissions import is_super_admin

from .models import Business, RunResult, SearchRun
from .services import export, extraction, google_places


def admin_only(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not is_super_admin(request.user):
            return HttpResponseForbidden('Lead Finder is available to administrators only.')
        return view(request, *args, **kwargs)
    return wrapper


def _json_body(request) -> dict:
    try:
        return json.loads(request.body.decode('utf-8'))
    except Exception:
        return {}


def _result_json(r: RunResult) -> dict:
    b = r.business
    return {
        'id': r.id, 'outcome': r.outcome, 'reason': r.reason, 'name': r.name or (b.name if b else ''),
        'category': b.category if b else '', 'phone': b.phone if b else '', 'website': b.website if b else '',
        'websiteStatus': b.website_status if b else '', 'instagram': b.instagram_url if b else '',
        'facebook': b.facebook_url if b else '',
        'rating': float(b.rating) if b and b.rating is not None else None,
        'reviews': b.review_count if b else None,
        'websiteOpportunity': b.website_opportunity_label if b else 'unknown',
        'metaOpportunity': b.meta_opportunity_label if b else 'unknown',
        'overallScore': b.overall_score if b else None, 'mapsUri': b.maps_uri if b else '',
    }


def _run_json(run: SearchRun) -> dict:
    return {
        'id': run.pk, 'status': run.status, 'location': run.location, 'category': run.category,
        'discovered': run.discovered_count, 'newCount': run.new_count, 'duplicates': run.duplicate_count,
        'rejected': run.rejected_count, 'invalid': run.invalid_count, 'target': run.target_count,
        'remaining': run.remaining_target, 'errorMessage': run.error_message,
    }


@admin_only
def index(request):
    return render(request, 'leadfinder/index.html', {
        'recent_runs': SearchRun.objects.all()[:10],
        'default_target': extraction.DEFAULT_TARGET_COUNT,
        'max_target': extraction.MAX_TARGET_COUNT,
        'history_count': Business.objects.count(),
        'configured': google_places.is_configured(),
    })


@admin_only
@require_http_methods(['POST'])
def run_start(request):
    body = _json_body(request)
    location = str(body.get('location') or '').strip()
    category = str(body.get('category') or '').strip()
    if not location or not category:
        return JsonResponse({'error': 'location and category are required'}, status=400)
    try:
        target = int(body['target']) if body.get('target') not in (None, '') else None
    except (TypeError, ValueError):
        return JsonResponse({'error': 'target must be a number'}, status=400)
    if not google_places.is_configured():
        return JsonResponse({'error': 'GOOGLE_MAPS_API_KEY is not configured.'}, status=503)
    if extraction.has_active_run():
        return JsonResponse({'error': 'A search is already in progress.'}, status=409)
    run = extraction.start_run(location=location, category=category, target_count=target, user=request.user)
    return JsonResponse(_run_json(run))


@admin_only
@require_http_methods(['POST'])
def run_stop(request, run_id):
    run = get_object_or_404(SearchRun, pk=run_id)
    extraction.request_stop(run.pk)
    run.refresh_from_db()
    return JsonResponse(_run_json(run))


@admin_only
@require_http_methods(['GET'])
def run_status(request, run_id):
    run = get_object_or_404(SearchRun, pk=run_id)
    try:
        after_id = int(request.GET.get('after') or 0)
    except (TypeError, ValueError):
        after_id = 0
    items = run.results.select_related('business').filter(id__gt=after_id).order_by('id')[:200]
    payload = _run_json(run)
    payload['results'] = [_result_json(r) for r in items]
    return JsonResponse(payload)


@admin_only
def history(request):
    qs = Business.objects.all()
    q = (request.GET.get('q') or '').strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(address__icontains=q)
                       | Q(category__icontains=q) | Q(search_location__icontains=q))
    if request.GET.get('qualified') == '1':
        qs = qs.filter(qualified=True)
    page = Paginator(qs, 50).get_page(request.GET.get('page'))
    return render(request, 'leadfinder/history.html', {
        'page_obj': page, 'q': q, 'only_qualified': request.GET.get('qualified') == '1',
        'total': Business.objects.count(),
    })


def _export_response(request, businesses, title: str, filename: str):
    fmt = (request.GET.get('format') or 'xlsx').lower()
    stamp = filename
    if fmt == 'pdf':
        return _attachment(export.to_pdf(businesses, title), f'{stamp}.pdf', 'application/pdf')
    if fmt == 'xlsx':
        return _attachment(
            export.to_xlsx(businesses, title), f'{stamp}.xlsx',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    raise Http404('Unknown export format')


def _attachment(data: bytes, filename: str, content_type: str) -> HttpResponse:
    resp = HttpResponse(data, content_type=content_type)
    resp['Content-Disposition'] = f'attachment; filename="{filename}"'
    return resp


@admin_only
def export_run(request, run_id):
    """Businesses newly found by one run (qualified). ?all=1 adds the ones that were rejected."""
    run = get_object_or_404(SearchRun, pk=run_id)
    outcomes = [RunResult.Outcome.NEW]
    if request.GET.get('all') == '1':
        outcomes.append(RunResult.Outcome.REJECTED)
    ids = run.results.filter(outcome__in=outcomes, business__isnull=False).values_list('business_id', flat=True)
    businesses = Business.objects.filter(pk__in=ids).order_by('-overall_score', 'name')
    return _export_response(request, businesses, f'Lead Finder — {run.category} in {run.location} (run #{run.pk})',
                            f'leadfinder-run-{run.pk}-{slugify(run.category)}-{slugify(run.location)}')


@admin_only
def export_history(request):
    """Everything in history. ?qualified=1 limits to qualified businesses, ?q= applies the history search."""
    businesses = Business.objects.all()
    if request.GET.get('qualified') == '1':
        businesses = businesses.filter(qualified=True)
    q = (request.GET.get('q') or '').strip()
    if q:
        from django.db.models import Q
        businesses = businesses.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(address__icontains=q)
                                       | Q(category__icontains=q) | Q(search_location__icontains=q))
    return _export_response(request, businesses.order_by('-id'), 'Lead Finder — full history', 'leadfinder-history')
