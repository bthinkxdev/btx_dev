from django.urls import path

from . import views

urlpatterns = [
    path('mcp/authorize/<uuid:pk>/', views.authorize, name='mcp_authorize'),
]
