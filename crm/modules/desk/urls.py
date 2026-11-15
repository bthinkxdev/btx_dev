from django.urls import path

from . import views

urlpatterns = [
    path('business-staff/', views.desk_home, name='desk_home'),
    path('business-staff/ask/', views.desk_ask, name='desk_ask'),
    path('business-staff/briefing/', views.desk_briefing_generate, name='desk_briefing_generate'),
    path('business-staff/suggestions/refresh/', views.desk_actions_refresh, name='desk_actions_refresh'),
    path('business-staff/actions/<int:pk>/<slug:verb>/', views.desk_action, name='desk_action'),
    path('business-staff/goals/', views.desk_goals, name='desk_goals'),
    path('business-staff/goals/<int:pk>/delete/', views.desk_goal_delete, name='desk_goal_delete'),
    path('business-staff/plan/', views.desk_plan, name='desk_plan'),
    path('business-staff/plan/<int:pk>/review/', views.desk_plan_review, name='desk_plan_review'),
    path('business-staff/plan/<int:pk>/reread/', views.desk_plan_reread, name='desk_plan_reread'),
    path('business-staff/plan/<int:pk>/delete/', views.desk_plan_delete, name='desk_plan_delete'),
]
