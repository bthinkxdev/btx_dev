from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

from pages.views import page_404
from crm.views import whatsapp_webhook
from crm import api_wa_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('crm/', include('crm.urls')),
    path('webhook/whatsapp/', whatsapp_webhook, name='whatsapp_webhook'),
    path('api/wa/incoming/', api_wa_views.wa_incoming, name='wa_incoming'),
    path('api/wa/status/', api_wa_views.wa_status, name='wa_status'),
    path('api/wa/outgoing/', api_wa_views.wa_outgoing, name='wa_outgoing'),
    path('', include('pages.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

handler404 = page_404
