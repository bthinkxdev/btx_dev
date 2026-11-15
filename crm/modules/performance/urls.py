from django.urls import path

from . import views

urlpatterns = [
    path('people/performance/', views.team_board, name='performance_board'),
    path('people/<int:pk>/performance/', views.staff_profile, name='staff_profile'),
    path('people/reports/', views.daily_board, name='daily_board'),
    path('people/reports/find/', views.daily_find, name='daily_find'),
    path('people/reports/<int:pk>/', views.daily_report, name='daily_report'),
    path('login/report/event/', views.report_event, name='report_event'),
]
