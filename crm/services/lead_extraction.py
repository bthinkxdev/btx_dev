"""
Google Places lead-extraction runs: search -> details -> qualify -> assign.

Each ExtractionRun is executed on a background daemon thread started from the
view that handles "Start Extraction" — no Celery/queue infra yet (explicitly
deferred). The status/live-table endpoints just read ExtractionRun /
ExtractionLead from the DB, so polling works regardless of what's driving the
run underneath.

Orchestration only — the actual analysis lives in dedicated services:
google_places (Google I/O), website_check (site reachability/quality),
online_presence (social detection), lead_qualification (scoring).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time

from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from ..models import EmployeeProfile, ExtractionLead, ExtractionRun, Lead, Place
from ..rbac import ROLE_SALES
from . import google_places, lead_qualification, online_presence, website_check

logger = logging.getLogger(__name__)

# Hard ceiling on Google Details API calls spent in one run, independent of
# target_count — without this, a run whose target massively exceeds what a
# location/category can realistically supply would keep paging/varying the
# query forever. Already-known duplicates are free (no Details call, see
# _process_candidate) and don't count against this — only real work does.
MAX_CANDIDATES_PER_RUN = 200

# Separate, looser ceiling on total candidates scanned INCLUDING free duplicate
# skips — the backstop for a location/category that's almost entirely mined
# out, so a run can't loop forever re-discovering the same known businesses.
MAX_TOTAL_SCANNED = 1000

# Google requires a brief pause before a nextPageToken becomes valid.
NEXT_PAGE_DELAY_SECONDS = 2

# Natural rewordings of the same search, tried in order once pagination for an
# earlier phrasing is exhausted. Google Text Search caps out around ~60
# results per exact query regardless of pagination, so a target well above
# that needs a differently-worded query to surface more of the same area —
# this is still one location/category, not real district/locality
# partitioning (still deferred), just enough breadth to approach a target a
# single phrasing can't reach on its own. Results overlap heavily across
# variations; the existing google_place_id dedup collapses that automatically.
_QUERY_VARIATION_TEMPLATES = (
    '{category} in {location}',
    'best {category} in {location}',
    '{category} shop in {location}',
    'top {category} in {location}',
)


def _query_variations(category: str, location: str) -> list[str]:
    seen = set()
    queries = []
    for template in _QUERY_VARIATION_TEMPLATES:
        q = template.format(category=category, location=location).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)
    return queries

LEAD_SOURCE = 'Google Places'

# Hard maximum qualified leads PER ELIGIBLE EXECUTIVE, per run — never let one
# executive receive lead #(MAX_TARGET_COUNT + 1) in a single run.
MAX_TARGET_COUNT = 50
DEFAULT_TARGET_COUNT = 50


def eligible_executive_ids() -> list[int]:
    """Active, sales-team, opted-in executives — in a stable order for round-robin assignment."""
    return list(
        EmployeeProfile.objects.filter(
            eligible_for_leads=True,
            crm_role=ROLE_SALES,
            user__is_active=True,
        ).order_by('user_id').values_list('user_id', flat=True)
    )


def start_extraction(*, location: str, category: str, created_by, target_count: int | None = None) -> ExtractionRun:
    try:
        target = int(target_count) if target_count else DEFAULT_TARGET_COUNT
    except (TypeError, ValueError):
        target = DEFAULT_TARGET_COUNT
    target = max(1, min(target, MAX_TARGET_COUNT))

    executive_count = len(eligible_executive_ids())
    run = ExtractionRun.objects.create(
        location=(location or '').strip(),
        category=(category or '').strip(),
        created_by=created_by,
        target_count=target,
        total_target_count=target * executive_count,
    )
    thread = threading.Thread(target=_run_safe, args=(run.pk,), daemon=True)
    thread.start()
    return run


def request_stop(run_id: int) -> bool:
    """Flip stop_requested; the worker checks it between candidates. Returns False if already finished."""
    updated = ExtractionRun.objects.filter(
        pk=run_id,
        status__in=(ExtractionRun.Status.PENDING, ExtractionRun.Status.RUNNING),
    ).update(stop_requested=True, status=ExtractionRun.Status.STOPPING)
    return bool(updated)


def _stop_requested(run_id: int) -> bool:
    return ExtractionRun.objects.filter(pk=run_id, stop_requested=True).exists()


def _next_available_executive(cycle, per_exec_counts: dict, per_exec_target: int):
    """
    Round-robin, skipping anyone who's already hit their per-executive quota.
    Returns None only once every executive in the pool is at quota.
    """
    for _ in range(len(per_exec_counts)):
        eid = next(cycle)
        if per_exec_counts[eid] < per_exec_target:
            return eid
    return None


def _safe_places_error(exc: Exception) -> str:
    if isinstance(exc, google_places.GooglePlacesNotConfigured):
        return 'Google Places is not configured.'
    if isinstance(exc, google_places.GooglePlacesRateLimitError):
        return 'Google Places rate limit reached — try again shortly.'
    if isinstance(exc, google_places.GooglePlacesRequestError):
        return 'Could not reach Google Places (network/timeout).'
    return 'Google Places search failed.'


def _record_item(
    run, place, *, qualification_status, reason,
    website_result=None, social_result=None, evaluation=None,
    details=None, lead=None, assigned_to_id=None,
):
    details = details or {}
    website_result = website_result or {}
    social_result = social_result or {}
    evaluation = evaluation or {}

    ExtractionLead.objects.create(
        run=run,
        place=place,
        lead=lead,
        business_name=details.get('name') or (place.name if place else ''),
        phone=(
            details.get('nationalPhoneNumber') or details.get('internationalPhoneNumber')
            or (place.phone if place else '')
        ),
        website=details.get('website') or (place.website if place else ''),
        rating=details.get('rating') if details.get('rating') is not None else (place.rating if place else None),
        review_count=(
            details.get('userRatingCount') if details.get('userRatingCount') is not None
            else (place.review_count if place else None)
        ),
        business_status=details.get('businessStatus') or (place.business_status if place else ''),
        website_status=website_result.get('status', ExtractionLead.WebsiteStatus.NONE),
        instagram_url=social_result.get('instagram_url', ''),
        facebook_url=social_result.get('facebook_url', ''),
        social_presence_type=social_result.get('social_presence_type', ExtractionLead.SocialPresenceType.NONE),
        social_activity=social_result.get('social_activity', ExtractionLead.SocialActivity.UNKNOWN),
        qualification_status=qualification_status,
        business_quality_score=evaluation.get('business_quality_score'),
        website_opportunity_score=evaluation.get('website_opportunity_score'),
        website_opportunity_label=evaluation.get('website_opportunity_label', ExtractionLead.OpportunityLevel.UNKNOWN),
        meta_opportunity_score=evaluation.get('meta_opportunity_score'),
        meta_opportunity_label=evaluation.get('meta_opportunity_label', ExtractionLead.OpportunityLevel.UNKNOWN),
        overall_score=evaluation.get('overall_score'),
        reason=(reason or '')[:255],
        assigned_to_id=assigned_to_id,
    )


def _build_lead_notes(details, evaluation, website_result, social_result) -> str:
    website_desc = 'No website' if website_result.get('status') == website_check.NONE else (
        details.get('website') or 'Unknown'
    )
    presence = social_result.get('social_presence_type', 'none')
    social_desc = {
        'none': 'Not found',
        'instagram': f"Instagram found ({social_result.get('instagram_url')})",
        'facebook': f"Facebook found ({social_result.get('facebook_url')})",
        'instagram_and_facebook': 'Instagram & Facebook found',
    }.get(presence, presence)

    return (
        'Google Places Lead\n\n'
        f"Business Quality: {evaluation.get('business_quality_score')}\n"
        f"Website Opportunity: {evaluation.get('website_opportunity_score')} "
        f"({evaluation.get('website_opportunity_label')})\n"
        f"Meta Opportunity: {evaluation.get('meta_opportunity_score')} "
        f"({evaluation.get('meta_opportunity_label')})\n"
        f"Overall Opportunity: {evaluation.get('overall_score')}\n\n"
        f"Website: {website_desc}\n"
        f"Social: {social_desc}\n"
        f"Rating: {details.get('rating') if details.get('rating') is not None else 'Not rated'}\n"
        f"Reviews: {details.get('userRatingCount') or 0}\n\n"
        f"Reason:\n{evaluation.get('reason', '')}"
    )


CANDIDATE_OK = 'ok'          # spent a Google Details call — counts against MAX_CANDIDATES_PER_RUN
CANDIDATE_SKIPPED = 'skip'   # already-known duplicate / junk row — free, doesn't eat the budget
CANDIDATE_STOP = 'stop'      # every executive is at quota — caller should stop the whole run


def _process_candidate(run, candidate, executive_cycle, per_exec_counts, per_exec_target) -> str:
    """
    Dedup -> details -> qualify -> assign for one discovered place.

    Already-known duplicates are recognized from our own Place table before
    ever calling Google Place Details, so re-encountering businesses a prior
    run already turned into leads costs nothing and doesn't count against
    MAX_CANDIDATES_PER_RUN — a heavily-mined location/category can burn
    through them cheaply and keep searching for something actually new,
    instead of a run's whole budget going to businesses we already have.
    """
    place_id = (candidate.get('placeId') or '').strip()
    if not place_id:
        return CANDIDATE_SKIPPED

    run.discovered_count += 1

    existing = Place.objects.filter(google_place_id=place_id).first()
    if existing is not None and Lead.objects.filter(place=existing).exists():
        run.duplicate_count += 1
        _record_item(
            run, existing,
            qualification_status=ExtractionLead.QualificationStatus.DUPLICATE,
            reason='Already extracted and assigned in a previous run.',
        )
        run.save(update_fields=['discovered_count', 'duplicate_count'])
        return CANDIDATE_SKIPPED

    try:
        details = google_places.get_place_details(place_id)
    except google_places.GooglePlacesError as exc:
        run.invalid_count += 1
        _record_item(
            run, existing,
            qualification_status=ExtractionLead.QualificationStatus.INVALID,
            reason='Could not retrieve business details from Google.',
            details={'name': candidate.get('name', '')},
        )
        run.save(update_fields=['discovered_count', 'invalid_count'])
        logger.info('Extraction run %s: details fetch failed for %s: %s', run.pk, place_id, exc)
        return CANDIDATE_OK

    place = google_places.upsert_place(
        details, latitude=candidate.get('latitude'), longitude=candidate.get('longitude'),
    )

    # Website + social analysis (bounded timeouts — one slow/broken site never stalls the run).
    website_result = website_check.check_website(details.get('website') or '')
    social_result = online_presence.detect(
        google_website_url=details.get('website') or '',
        website_html=website_result.get('html', ''),
    )

    evaluation = lead_qualification.evaluate_business(
        details, website_result=website_result, social_result=social_result, category=run.category,
    )

    if not evaluation['qualified']:
        run.invalid_count += 1
        _record_item(
            run, place, details=details, website_result=website_result, social_result=social_result,
            evaluation=evaluation, qualification_status=ExtractionLead.QualificationStatus.REJECTED,
            reason=evaluation['reason'],
        )
        run.save(update_fields=['discovered_count', 'invalid_count'])
        return CANDIDATE_OK

    executive_id = _next_available_executive(executive_cycle, per_exec_counts, per_exec_target)
    if executive_id is None:
        return CANDIDATE_STOP

    notes = _build_lead_notes(details, evaluation, website_result, social_result)
    lead = _create_and_assign_lead(place, details, executive_id, evaluation, notes)
    if lead is None:
        run.duplicate_count += 1
        _record_item(
            run, place,
            qualification_status=ExtractionLead.QualificationStatus.DUPLICATE,
            reason='Already extracted and assigned in a previous run.',
        )
        run.save(update_fields=['discovered_count', 'duplicate_count'])
        return CANDIDATE_OK

    per_exec_counts[executive_id] += 1
    run.qualified_count += 1
    run.assigned_count += 1
    _record_item(
        run, place, details=details, website_result=website_result, social_result=social_result,
        evaluation=evaluation, qualification_status=ExtractionLead.QualificationStatus.QUALIFIED,
        reason=evaluation['reason'], lead=lead, assigned_to_id=executive_id,
    )
    run.save(update_fields=['discovered_count', 'qualified_count', 'assigned_count'])
    return CANDIDATE_OK


def _create_and_assign_lead(place, details, executive_id, evaluation, notes):
    """Atomic create — the OneToOne on Lead.place is the DB-level duplicate guard."""
    try:
        with transaction.atomic():
            return Lead.objects.create(
                employee_id=executive_id,
                name=details.get('name') or place.name or 'Unnamed business',
                phone=(
                    details.get('nationalPhoneNumber') or details.get('internationalPhoneNumber')
                    or place.phone
                ),
                source=LEAD_SOURCE,
                status=Lead.Status.NEW,
                high_hope=bool(evaluation.get('high_hope')),
                notes=notes,
                place=place,
            )
    except IntegrityError:
        # Another run/thread already turned this place into a lead — race lost, not a bug.
        return None


def _run_safe(run_id: int):
    close_old_connections()
    try:
        _run(run_id)
    except Exception:
        logger.exception('Lead extraction run %s crashed', run_id)
        ExtractionRun.objects.filter(pk=run_id).update(
            status=ExtractionRun.Status.FAILED,
            error_message='Extraction failed unexpectedly.',
            completed_at=timezone.now(),
        )
    finally:
        close_old_connections()


def _run(run_id: int):
    run = ExtractionRun.objects.get(pk=run_id)

    executive_ids = eligible_executive_ids()
    if not executive_ids:
        run.status = ExtractionRun.Status.FAILED
        run.error_message = 'No eligible sales executives available.'
        run.completed_at = timezone.now()
        run.save(update_fields=['status', 'error_message', 'completed_at'])
        return
    executive_cycle = itertools.cycle(executive_ids)
    per_exec_counts = {eid: 0 for eid in executive_ids}
    per_exec_target = run.target_count

    run.status = ExtractionRun.Status.RUNNING
    run.started_at = timezone.now()
    # Recomputed against the executive pool actually used below — keeps total_target_count
    # consistent even in the rare case eligibility changed between "Start" and this thread running.
    run.total_target_count = per_exec_target * len(executive_ids)
    run.save(update_fields=['status', 'started_at', 'total_target_count'])

    queries = _query_variations(run.category, run.location)
    candidates_processed = 0  # real Details-API work — bounded by MAX_CANDIDATES_PER_RUN
    total_scanned = 0         # includes free duplicate skips — bounded by MAX_TOTAL_SCANNED
    last_error = None

    def _budget_exhausted():
        return (
            run.qualified_count >= run.total_target_count
            or candidates_processed >= MAX_CANDIDATES_PER_RUN
            or total_scanned >= MAX_TOTAL_SCANNED
        )

    for query in queries:
        if _budget_exhausted():
            break
        if _stop_requested(run.pk):
            run.status = ExtractionRun.Status.STOPPED
            run.stopped_at = timezone.now()
            run.save(update_fields=['status', 'stopped_at'])
            return

        page_token = ''
        while True:
            if _stop_requested(run.pk):
                run.status = ExtractionRun.Status.STOPPED
                run.stopped_at = timezone.now()
                run.save(update_fields=['status', 'stopped_at'])
                return

            try:
                page = google_places.search_text_page(query, page_token=page_token)
            except google_places.GooglePlacesError as exc:
                # Isolated to this query variation — try the next one rather than
                # failing the whole run over one bad/rate-limited phrasing.
                last_error = exc
                logger.info('Extraction run %s: search failed for %r: %s', run.pk, query, exc)
                break

            page_candidates = page['results']
            if not page_candidates:
                break

            for candidate in page_candidates:
                if _budget_exhausted():
                    break
                if _stop_requested(run.pk):
                    run.status = ExtractionRun.Status.STOPPED
                    run.stopped_at = timezone.now()
                    run.save(update_fields=['status', 'stopped_at'])
                    return

                result = _process_candidate(run, candidate, executive_cycle, per_exec_counts, per_exec_target)
                total_scanned += 1
                if result == CANDIDATE_OK:
                    candidates_processed += 1
                elif result == CANDIDATE_STOP:
                    break  # every executive is at quota — nothing left to assign

            if _budget_exhausted():
                break

            next_token = page['nextPageToken']
            if not next_token:
                break  # Google has no more results for this phrasing
            page_token = next_token
            time.sleep(NEXT_PAGE_DELAY_SECONDS)

    if run.discovered_count == 0 and last_error is not None:
        # Every query variation failed and we never got a single result — a real failure,
        # not just "this location/category is small." Otherwise, even a partial/empty
        # result set is a legitimate completed run (nothing qualified is not the same as broken).
        run.status = ExtractionRun.Status.FAILED
        run.error_message = _safe_places_error(last_error)
        run.completed_at = timezone.now()
        run.save(update_fields=['status', 'error_message', 'completed_at'])
        return

    run.status = ExtractionRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.save(update_fields=['status', 'completed_at'])
