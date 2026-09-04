def seo(request):
    """Site-wide SEO context: clean self-referencing canonical (no query string)
    and a default Open Graph image for pages that don't override one."""
    return {
        'canonical_url': request.build_absolute_uri(request.path),
        'og_default_image_url': request.build_absolute_uri('/static/assets/images/logo.jpg'),
    }
