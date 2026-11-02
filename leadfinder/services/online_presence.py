"""
Social/online-presence detection for lead-generation qualification.

Deliberately narrow: only two legitimate, already-available sources feed
this — no fabricated data, no calls to Instagram/Facebook APIs we hold no
credentials for.

  1. The business's own "website" field on Google, when that URL IS itself
     a social profile link (common for small local businesses).
  2. Social links found in the HTML of the business's real website — reused
     from website_check.check_website()'s fetch, no second HTTP request.

social_activity stays 'unknown' in this basic implementation: we have no
permitted source that tells us whether a profile is actively posted to.
Extending this to a real Instagram/Facebook lookup later is a matter of
adding another source function here — callers only see detect().
"""

from __future__ import annotations

import re

from .website_check import is_social_domain

_INSTAGRAM_LINK = re.compile(r'https?://(www\.)?instagram\.com/[A-Za-z0-9_.\-/]+', re.IGNORECASE)
_FACEBOOK_LINK = re.compile(r'https?://(www\.)?(facebook|fb)\.com/[A-Za-z0-9_.\-/]+', re.IGNORECASE)

NONE = 'none'
INSTAGRAM = 'instagram'
FACEBOOK = 'facebook'
INSTAGRAM_AND_FACEBOOK = 'instagram_and_facebook'


def _first_match(pattern: re.Pattern, text: str) -> str:
    m = pattern.search(text or '')
    return m.group(0) if m else ''


def detect(*, google_website_url: str = '', website_html: str = '') -> dict:
    """Returns {instagram_url, facebook_url, social_presence_type, social_activity}."""
    instagram_url = ''
    facebook_url = ''

    google_website_url = (google_website_url or '').strip()
    if google_website_url and is_social_domain(google_website_url):
        lower = google_website_url.lower()
        if 'instagram.com' in lower:
            instagram_url = google_website_url
        elif 'facebook.com' in lower or 'fb.com' in lower:
            facebook_url = google_website_url

    if website_html:
        if not instagram_url:
            instagram_url = _first_match(_INSTAGRAM_LINK, website_html)
        if not facebook_url:
            facebook_url = _first_match(_FACEBOOK_LINK, website_html)

    if instagram_url and facebook_url:
        presence_type = INSTAGRAM_AND_FACEBOOK
    elif instagram_url:
        presence_type = INSTAGRAM
    elif facebook_url:
        presence_type = FACEBOOK
    else:
        presence_type = NONE

    return {
        'instagram_url': instagram_url,
        'facebook_url': facebook_url,
        'social_presence_type': presence_type,
        'social_activity': 'unknown',
    }
