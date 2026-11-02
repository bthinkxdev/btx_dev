"""
Sales-opportunity scoring for Google Places lead extraction.

We sell websites, e-commerce builds, and Meta marketing — so a business
being "collectible" is not the goal. The goal is finding businesses with
real commercial standing but a weak or missing digital presence, since
those are our best sales opportunities.

Four numbers, kept deliberately separate (never collapsed into one
unexplained score):

- business_quality_score:     is this a real, active, decent business?
- website_opportunity_score:  how much does it need a (better) website?
- meta_opportunity_score:     how much does it need Meta marketing help?
- overall_score:              weighted blend of the three, used to qualify.

A business is never rejected just for having no website or no social
presence — those raise opportunity scores, they don't lower qualification.
All thresholds/weights are named constants here, not scattered magic
numbers, so the rules can be tuned in one place.
"""

from __future__ import annotations

import re

from . import website_check

# ── Hard gates ──────────────────────────────────────────────────────────
CLOSED_PERMANENTLY = 'CLOSED_PERMANENTLY'
MIN_PHONE_DIGITS = 7  # a usable, callable number — not just "some text in the phone field"

# ── Qualification threshold ──────────────────────────────────────────────
# Qualification is gated on business quality alone — NOT on overall_score.
# A high-quality business with a great website and full social presence
# (low website/meta opportunity) must still qualify; opportunity scores are
# for prioritization, never a reason to reject a real, decent business.
MIN_BUSINESS_QUALITY_FOR_QUALIFICATION = 50

# ── high_hope thresholds (Lead.high_hope — only genuinely strong opportunities) ──
HIGH_HOPE_QUALITY_THRESHOLD = 70
HIGH_HOPE_OVERALL_THRESHOLD = 75

# ── business_quality_score point allocation (sums to 100) ──────────────
QUALITY_POINTS_NAME = 10
QUALITY_POINTS_ADDRESS = 10
QUALITY_POINTS_PHONE = 15
QUALITY_POINTS_STATUS_OPERATIONAL = 15
QUALITY_POINTS_STATUS_NEUTRAL = 5  # blank/CLOSED_TEMPORARILY — not penalized like a hard reject
QUALITY_POINTS_RATING_MAX = 30
QUALITY_POINTS_RATING_UNRATED = 10  # no rating yet is not the same as a bad rating
QUALITY_POINTS_REVIEWS_MAX = 20
QUALITY_REVIEWS_CAP = 50  # review_count at/above this earns full review points

# ── website_opportunity_score ───────────────────────────────────────────
WEBSITE_OPP_NO_SITE = 95  # none / unreachable / invalid / timeout — maximum opportunity
WEBSITE_OPP_HAS_SITE_BASE = 80
WEBSITE_OPP_DEDUCT_HTTPS = 10
WEBSITE_OPP_DEDUCT_MOBILE = 15
WEBSITE_OPP_DEDUCT_MODERN = 20
WEBSITE_OPP_DEDUCT_BASIC = 5
WEBSITE_OPP_DEDUCT_ECOMMERCE = 15
WEBSITE_OPP_FLOOR = 5

WEBSITE_OPP_LABEL_VERY_HIGH = 80
WEBSITE_OPP_LABEL_HIGH = 55
WEBSITE_OPP_LABEL_MEDIUM = 30
# below MEDIUM -> LOW

# ── meta_opportunity_score ──────────────────────────────────────────────
META_OPP_BASE_NO_PRESENCE = 70
META_OPP_BASE_SINGLE_PRESENCE = 55
META_OPP_BASE_MULTI_PRESENCE = 35
META_OPP_INACTIVE_BONUS = 15  # a confirmed-inactive profile is itself an opportunity signal
META_OPP_QUALITY_WEIGHT = 0.3  # nudges the base score toward/away from 50 by business quality

META_OPP_LABEL_VERY_HIGH = 80
META_OPP_LABEL_HIGH = 55
META_OPP_LABEL_MEDIUM = 30
# below MEDIUM -> LOW

