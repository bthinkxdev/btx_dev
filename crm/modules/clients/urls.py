from django.urls import path

from . import views

urlpatterns = [
    path('clients/', views.ClientList.as_view(), name='client_list'),
    path('clients/new/', views.client_form, name='client_create'),
    path('clients/<int:pk>/', views.client_detail, name='client_detail'),
    path('clients/<int:pk>/edit/', views.client_form, name='client_edit'),
    path('clients/<int:pk>/contacts/new/', views.contact_save, name='contact_add'),
    path('clients/<int:pk>/contacts/<int:contact_id>/', views.contact_save, name='contact_edit'),
    path('clients/<int:pk>/contacts/<int:contact_id>/<slug:action>/', views.contact_action, name='contact_action'),
    path('clients/<int:pk>/status/', views.client_status, name='client_status'),
    path('clients/<int:pk>/archive/', views.client_archive, name='client_archive'),
    path('clients/<int:pk>/merge/', views.client_merge, name='client_merge'),
    path('services/', views.ServiceList.as_view(), name='service_list'),
    path('services/new/', views.service_form, name='service_create'),
    path('services/<int:pk>/', views.service_form, name='service_edit'),
    path('services/<int:pk>/tax-rules/', views.service_tax_rule, name='service_tax_rule'),
    path('services/<int:pk>/tax-rules/<int:rule_id>/end/', views.service_tax_rule_end, name='service_tax_rule_end'),
]
