"""Static (git-deployable) blog posts.

These render from template + code, not the database, so they ship on a
normal `git push` with no dependency on `media/` (gitignored, per this
project's convention) or on running a seed command against the live
deployment. Content is shared with pages/blog_content.py so nothing is
duplicated.

Add future posts either here (static, if you want them guaranteed to
ship with the code) or as normal `BlogPost` rows via /admin/ (dynamic —
see pages/management/commands/seed_blog_posts.py for the same idea
applied to the database). `pages.views.blog_post` checks this registry
first and falls back to the database, so both kinds of post live at the
same /blog/<slug>/ URL structure and appear together on /blog/.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone

from django.templatetags.static import static
from django.urls import reverse
from django.utils.text import slugify

from .blog_content import ARTICLES

# Fixed publish dates (not "now - N days", so they don't drift on every
# server restart). Matches the order/spacing the original seed produced.
_PUBLISHED_DATES = [
    datetime(2026, 8, 7, tzinfo=dt_timezone.utc),
    datetime(2026, 8, 11, tzinfo=dt_timezone.utc),
    datetime(2026, 8, 15, tzinfo=dt_timezone.utc),
    datetime(2026, 8, 19, tzinfo=dt_timezone.utc),
    datetime(2026, 8, 23, tzinfo=dt_timezone.utc),
    datetime(2026, 8, 27, tzinfo=dt_timezone.utc),
    datetime(2026, 8, 31, tzinfo=dt_timezone.utc),
    datetime(2026, 9, 4, tzinfo=dt_timezone.utc),
]


@dataclass(frozen=True)
class _StaticImage:
    _url: str

    @property
    def url(self) -> str:
        return self._url


@dataclass(frozen=True)
class StaticPost:
    """Duck-types the subset of BlogPost that templates/views rely on."""

    slug: str
    title: str
    category: str
    category_slug: str
    excerpt: str
    body: str
    meta_description: str
    image_path: str
    published_at: datetime
    updated_at: datetime
    read_time_minutes: int
    is_featured: bool = False

    @property
    def featured_image(self) -> _StaticImage:
        return _StaticImage(static(self.image_path))

    def get_meta_description(self) -> str:
        return self.meta_description

    def get_absolute_url(self) -> str:
        return reverse('pages:blog_post', args=[self.slug])


def _build_static_posts() -> list[StaticPost]:
    ecom_url = reverse('pages:service_ecommerce')
    dm_url = reverse('pages:service_digital_marketing')
    shipping_url = reverse('pages:blog_post', args=['ecommerce-shipping-delivery-integration'])

    posts = []
    for i, article in enumerate(ARTICLES):
        body = article['body'].format(ecom_url=ecom_url, dm_url=dm_url, shipping_url=shipping_url).strip()
        published_at = _PUBLISHED_DATES[i] if i < len(_PUBLISHED_DATES) else _PUBLISHED_DATES[-1]
        posts.append(StaticPost(
            slug=article['slug'],
            title=article['title'],
            category=article['category'],
            category_slug=slugify(article['category']),
            excerpt=article['excerpt'],
            body=body,
            meta_description=article['meta_description'],
            image_path=f"assets/images/blog/{article['slug']}.png",
            published_at=published_at,
            updated_at=published_at,
            read_time_minutes=article['read_time_minutes'],
            is_featured=article.get('is_featured', False),
        ))
    return posts


def get_static_posts() -> list[StaticPost]:
    """Built lazily (not at import time) since it needs URL resolution,
    which isn't safe before the URLconf is fully loaded."""
    return _build_static_posts()


def get_static_post(slug: str) -> StaticPost | None:
    return next((p for p in get_static_posts() if p.slug == slug), None)
