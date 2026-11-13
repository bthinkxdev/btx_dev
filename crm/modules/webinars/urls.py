from django.urls import path

from . import views

urlpatterns = [
    path('webinars/', views.webinar_list, name='webinar_list'),
    path('webinars/new/', views.webinar_form, name='webinar_create'),
    path('webinars/tracking/', views.tracking_settings, name='webinar_tracking'),
    path('webinars/tracking/test/', views.tracking_test, name='webinar_tracking_test'),
    path('webinars/<int:pk>/edit/', views.webinar_form, name='webinar_edit'),
    path('webinars/<int:pk>/duplicate/', views.webinar_duplicate, name='webinar_duplicate'),
    path('webinars/<int:pk>/toggle/', views.webinar_toggle, name='webinar_toggle'),
    path('webinars/<int:pk>/joining-links/', views.send_joining_links, name='webinar_send_joining'),
    path('webinars/registrations/', views.RegistrationList.as_view(), name='webinar_registrations'),
    path('webinars/registrations/<int:pk>/', views.lead_detail, name='webinar_lead_detail'),
    path('webinars/registrations/<int:pk>/stage/', views.lead_stage, name='webinar_lead_stage'),
    path('webinars/registrations/<int:pk>/assign/', views.lead_assign, name='webinar_lead_assign'),
    path('webinars/registrations/<int:pk>/note/', views.lead_note, name='webinar_lead_note'),
    path('webinars/registrations/<int:pk>/convert/', views.lead_convert, name='webinar_lead_convert'),
    path('webinars/registrations/<int:pk>/action/', views.registration_action, name='webinar_registration_action'),
]
