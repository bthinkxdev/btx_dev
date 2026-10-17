from django.contrib.auth import views as auth_views
from django.urls import path

from . import billing_views, push_views, views, website_leads_views, workbench_views

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
    path('leads/search/', views.lead_search, name='lead_search'),
    path('leads/quick-add/', views.lead_quick_add, name='lead_quick_add'),
    path('leads/create/', views.lead_create, name='lead_create'),
    path('leads/import/', views.leads_import_excel, name='leads_import'),
    path('leads/<int:pk>/patch/', views.lead_patch, name='lead_patch'),
    path('leads/<int:pk>/quick-fu/', views.lead_quick_followup, name='lead_quick_fu'),
    path('leads/<int:pk>/', views.lead_detail, name='lead_detail'),
    path('leads/<int:pk>/notes/', views.lead_notes_save, name='lead_notes_save'),
    path(
        'leads/<int:pk>/contact/',
        views.lead_contact_save,
        name='lead_contact_save',
    ),
    path('leads/<int:pk>/status/', views.lead_status_detail, name='lead_status_detail'),
    path('leads/<int:pk>/mark-won/', views.lead_mark_won, name='lead_mark_won'),
    path('leads/<int:pk>/mark-lost/', views.lead_mark_lost, name='lead_mark_lost'),
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
    path(
        'leads/<int:pk>/high-hope-toggle/',
        views.lead_high_hope_toggle,
        name='lead_high_hope_toggle',
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

    # Billing — simple bill + receipt, no ledger/accounts.
    path('billing/', billing_views.billing_dashboard, name='billing_dashboard'),
    path('billing/create/', billing_views.bill_create, name='bill_create'),
    path('billing/bills/<int:pk>/', billing_views.bill_detail, name='bill_detail'),
    path('billing/bills/<int:pk>/pdf/', billing_views.bill_pdf, name='bill_pdf'),
    path(
        'billing/bills/<int:pk>/payments/add/',
        billing_views.bill_payment_create,
        name='bill_payment_create',
    ),
    path(
        'billing/payments/<int:pk>/receipt/',
        billing_views.bill_payment_receipt,
        name='bill_payment_receipt',
    ),

    # Daily Workbench — Activity-based work queue (additive, v1).
    path('workbench/', workbench_views.workbench, name='workbench'),
    path('workbench/team/', workbench_views.workbench_team, name='workbench_team'),
    path('pipeline/', workbench_views.pipeline_board, name='pipeline_board'),
    path(
        'workbench/leads/<int:pk>/panel/',
        workbench_views.workbench_lead_panel,
        name='workbench_lead_panel',
    ),
    path(
        'workbench/leads/<int:pk>/complete/',
        workbench_views.workbench_complete_activity,
        name='workbench_complete_activity',
    ),

    # Website Leads — contact form + quotation PDF requests from the public site.
    path('website-leads/', website_leads_views.website_leads, name='website_leads'),
    path(
        'website-leads/<str:lead_type>/<int:pk>/panel/',
        website_leads_views.website_lead_detail,
        name='website_lead_detail',
    ),
    path(
        'website-leads/<str:lead_type>/<int:pk>/toggle-seen/',
        website_leads_views.website_lead_toggle_seen,
        name='website_lead_toggle_seen',
    ),

    # PWA + Web Push.
    path('manifest.json', push_views.manifest_json, name='manifest'),
    path('sw.js', push_views.service_worker_js, name='service_worker'),
    path('push/subscribe/', push_views.push_subscribe, name='push_subscribe'),
    path('push/unsubscribe/', push_views.push_unsubscribe, name='push_unsubscribe'),
    path('push/test/', push_views.push_test, name='push_test'),
]
