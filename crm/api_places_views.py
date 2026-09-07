"""
Lead-generation Places API — thin JSON views over crm.services.google_places.

Called from the CRM UI by logged-in staff (session auth + CSRF), not by an
external service — unlike the Node-gateway endpoints in api_wa_views.py.
"""

from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from .rbac import can_access_sales_pipeline
from .services import google_places

logger = logging.getLogger(__name__)


def _json_body(request) -> dict:
    try:
        return json.loads(request.body.decode('utf-8'))
    except Exception:
        return {}


def _forbidden():
    return JsonResponse({'error': 'forbidden'}, status=403)


@login_required
@require_http_methods(['POST'])
def places_search(request):
    """POST {"query": "boutiques in Thiruvananthapuram"} -> {"results": [...]}"""
    if not can_access_sales_pipeline(request.user):
        return _forbidden()

    query = str(_json_body(request).get('query') or '').strip()
    if not query:
        return JsonResponse({'error': 'query is required'}, status=400)

    try:
        results = google_places.search_text(query)
    except google_places.GooglePlacesNotConfigured:
        logger.error('Places search attempted without GOOGLE_MAPS_API_KEY configured')
        return JsonResponse({'error': 'Places search is not configured'}, status=503)
    except google_places.GooglePlacesRateLimitError:
        return JsonResponse({'error': 'Places search is rate-limited, try again shortly'}, status=429)
    except google_places.GooglePlacesRequestError:
        return JsonResponse({'error': 'Could not reach Google Places'}, status=504)
    except google_places.GooglePlacesAPIError:
        return JsonResponse({'error': 'Places search failed'}, status=502)

    return JsonResponse({'results': results})


@login_required
@require_http_methods(['GET'])
def place_details(request, place_id):
    """
    GET -> fetch Place Details, then dedup-save into Place by google_place_id
    (create on first sighting, update on repeat), and return the normalized data.
    """
    if not can_access_sales_pipeline(request.user):
        return _forbidden()

    place_id = str(place_id or '').strip()
    if not place_id:
        return JsonResponse({'error': 'place_id is required'}, status=400)

    try:
        details = google_places.get_place_details(place_id)
    except google_places.GooglePlacesNotConfigured:
        logger.error('Place details attempted without GOOGLE_MAPS_API_KEY configured')
        return JsonResponse({'error': 'Place details is not configured'}, status=503)
    except google_places.GooglePlacesNotFoundError:
        return JsonResponse({'error': 'Place not found'}, status=404)
    except google_places.GooglePlacesRateLimitError:
        return JsonResponse({'error': 'Place details is rate-limited, try again shortly'}, status=429)
    except google_places.GooglePlacesRequestError:
        return JsonResponse({'error': 'Could not reach Google Places'}, status=504)
    except google_places.GooglePlacesAPIError:
        return JsonResponse({'error': 'Place details failed'}, status=502)

    place = google_places.upsert_place(details)

    return JsonResponse({
        'placeId': place.google_place_id,
        'name': place.name,
        'address': place.address,
        'phone': place.phone,
        'website': place.website,
        'rating': float(place.rating) if place.rating is not None else None,
        'reviewCount': place.review_count,
        'businessStatus': place.business_status,
    })
