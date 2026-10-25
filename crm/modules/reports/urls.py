from django.urls import path

from . import views

urlpatterns = [
    path('dashboards/owner/', views.dashboard_owner, name='dashboard_owner'),
    path('dashboards/sales/', views.dashboard_sales, name='dashboard_sales'),
    path('dashboards/finance/', views.dashboard_finance, name='dashboard_finance'),
    path('dashboards/delivery/', views.dashboard_delivery, name='dashboard_delivery'),
    path('dashboards/hr/', views.hr_dashboard, name='hr_dashboard'),
    path('dashboards/<slug:key>/configure/', views.dashboard_configure, name='dashboard_configure'),
    path('reports/', views.report_index, name='report_index'),
    path('reports/schedules/<int:pk>/delete/', views.report_schedule_delete, name='report_schedule_delete'),
    path('reports/<slug:slug>/', views.report_view, name='report_view'),
    path('reports/<slug:slug>/schedule/', views.report_schedule, name='report_schedule'),
]
