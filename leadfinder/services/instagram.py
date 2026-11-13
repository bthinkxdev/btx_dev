"""
Instagram profile enrichment through an Apify actor (default: danek/instagram-profiles-scraper-ppr).

All Apify HTTP calls live here. The token comes from settings.APIFY_API_TOKEN and is sent in a header,
never in the URL. The actor is paid-per-result, so callers batch usernames and cap the batch size.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_BASE_URL = 'https://api.apify.com/v2'
REQUEST_TIMEOUT = 290          # the run-sync endpoint itself gives up at 300 s
MAX_PROFILES_PER_RUN = 100     # cost cap per search run (~US$0.002 per profile)
MIN_POSTS_FOR_ACTIVE = 10      # fewer posts than this = barely-used profile (we get no last-post date)

_RESERVED_PATHS = {'p', 'reel', 'reels', 'tv', 'explore', 'accounts', 'stories', 'direct', 'about', 'developers', 'share'}
_USERNAME_URL = re.compile(r'instagram\.com/([A-Za-z0-9_.]+)', re.IGNORECASE)


class InstagramEnrichmentError(Exception):
    """Apify could not be reached or rejected the request."""


def _token() -> str:
    return str(getattr(settings, 'APIFY_API_TOKEN', '') or '').strip()


def _actor_id() -> str:
    return str(getattr(settings, 'APIFY_INSTAGRAM_ACTOR_ID', '') or '').strip()


def is_configured() -> bool:
    return bool(_token() and _actor_id())


def username_from_url(url: str) -> str:
    """'https://www.instagram.com/shop.name/?hl=en' -> 'shop.name'. Post/reel/explore links give ''."""
    m = _USERNAME_URL.search(url or '')
    if not m:
        return ''
    name = m.group(1).strip('.')
    return '' if name.lower() in _RESERVED_PATHS else name.lower()


def fetch_profiles(usernames: list[str]) -> dict[str, dict[str, Any]]:
    """
    Run the actor once for all usernames. Returns {username: normalized profile}.
    Usernames Apify returned nothing for are simply absent. Raises InstagramEnrichmentError on a failed call.
    """
    usernames = list(dict.fromkeys(u for u in usernames if u))[:MAX_PROFILES_PER_RUN]
    if not usernames:
        return {}
    if not is_configured():
        raise InstagramEnrichmentError('Apify is not configured (APIFY_API_TOKEN / APIFY_INSTAGRAM_ACTOR_ID).')

    url = f'{_BASE_URL}/acts/{_actor_id()}/run-sync-get-dataset-items'
    try:
        resp = requests.post(url, json={'usernames': usernames}, timeout=REQUEST_TIMEOUT,
                             headers={'Authorization': f'Bearer {_token()}'})
    except requests.exceptions.RequestException as exc:
        logger.error('Apify request failed: %s', exc)
        raise InstagramEnrichmentError('Could not reach Apify.') from exc

    if resp.status_code in (401, 403):
        raise InstagramEnrichmentError('Apify rejected the API token.')
    if resp.status_code >= 400:
        logger.error('Apify error %s: %s', resp.status_code, resp.text[:300])
        raise InstagramEnrichmentError(f'Apify returned {resp.status_code}.')
    try:
        items = resp.json()
    except ValueError as exc:
        raise InstagramEnrichmentError('Apify returned an invalid response.') from exc

    profiles: dict[str, dict[str, Any]] = {}
    for item in items if isinstance(items, list) else []:
        profile = normalize(item)
        if profile:
            profiles[profile['username']] = profile
    return profiles


def normalize(item: dict) -> dict[str, Any] | None:
    if not isinstance(item, dict) or item.get('error') or not item.get('username'):
        return None
    posts = item.get('media_count')
    return {
        'username': str(item['username']).lower(),
        'followers': item.get('follower_count'),
        'posts': posts,
        'is_business': bool(item.get('is_business')),
        'is_private': bool(item.get('is_private')),
        'bio': (item.get('biography') or '')[:500],
        'category': item.get('category') or '',
        'email': item.get('public_email') or item.get('biography_email') or '',
        'phone': item.get('public_phone_number') or item.get('contact_phone_number') or '',
        'external_url': item.get('external_url') or '',
    }


def activity_of(profile: dict) -> str:
    """'inactive' for a barely-used profile, else 'active'. Posts count only — the actor gives no last-post date."""
    posts = profile.get('posts')
    if posts is None:
        return 'unknown'
    return 'inactive' if posts < MIN_POSTS_FOR_ACTIVE else 'active'
