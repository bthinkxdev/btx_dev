from django.urls import path

from . import views

urlpatterns = [
    path('pipeline/', views.pipeline, name='pipeline'),
    path('targets/', views.targets, name='targets'),
    path('targets/save/', views.target_save, name='target_save'),
]
