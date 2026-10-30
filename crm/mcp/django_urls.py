from django.urls import path

from . import django_views as v

# Paths the MCP/OAuth standards fix (claude.ai looks them up on the site root), so they sit outside /crm/.
urlpatterns = [
    path('mcp', v.gateway),
    path('authorize', v.gateway),
    path('token', v.gateway),
    path('register', v.gateway),
    path('revoke', v.gateway),
    path('.well-known/oauth-authorization-server', v.gateway),
    path('.well-known/oauth-protected-resource/mcp', v.gateway),
    # some clients ask for the un-suffixed form first
    path('.well-known/oauth-protected-resource', v.gateway, {'alias_path': '/.well-known/oauth-protected-resource/mcp'}),
]
