from django.urls import path

from . import views

urlpatterns = [
    path('projects/', views.ProjectList.as_view(), name='project_list'),
    path('projects/new/', views.project_create, name='project_create'),
    path('projects/<int:pk>/', views.project_detail, name='project_detail'),
    path('projects/<int:pk>/edit/', views.project_edit, name='project_edit'),
    path('projects/<int:pk>/do/<slug:action>/', views.project_action, name='project_action'),
    path('tasks/', views.TaskList.as_view(), name='task_list'),
    path('tasks/new/', views.task_create, name='task_create'),
    path('tasks/<int:pk>/', views.task_detail, name='task_detail'),
    path('tasks/<int:pk>/status/', views.task_status, name='task_status'),
    path('timesheet/', views.timesheet, name='timesheet'),
    path('timesheet/approvals/', views.timesheet_approvals, name='timesheet_approvals'),
    path('blockers/', views.blockers, name='blockers'),
    path('blockers/<int:pk>/resolve/', views.blocker_action, name='blocker_resolve'),
    path('tickets/', views.TicketList.as_view(), name='ticket_list'),
    path('tickets/new/', views.ticket_create, name='ticket_create'),
    path('tickets/<int:pk>/', views.ticket_detail, name='ticket_detail'),
]
