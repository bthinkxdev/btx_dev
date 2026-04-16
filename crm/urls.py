from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'crm'

urlpatterns = [
    path(
        'login/',
        auth_views.LoginView.as_view(template_name='crm/login.html'),
        name='login',
    ),
    path(
        'logout/',
        auth_views.LogoutView.as_view(),
        name='logout',
    ),
    path('', views.dashboard, name='dashboard'),
    path('leads/', views.leads_list, name='leads'),
    path('leads/more/', views.leads_more_json, name='leads_more'),
    path('leads/search/', views.lead_search, name='lead_search'),
    path('leads/quick-add/', views.lead_quick_add, name='lead_quick_add'),
    path('leads/create/', views.lead_create, name='lead_create'),
    path('leads/import/', views.leads_import_excel, name='leads_import'),
    path('leads/<int:pk>/patch/', views.lead_patch, name='lead_patch'),
    path('leads/<int:pk>/quick-fu/', views.lead_quick_followup, name='lead_quick_fu'),
    path('leads/<int:pk>/quick-note/', views.lead_quick_note, name='lead_quick_note'),
    path('leads/<int:pk>/', views.lead_detail, name='lead_detail'),
    path('leads/<int:pk>/notes/', views.lead_notes_save, name='lead_notes_save'),
    path(
        'leads/<int:pk>/contact/',
        views.lead_contact_save,
        name='lead_contact_save',
    ),
    path('leads/<int:pk>/status/', views.lead_status_detail, name='lead_status_detail'),
    path('leads/<int:lead_pk>/followup/', views.followup_add, name='followup_add'),
    path('leads/<int:lead_pk>/task/', views.task_add, name='task_add'),
    path(
        'leads/<int:pk>/tasks-panel/',
        views.lead_tasks_panel,
        name='lead_tasks_panel',
    ),
    path('leads/<int:pk>/log-call/', views.lead_log_call, name='lead_log_call'),
    path(
        'leads/<int:pk>/log-whatsapp/',
        views.lead_log_whatsapp,
        name='lead_log_whatsapp',
    ),
    path('tasks/<int:pk>/toggle/', views.task_toggle, name='task_toggle'),
    path('tasks/<int:pk>/update/', views.task_update, name='task_update'),
    path(
        'header/tasks-dropdown/',
        views.tasks_header_dropdown,
        name='tasks_header_dropdown',
    ),
    path(
        'header/tasks-badges/',
        views.tasks_header_badges,
        name='tasks_header_badges',
    ),
    path('followups/', views.followups_page, name='followups'),
    path('followups/<int:pk>/done/', views.followup_done, name='followup_done'),
    path('followups/<int:pk>/reschedule/', views.followup_reschedule, name='followup_reschedule'),
    path('packages/', views.packages_page, name='packages'),
    path('packages/create/', views.package_create, name='package_create'),
    path('packages/<int:pk>/edit/', views.package_update, name='package_update'),
    path('packages/<int:pk>/delete/', views.package_delete, name='package_delete'),
    path('performance/', views.performance, name='performance'),
    path(
        'performance/report-card/',
        views.performance_report_card,
        name='performance_report_card',
    ),
    path(
        'leads/<int:pk>/high-hope-toggle/',
        views.lead_high_hope_toggle,
        name='lead_high_hope_toggle',
    ),
    path('achievements/', views.achievements_dashboard, name='achievements_dashboard'),
    path('achievements/create/', views.achievement_create, name='achievement_create'),
    path('achievements/<int:pk>/edit/', views.achievement_update, name='achievement_update'),
    path('achievements/<int:pk>/delete/', views.achievement_delete, name='achievement_delete'),
]
