"""
Lead Finder runs: search -> dedup against history -> details -> analyse -> store.

Each SearchRun executes on a background daemon thread started from the view that handles "Start";
the status endpoint just reads SearchRun / RunResult from the DB, so polling works regardless of what
drives the run underneath. Nothing is created outside this app's own tables.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from ..models import Business, RunResult, SearchRun
from . import google_places, lead_qualification, online_presence, website_check

logger = logging.getLogger(__name__)

# Hard ceiling on candidates scanned in one run, independent of the target.
MAX_CANDIDATES_PER_RUN = 200
MAX_TARGET_COUNT = 200
DEFAULT_TARGET_COUNT = 50

# Google requires a brief pause before a nextPageToken becomes valid.
NEXT_PAGE_DELAY_SECONDS = 2

# Natural rewordings of the same search, tried once pagination for an earlier phrasing is exhausted.
# Results overlap heavily; the history dedup collapses that automatically.
_QUERY_VARIATION_TEMPLATES = (
    '{category} in {location}',
    'best {category} in {location}',
    '{category} shop in {location}',
    'top {category} in {location}',
)

_NON_DIGITS = re.compile(r'\D')


def _query_variations(category: str, location: str) -> list[str]:
    seen = set()
    queries = []
    for template in _QUERY_VARIATION_TEMPLATES:
        q = template.format(category=category, location=location).strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            queries.append(q)
    return queries


def phone_key(phone: str) -> str:
    digits = _NON_DIGITS.sub('', phone or '')
    return digits[-10:] if len(digits) >= 7 else ''


def has_active_run() -> bool:
    return SearchRun.objects.filter(status__in=SearchRun.ACTIVE_STATUSES).exists()


def start_run(*, location: str, category: str, target_count, user) -> SearchRun:
    try:
        target = int(target_count) if target_count else DEFAULT_TARGET_COUNT
    except (TypeError, ValueError):
        target = DEFAULT_TARGET_COUNT
    target = max(1, min(target, MAX_TARGET_COUNT))

    run = SearchRun.objects.create(
        location=(location or '').strip(), category=(category or '').strip(), target_count=target,
        created_by_id=getattr(user, 'pk', None),
        created_by_name=(user.get_full_name() or user.get_username()) if user else '',
    )
    threading.Thread(target=_run_safe, args=(run.pk,), daemon=True).start()
    return run


def request_stop(run_id: int) -> bool:
    updated = SearchRun.objects.filter(
        pk=run_id, status__in=(SearchRun.Status.PENDING, SearchRun.Status.RUNNING),
    ).update(stop_requested=True, status=SearchRun.Status.STOPPING)
    return bool(updated)


def _stop_requested(run_id: int) -> bool:
    return SearchRun.objects.filter(pk=run_id, stop_requested=True).exists()


def _safe_places_error(exc: Exception) -> str:
    if isinstance(exc, google_places.GooglePlacesNotConfigured):
        return 'Google Places is not configured (GOOGLE_MAPS_API_KEY missing).'
    if isinstance(exc, google_places.GooglePlacesRateLimitError):
        return 'Google Places rate limit reached — try again shortly.'
    if isinstance(exc, google_places.GooglePlacesRequestError):
        return 'Could not reach Google Places (network/timeout).'
    return 'Google Places search failed.'


def _bump(run: SearchRun, *fields: str) -> None:
    for f in fields:
        setattr(run, f, getattr(run, f) + 1)
    run.save(update_fields=list(fields))


def _record(run, outcome, *, name='', business=None, reason=''):
    RunResult.objects.create(run=run, business=business, outcome=outcome, name=(name or '')[:255],
                             reason=(reason or '')[:255])


def _find_existing(place_id: str = '', phone: str = ''):
    biz = Business.objects.filter(google_place_id=place_id).first() if place_id else None
    if biz is None and phone_key(phone):
        biz = Business.objects.filter(phone_key=phone_key(phone)).first()
    return biz


def _mark_seen(biz: Business) -> None:
    Business.objects.filter(pk=biz.pk).update(times_seen=F('times_seen') + 1, last_seen_at=timezone.now())


def _save_business(run, candidate, details, website_result, social_result, evaluation):
    phone = details.get('nationalPhoneNumber') or details.get('internationalPhoneNumber') or ''
    fields = dict(
        phone_key=phone_key(phone), name=(details.get('name') or candidate.get('name') or 'Unnamed business')[:255],
        category=run.category, search_location=run.location, address=(details.get('address') or '')[:500],
        phone=phone[:50], website=(details.get('website') or '')[:500], maps_uri=(details.get('mapsUri') or '')[:500],
        primary_type=(details.get('primaryType') or candidate.get('primaryType') or '')[:100],
        rating=details.get('rating'), review_count=details.get('userRatingCount'),
        business_status=(details.get('businessStatus') or '')[:40],
        latitude=candidate.get('latitude'), longitude=candidate.get('longitude'),
        website_status=website_result.get('status', ''),
        instagram_url=social_result.get('instagram_url', '')[:500],
        facebook_url=social_result.get('facebook_url', '')[:500],
        social_presence_type=social_result.get('social_presence_type', ''),
        qualified=bool(evaluation['qualified']),
        business_quality_score=evaluation.get('business_quality_score'),
        website_opportunity_score=evaluation.get('website_opportunity_score'),
        website_opportunity_label=evaluation.get('website_opportunity_label', Business.Opportunity.UNKNOWN),
        meta_opportunity_score=evaluation.get('meta_opportunity_score'),
        meta_opportunity_label=evaluation.get('meta_opportunity_label', Business.Opportunity.UNKNOWN),
        overall_score=evaluation.get('overall_score'), reason=(evaluation.get('reason') or '')[:255],
        first_run=run,
    )
    try:
        with transaction.atomic():
            return Business.objects.create(google_place_id=candidate['placeId'], **fields)
    except IntegrityError:
        return None  # another thread stored it first — treat as a duplicate


def _process_candidate(run, candidate) -> None:
    place_id = (candidate.get('placeId') or '').strip()
    if not place_id:
        return

    existing = _find_existing(place_id)
    if existing is not None:
        _mark_seen(existing)
        reason = (f'Already in history (first found in run #{existing.first_run_id}).'
                  if existing.first_run_id else 'Already in history.')
        _record(run, RunResult.Outcome.DUPLICATE, name=existing.name, business=existing, reason=reason)
        _bump(run, 'discovered_count', 'duplicate_count')
        return

    try:
        details = google_places.get_place_details(place_id)
    except google_places.GooglePlacesError as exc:
        _record(run, RunResult.Outcome.INVALID, name=candidate.get('name', ''),
                reason='Could not retrieve business details from Google.')
        _bump(run, 'discovered_count', 'invalid_count')
        logger.info('Lead Finder run %s: details fetch failed for %s: %s', run.pk, place_id, exc)
        return

    # Same business listed under a different place id — caught by phone number.
    phone = details.get('nationalPhoneNumber') or details.get('internationalPhoneNumber') or ''
    twin = _find_existing(phone=phone)
    if twin is not None:
        _mark_seen(twin)
        _record(run, RunResult.Outcome.DUPLICATE, name=twin.name, business=twin,
                reason='Same phone number as a business already in history.')
        _bump(run, 'discovered_count', 'duplicate_count')
        return

    website_result = website_check.check_website(details.get('website') or '')
    social_result = online_presence.detect(
        google_website_url=details.get('website') or '', website_html=website_result.get('html', ''),
    )
    evaluation = lead_qualification.evaluate_business(
        details, website_result=website_result, social_result=social_result, category=run.category,
    )

    business = _save_business(run, candidate, details, website_result, social_result, evaluation)
    if business is None:
        _record(run, RunResult.Outcome.DUPLICATE, name=details.get('name', ''), reason='Already in history.')
        _bump(run, 'discovered_count', 'duplicate_count')
        return

    if evaluation['qualified']:
        _record(run, RunResult.Outcome.NEW, name=business.name, business=business, reason=evaluation['reason'])
        _bump(run, 'discovered_count', 'new_count')
    else:
        _record(run, RunResult.Outcome.REJECTED, name=business.name, business=business, reason=evaluation['reason'])
        _bump(run, 'discovered_count', 'rejected_count')


def _finish(run, status, **extra):
    run.status = status
    for k, v in extra.items():
        setattr(run, k, v)
    run.save(update_fields=['status', *extra.keys()])


def _run_safe(run_id: int):
    close_old_connections()
    try:
        _run(run_id)
    except Exception:
        logger.exception('Lead Finder run %s crashed', run_id)
        SearchRun.objects.filter(pk=run_id).update(
            status=SearchRun.Status.FAILED, error_message='Search failed unexpectedly.', completed_at=timezone.now(),
        )
    finally:
        close_old_connections()


def _run(run_id: int):
    run = SearchRun.objects.get(pk=run_id)
    run.status = SearchRun.Status.RUNNING
    run.started_at = timezone.now()
    run.save(update_fields=['status', 'started_at'])

    processed = 0
    last_error = None
    seen_this_run: set[str] = set()  # same place returned by several query wordings counts once

    def stopped() -> bool:
        if _stop_requested(run.pk):
            _finish(run, SearchRun.Status.STOPPED, stopped_at=timezone.now())
            return True
        return False

    for query in _query_variations(run.category, run.location):
        if run.new_count >= run.target_count or processed >= MAX_CANDIDATES_PER_RUN:
            break
        page_token = ''
        while True:
            if stopped():
                return
            try:
                page = google_places.search_text_page(query, page_token=page_token)
            except google_places.GooglePlacesError as exc:
                last_error = exc
                logger.info('Lead Finder run %s: search failed for %r: %s', run.pk, query, exc)
                break
            if not page['results']:
                break

            for candidate in page['results']:
                if processed >= MAX_CANDIDATES_PER_RUN or run.new_count >= run.target_count:
                    break
                if stopped():
                    return
                pid = (candidate.get('placeId') or '').strip()
                if not pid or pid in seen_this_run:
                    continue
                seen_this_run.add(pid)
                _process_candidate(run, candidate)
                processed += 1

            if run.new_count >= run.target_count or processed >= MAX_CANDIDATES_PER_RUN:
                break
            if not page['nextPageToken']:
                break
            page_token = page['nextPageToken']
            time.sleep(NEXT_PAGE_DELAY_SECONDS)

    if run.discovered_count == 0 and last_error is not None:
        _finish(run, SearchRun.Status.FAILED, error_message=_safe_places_error(last_error),
                completed_at=timezone.now())
        return
    _finish(run, SearchRun.Status.COMPLETED, completed_at=timezone.now())
