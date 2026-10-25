from django.urls import path

from . import views

urlpatterns = [
    path('settings/company/', views.settings_company, name='settings_company'),
    path('settings/policy/', views.settings_policy, name='settings_policy'),
    path('settings/tax/', views.settings_tax, name='settings_tax'),
    path('settings/tax/series/<int:pk>/', views.settings_series, name='settings_series'),
    path('settings/tax/category/<int:pk>/', views.settings_category_tds, name='settings_category_tds'),
    path('settings/gateway/', views.settings_gateway, name='settings_gateway'),
    path('settings/users/', views.user_list, name='user_list'),
    path('settings/users/<int:pk>/', views.user_detail, name='user_detail'),
    path('settings/users/<int:pk>/<slug:action>/', views.user_action, name='user_action'),
    path('settings/roles/', views.role_list, name='role_list'),
    path('settings/roles/new/', views.role_create, name='role_create'),
    path('settings/roles/<int:pk>/save/', views.role_save, name='role_save'),
    path('settings/teams/', views.team_list, name='team_list'),
    path('settings/templates/', views.template_list, name='template_list'),
    path('settings/templates/<int:pk>/', views.template_edit, name='template_edit'),
    path('settings/deliveries/<int:pk>/retry/', views.delivery_retry, name='delivery_retry'),
    path('settings/jobs/', views.job_list, name='job_list'),
    path('settings/jobs/<slug:action>/', views.job_action, name='job_action'),
    path('settings/audit/', views.audit_log, name='audit_log'),
    path('settings/security/', views.security_events, name='security_events'),
    path('settings/security/unlock/<int:pk>/', views.security_unlock, name='security_unlock'),
]
