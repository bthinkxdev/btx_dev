"""Public chat API, mounted at /chat/ (outside the login-protected /crm/ area)."""
from django.urls import path

from . import public

app_name = 'chat_public'

urlpatterns = [
    path('api/start/', public.start, name='start'),
    path('api/message/', public.message, name='message'),
    path('api/lead/', public.lead, name='lead'),
]