# ── overall_score weights (sum to 1.0) ──────────────────────────────────
WEIGHT_BUSINESS_QUALITY = 0.35
WEIGHT_WEBSITE_OPPORTUNITY = 0.35
WEIGHT_META_OPPORTUNITY = 0.30

_DIGITS = re.compile(r'\d')


def is_usable_phone(phone: str) -> bool:
    digit_count = len(_DIGITS.findall(phone or ''))
    return digit_count >= MIN_PHONE_DIGITS


def _opportunity_label(score: int, very_high: int, high: int, medium: int) -> str:
    if score >= very_high:
        return 'very_high'
    if score >= high:
        return 'high'
    if score >= medium:
        return 'medium'
    return 'low'


def _business_quality_score(details: dict) -> int:
    score = 0
    if (details.get('name') or '').strip():
        score += QUALITY_POINTS_NAME
    if (details.get('address') or '').strip():
        score += QUALITY_POINTS_ADDRESS
    phone = details.get('nationalPhoneNumber') or details.get('internationalPhoneNumber') or ''
    if is_usable_phone(phone):
        score += QUALITY_POINTS_PHONE

    status = (details.get('businessStatus') or '').strip().upper()
    if status == 'OPERATIONAL':
        score += QUALITY_POINTS_STATUS_OPERATIONAL
    elif status in ('', 'CLOSED_TEMPORARILY'):
        score += QUALITY_POINTS_STATUS_NEUTRAL

    rating = details.get('rating')
    if rating is None:
        score += QUALITY_POINTS_RATING_UNRATED
    else:
        score += round(min(float(rating), 5) / 5 * QUALITY_POINTS_RATING_MAX)

    review_count = details.get('userRatingCount') or 0
    score += round(min(review_count, QUALITY_REVIEWS_CAP) / QUALITY_REVIEWS_CAP * QUALITY_POINTS_REVIEWS_MAX)

    return min(score, 100)


def _website_opportunity(website_result: dict) -> tuple[int, str]:
    status = website_result.get('status')
    if status != website_check.REACHABLE:
        # No usable website at all (or broken/timed out) is the strongest sales signal.
        score = WEBSITE_OPP_NO_SITE
    else:
        score = WEBSITE_OPP_HAS_SITE_BASE
        if website_result.get('https'):
            score -= WEBSITE_OPP_DEDUCT_HTTPS
        if website_result.get('mobile_responsive'):
            score -= WEBSITE_OPP_DEDUCT_MOBILE
        appearance = website_result.get('appearance')
        if appearance == 'modern':
            score -= WEBSITE_OPP_DEDUCT_MODERN
        elif appearance == 'basic':
            score -= WEBSITE_OPP_DEDUCT_BASIC
        if website_result.get('ecommerce_signal'):
            score -= WEBSITE_OPP_DEDUCT_ECOMMERCE
        score = max(score, WEBSITE_OPP_FLOOR)

    score = min(max(score, 0), 100)
    label = _opportunity_label(
        score, WEBSITE_OPP_LABEL_VERY_HIGH, WEBSITE_OPP_LABEL_HIGH, WEBSITE_OPP_LABEL_MEDIUM
    )
    return score, label


def _meta_opportunity(social_result: dict, business_quality_score: int) -> tuple[int, str]:
    presence = social_result.get('social_presence_type', 'none')
    if presence == 'none':
        base = META_OPP_BASE_NO_PRESENCE
    elif presence in ('instagram', 'facebook'):
        base = META_OPP_BASE_SINGLE_PRESENCE
    else:  # instagram_and_facebook / other / multiple
        base = META_OPP_BASE_MULTI_PRESENCE

    if social_result.get('social_activity') == 'inactive':
        base += META_OPP_INACTIVE_BONUS

    # Center the quality nudge on 50 so an average business leaves the base untouched.
    quality_bonus = round((business_quality_score - 50) * META_OPP_QUALITY_WEIGHT)
    score = min(max(base + quality_bonus, 0), 100)
    label = _opportunity_label(
        score, META_OPP_LABEL_VERY_HIGH, META_OPP_LABEL_HIGH, META_OPP_LABEL_MEDIUM
    )
    return score, label


