from django.urls import path

from . import views

app_name = 'leadfinder'

urlpatterns = [
    path('', views.index, name='lf_index'),
    path('runs/start/', views.run_start, name='lf_run_start'),
    path('runs/<int:run_id>/stop/', views.run_stop, name='lf_run_stop'),
    path('runs/<int:run_id>/status/', views.run_status, name='lf_run_status'),
    path('runs/<int:run_id>/export/', views.export_run, name='lf_export_run'),
    path('history/', views.history, name='lf_history'),
    path('history/export/', views.export_history, name='lf_export_history'),
]
