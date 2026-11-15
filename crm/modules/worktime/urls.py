from django.urls import path

from . import views

urlpatterns = [
    path('time/', views.my_time, name='my_time'),
    path('time/break/start/', views.break_start, name='break_start'),
    path('time/break/end/', views.break_end, name='break_end'),
    path('time/break/', views.on_break, name='on_break'),
    path('time/notice/', views.notice_ack, name='work_notice_ack'),
    path('time/team/', views.team_time, name='team_time'),
    path('time/report/', views.team_report, name='team_time_report'),
    path('time/person/<int:pk>/', views.person_time, name='person_time'),
]
