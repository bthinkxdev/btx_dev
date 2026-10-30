from django.urls import path

from . import views

urlpatterns = [
    path('careers/', views.CareerList.as_view(), name='career_list'),
    path('careers/new/', views.career_form, name='career_create'),
    path('careers/<int:pk>/', views.career_form, name='career_edit'),
    path('careers/<int:pk>/toggle/', views.career_toggle, name='career_toggle'),
    path('careers/<int:pk>/delete/', views.career_delete, name='career_delete'),
    path('careers/applications/', views.ApplicationList.as_view(), name='career_applications'),
    path('careers/applications/<int:pk>/update/', views.application_update, name='career_application_update'),
    path('careers/applications/<int:pk>/resume/', views.application_resume, name='career_application_resume'),
]
