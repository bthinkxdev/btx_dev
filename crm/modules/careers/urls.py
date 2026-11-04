from django.urls import path

from . import views

urlpatterns = [
    path('careers/', views.CareerList.as_view(), name='career_list'),
    path('careers/new/', views.career_form, name='career_create'),
    path('careers/<int:pk>/', views.career_form, name='career_edit'),
    path('careers/<int:pk>/toggle/', views.career_toggle, name='career_toggle'),
    path('careers/<int:pk>/delete/', views.career_delete, name='career_delete'),
    path('careers/applications/', views.ApplicationList.as_view(), name='career_applications'),
    path('careers/applications/<int:pk>/', views.application_detail, name='career_application_detail'),
    path('careers/applications/<int:pk>/action/', views.application_action, name='career_application_action'),
    path('careers/applications/<int:app_pk>/interviews/new/', views.interview_form, name='career_interview_create'),
    path('careers/interviews/', views.InterviewList.as_view(), name='career_interviews'),
    path('careers/interviews/<int:pk>/', views.interview_form, name='career_interview_edit'),
    path('careers/interviews/<int:pk>/outcome/', views.interview_outcome, name='career_interview_outcome'),
    path('careers/applications/<int:pk>/update/', views.application_update, name='career_application_update'),
    path('careers/applications/<int:pk>/resume/', views.application_resume, name='career_application_resume'),
]
