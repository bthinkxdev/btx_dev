from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from .models import BlogPost


class StaticViewSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.7

    PRIORITIES = {
        'pages:index': 1.0,
        'pages:services': 0.6,
        'pages:service_ecommerce': 0.9,
        'pages:service_digital_marketing': 0.9,
        'pages:portfolio': 0.7,
        'pages:contact': 0.6,
        'pages:about': 0.5,
        'pages:blog': 0.6,
        'pages:careers': 0.4,
        'pages:privacy_policy': 0.1,
        'pages:terms': 0.1,
    }

    def items(self):
        return list(self.PRIORITIES.keys())

    def location(self, item):
        return reverse(item)

    def priority(self, item):
        return self.PRIORITIES.get(item, 0.5)


class BlogPostSitemap(Sitemap):
    changefreq = 'monthly'
    priority = 0.5

    def items(self):
        return BlogPost.objects.filter(is_published=True).order_by('-published_at')

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return reverse('pages:blog_post', args=[obj.slug])


sitemaps = {
    'static': StaticViewSitemap,
    'blog': BlogPostSitemap,
}
