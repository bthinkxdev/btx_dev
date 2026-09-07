"""
Google Places API (New) client — Text Search + Place Details.

All Google Places HTTP calls live here. Views must not call requests/Google
directly — go through search_text() / get_place_details() so the API key,
field masks, and error handling stay in one place.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

from ..models import Place

logger = logging.getLogger(__name__)

_BASE_URL = 'https://places.googleapis.com/v1'

# Minimal field masks — keep discovery/detail calls cheap and predictable.
SEARCH_FIELD_MASK = (
    'places.id,places.displayName,places.formattedAddress,places.location,'
    'places.primaryType,nextPageToken'
)
DETAILS_FIELD_MASK = (
    'displayName,formattedAddress,nationalPhoneNumber,internationalPhoneNumber,'
    'rating,userRatingCount,websiteUri,businessStatus,primaryType,googleMapsUri'
)


class GooglePlacesError(Exception):
    """Base error for anything that goes wrong talking to Google Places."""


class GooglePlacesNotConfigured(GooglePlacesError):
    """GOOGLE_MAPS_API_KEY is missing."""


class GooglePlacesRequestError(GooglePlacesError):
    """Network failure or timeout reaching Google."""


class GooglePlacesAPIError(GooglePlacesError):
    """Google returned a non-2xx response."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GooglePlacesRateLimitError(GooglePlacesAPIError):
    """Google reported rate limiting / quota exceeded (HTTP 429)."""


class GooglePlacesNotFoundError(GooglePlacesError):
    """The requested place id does not exist."""


def is_configured() -> bool:
    return bool(_api_key())


def _api_key() -> str:
    return str(getattr(settings, 'GOOGLE_MAPS_API_KEY', '') or '').strip()


def _timeout() -> int:
    return int(getattr(settings, 'GOOGLE_PLACES_REQUEST_TIMEOUT', 15) or 15)


def _request(
    method: str, path: str, *, field_mask: str, json_body: dict | None = None
) -> dict:
    api_key = _api_key()
    if not api_key:
        raise GooglePlacesNotConfigured('GOOGLE_MAPS_API_KEY is not configured')

    url = f'{_BASE_URL}/{path}'
    headers = {
        'X-Goog-Api-Key': api_key,
        'X-Goog-FieldMask': field_mask,
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.request(
            method, url, headers=headers, json=json_body, timeout=_timeout()
        )
    except requests.exceptions.Timeout as exc:
        logger.error('Google Places request timed out: %s %s', method, path)
        raise GooglePlacesRequestError('Google Places request timed out') from exc
    except requests.exceptions.RequestException as exc:
        logger.error('Google Places request failed: %s %s — %s', method, path, exc)
        raise GooglePlacesRequestError('Could not reach Google Places API') from exc

    if resp.status_code == 404:
        raise GooglePlacesNotFoundError('Place not found')
    if resp.status_code == 429:
        raise GooglePlacesRateLimitError('Google Places rate limit exceeded', status_code=429)
    if resp.status_code in (401, 403):
        logger.error('Google Places auth/permission error %s: %s', resp.status_code, resp.text[:500])
        raise GooglePlacesAPIError('Google Places API key rejected', status_code=resp.status_code)
    if resp.status_code >= 400:
        logger.error('Google Places API error %s: %s', resp.status_code, resp.text[:500])
        raise GooglePlacesAPIError(
            f'Google Places API returned {resp.status_code}', status_code=resp.status_code
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise GooglePlacesAPIError('Google Places API returned an invalid response') from exc


def search_text_page(query: str, page_token: str = '') -> dict[str, Any]:
    """
    Text Search (New), one page. Returns:
    {'results': [{placeId, name, address, latitude, longitude, primaryType}, ...],
     'nextPageToken': str}  (empty string when there are no more pages)

    Pass the returned nextPageToken back in to fetch the next page of the SAME
    query/location — this is pagination, not query variations/district
    exhaustion (still explicitly deferred); it just stops artificially capping
    a run at Google's ~20-results-per-call limit.
    """
    query = (query or '').strip()
    if not query:
        return {'results': [], 'nextPageToken': ''}

    body: dict[str, Any] = {'textQuery': query}
    if page_token:
        body['pageToken'] = page_token

    data = _request('POST', 'places:searchText', field_mask=SEARCH_FIELD_MASK, json_body=body)
    results = []
    for p in data.get('places') or []:
        loc = p.get('location') or {}
        results.append({
            'placeId': p.get('id') or '',
            'name': (p.get('displayName') or {}).get('text', ''),
            'address': p.get('formattedAddress') or '',
            'latitude': loc.get('latitude'),
            'longitude': loc.get('longitude'),
            'primaryType': p.get('primaryType') or '',
        })
    return {'results': results, 'nextPageToken': data.get('nextPageToken') or ''}


def search_text(query: str) -> list[dict[str, Any]]:
    """First-page-only convenience wrapper (used by the manual /api/places/search/ endpoint)."""
    return search_text_page(query)['results']


def get_place_details(place_id: str) -> dict[str, Any]:
    """
    Place Details (New). Returns a normalized dict:
    {placeId, name, address, nationalPhoneNumber, internationalPhoneNumber,
     rating, userRatingCount, website, businessStatus, primaryType, mapsUri}
    """
    place_id = (place_id or '').strip()
    if not place_id:
        raise GooglePlacesNotFoundError('Place not found')

    data = _request('GET', f'places/{place_id}', field_mask=DETAILS_FIELD_MASK)
    return {
        'placeId': place_id,
        'name': (data.get('displayName') or {}).get('text', ''),
        'address': data.get('formattedAddress') or '',
        'nationalPhoneNumber': data.get('nationalPhoneNumber') or '',
        'internationalPhoneNumber': data.get('internationalPhoneNumber') or '',
        'rating': data.get('rating'),
        'userRatingCount': data.get('userRatingCount'),
        'website': data.get('websiteUri') or '',
        'businessStatus': data.get('businessStatus') or '',
        'primaryType': data.get('primaryType') or '',
        'mapsUri': data.get('googleMapsUri') or '',
    }


def upsert_place(details: dict[str, Any], *, latitude=None, longitude=None) -> Place:
    """
    Dedup-save a normalized Place Details result by google_place_id
    (create on first sighting, update on repeat). Shared by the manual
    place-details endpoint and the extraction service.
    """
    defaults = {
        'name': details['name'],
        'address': details['address'],
        'phone': details['nationalPhoneNumber'] or details['internationalPhoneNumber'],
        'website': details['website'],
        'rating': details['rating'],
        'review_count': details['userRatingCount'],
        'business_status': details['businessStatus'],
        'primary_type': details.get('primaryType') or '',
        'maps_uri': details.get('mapsUri') or '',
    }
    if latitude is not None:
        defaults['latitude'] = latitude
    if longitude is not None:
        defaults['longitude'] = longitude

    place, _created = Place.objects.update_or_create(
        google_place_id=details['placeId'], defaults=defaults
    )
    return place
