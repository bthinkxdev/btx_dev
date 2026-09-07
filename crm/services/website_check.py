"""
Lightweight website reachability + quality check for lead qualification.

Not a design audit or a headless-browser crawler — a handful of concrete,
verifiable signals read from the HTML we already fetch to decide reachability,
so we never claim "mobile friendly" / "modern" / "e-commerce" without having
actually looked for evidence of it.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 3
READ_TIMEOUT = 5
MAX_BYTES = 200_000  # cap how much of the page we read — no slow-site hang, no huge download

USER_AGENT = 'Mozilla/5.0 (compatible; LeadExtractionBot/1.0)'

SOCIAL_DOMAINS = (
    'facebook.com', 'fb.com', 'instagram.com', 'wa.me', 'whatsapp.com',
    'twitter.com', 'x.com', 'linkedin.com', 'youtube.com', 'pinterest.com',
    'threads.net',
)

NONE = 'none'
REACHABLE = 'reachable'
UNREACHABLE = 'unreachable'
INVALID = 'invalid'
TIMEOUT = 'timeout'

_ECOMMERCE_SIGNALS = re.compile(
    r'add[\s-]?to[\s-]?cart|checkout|shopping[\s-]?cart|cdn\.shopify\.com|woocommerce|'
    r'shopify|/cart\.js|buy[\s-]?now',
    re.IGNORECASE,
)
_VIEWPORT_META = re.compile(r'<meta[^>]+name=["\']viewport["\']', re.IGNORECASE)
_MODERN_MARKERS = re.compile(r'tailwind|bootstrap|nextjs|__next|react|<!doctype html>', re.IGNORECASE)
_OUTDATED_MARKERS = re.compile(r'<font[\s>]|<marquee|<center>|frameset', re.IGNORECASE)
_PARKED_DOMAIN_MARKERS = re.compile(
    r'domain (is )?for sale|buy this domain|this domain is parked|godaddy\.com/domain',
    re.IGNORECASE,
)


def is_social_domain(url: str) -> bool:
    host = (urlparse(url).netloc or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return any(host == d or host.endswith('.' + d) for d in SOCIAL_DOMAINS)


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


def check_website(url: str) -> dict:
    """
    Fetch + classify a business website. Returns:
    {
        status: none|reachable|unreachable|invalid|timeout,
        https: True/False/None,
        mobile_responsive: True/False/None,   # None = not checked (page not fetched)
        appearance: 'modern'|'basic'|'outdated'/None,
        ecommerce_signal: True/False/None,
        broken: bool,
        html: str,   # bounded snippet, '' if not fetched — reused by online_presence
    }
    Never raises — a bad/slow site becomes a status, not an exception.
    """
    url = (url or '').strip()
    result = {
        'status': NONE, 'https': None, 'mobile_responsive': None,
        'appearance': None, 'ecommerce_signal': None, 'broken': False, 'html': '',
    }
    if not url:
        return result

    if not _is_valid_http_url(url):
        result['status'] = INVALID
        return result

    try:
        resp = requests.get(
            url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), allow_redirects=True, stream=True,
            headers={'User-Agent': USER_AGENT},
        )
    except requests.exceptions.Timeout:
        result['status'] = TIMEOUT
        return result
    except requests.exceptions.RequestException as exc:
        logger.info('Website check failed for %s: %s', url, exc)
        result['status'] = UNREACHABLE
        return result

    try:
        final_url = resp.url or url
        result['https'] = urlparse(final_url).scheme == 'https'

        if resp.status_code >= 400:
            result['status'] = UNREACHABLE
            resp.close()
            return result

        html_bytes = b''
        for chunk in resp.iter_content(chunk_size=8192):
            html_bytes += chunk
            if len(html_bytes) >= MAX_BYTES:
                break
        resp.close()
        html = html_bytes.decode('utf-8', errors='ignore')
        result['html'] = html

        if _PARKED_DOMAIN_MARKERS.search(html):
            result['status'] = UNREACHABLE
            result['broken'] = True
            return result

        result['status'] = REACHABLE
        result['mobile_responsive'] = bool(_VIEWPORT_META.search(html))
        result['ecommerce_signal'] = bool(_ECOMMERCE_SIGNALS.search(html))

        if _OUTDATED_MARKERS.search(html):
            result['appearance'] = 'outdated'
        elif _MODERN_MARKERS.search(html) and result['mobile_responsive']:
            result['appearance'] = 'modern'
        else:
            result['appearance'] = 'basic'

        return result
    except requests.exceptions.RequestException as exc:
        logger.info('Website content read failed for %s: %s', url, exc)
        result['status'] = UNREACHABLE
        return result
