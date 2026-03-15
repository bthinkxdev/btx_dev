from django.contrib import admin
from django.urls import path, include
from pages.views import page_404

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('pages.urls')),
]

handler404 = page_404