def _rejected(reason: str, business_quality_score: int = 0) -> dict:
    return {
        'qualified': False,
        'high_hope': False,
        'business_quality_score': business_quality_score,
        'website_opportunity_score': None,
        'website_opportunity_label': 'unknown',
        'meta_opportunity_score': None,
        'meta_opportunity_label': 'unknown',
        'overall_score': 0,
        'reason': reason,
    }


def _website_desc(website_result: dict) -> str:
    status = website_result.get('status')
    if status == website_check.NONE:
        return 'no website'
    if status == website_check.REACHABLE:
        return 'an existing website'
    if status == website_check.TIMEOUT:
        return 'a website that timed out'
    if status == website_check.INVALID:
        return 'an invalid website link'
    return 'an unreachable website'


def _social_desc(social_result: dict) -> str:
    presence = social_result.get('social_presence_type', 'none')
    if presence == 'none':
        return 'no confirmed social presence'
    if presence == 'instagram_and_facebook':
        return 'Instagram & Facebook presence'
    return f'{presence} presence'


def _build_reason(
    qualified: bool, details: dict, category: str, website_result: dict,
    social_result: dict, quality: int,
) -> str:
    label = (category or 'business').strip() or 'business'
    rating = details.get('rating')
    rating_desc = f'{rating} rating' if rating is not None else 'no rating yet'
    reviews = details.get('userRatingCount') or 0

    if not qualified:
        return f'Weak {label} signal — {rating_desc}, {reviews} reviews; business quality below qualification threshold.'

    tone = 'Strong' if quality >= HIGH_HOPE_QUALITY_THRESHOLD else 'Active'
    return (
        f'{tone} {label} with {rating_desc}, {reviews} reviews, '
        f'{_website_desc(website_result)}, and {_social_desc(social_result)}.'
    )


def evaluate_business(details: dict, *, website_result: dict, social_result: dict, category: str = '') -> dict:
    """
    details: normalized dict from google_places.get_place_details().
    website_result: from website_check.check_website(details['website']).
    social_result: from online_presence.detect(...).

    Returns:
    {
        qualified: bool, high_hope: bool,
        business_quality_score: int,
        website_opportunity_score: int|None, website_opportunity_label: str,
        meta_opportunity_score: int|None, meta_opportunity_label: str,
        overall_score: int,
        reason: str,
    }
    """
    name = (details.get('name') or '').strip()
    status = (details.get('businessStatus') or '').strip().upper()
    phone = details.get('nationalPhoneNumber') or details.get('internationalPhoneNumber') or ''

    if not name:
        return _rejected('Missing business name/identity — unusable record.')
    if status == CLOSED_PERMANENTLY:
        return _rejected('Business is permanently closed.')

    quality = _business_quality_score(details)

    if not is_usable_phone(phone):
        # Scored for transparency, but a lead we can't call is not a sales lead.
        return _rejected('No valid, callable phone number on file.', business_quality_score=quality)

    website_score, website_label = _website_opportunity(website_result)
    meta_score, meta_label = _meta_opportunity(social_result, quality)
    overall = round(
        quality * WEIGHT_BUSINESS_QUALITY
        + website_score * WEIGHT_WEBSITE_OPPORTUNITY
        + meta_score * WEIGHT_META_OPPORTUNITY
    )

    qualified = quality >= MIN_BUSINESS_QUALITY_FOR_QUALIFICATION
    high_hope = (
        qualified
        and quality >= HIGH_HOPE_QUALITY_THRESHOLD
        and overall >= HIGH_HOPE_OVERALL_THRESHOLD
    )

    return {
        'qualified': qualified,
        'high_hope': high_hope,
        'business_quality_score': quality,
        'website_opportunity_score': website_score,
        'website_opportunity_label': website_label,
        'meta_opportunity_score': meta_score,
        'meta_opportunity_label': meta_label,
        'overall_score': overall,
        'reason': _build_reason(qualified, details, category, website_result, social_result, quality),
    }
