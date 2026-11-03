from django.conf import settings

from .models import SiteSettings


def seo(request):
    """Site-wide SEO context: clean self-referencing canonical (no query string)
    and a default Open Graph image for pages that don't override one."""
    # No row yet means the Diwali Special runs with its defaults until someone changes them.
    site_settings = SiteSettings.current()
    diwali_active = site_settings.diwali_active
    return {
        'diwali_active': diwali_active,
        'diwali_glitter': diwali_active and site_settings.diwali_glitter_enabled,
        'diwali_crackers': diwali_active and site_settings.diwali_crackers_enabled,
        'diwali_days_left': site_settings.days_to_diwali if diwali_active else None,
        'canonical_url': request.build_absolute_uri(request.path),
        'og_default_image_url': request.build_absolute_uri('/static/assets/images/logo.jpg'),
        'ga_measurement_id': settings.GA_MEASUREMENT_ID,
    }
