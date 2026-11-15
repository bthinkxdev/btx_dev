from django.urls import path

from . import views

urlpatterns = [
    path('deliverables/', views.DeliverableList.as_view(), name='deliverable_list'),
    path('deliverables/mine/', views.mine, name='deliverable_mine'),
    path('deliverables/workload/', views.workload, name='deliverable_workload'),
    path('deliverables/calendar-link/reset/', views.calendar_feed_reset, name='calendar_feed_reset'),
    path('deliverables/<int:pk>/', views.detail, name='deliverable_detail'),
    path('deliverables/<int:pk>/do/<slug:verb>/', views.action, name='deliverable_action'),
    path('deliverables/<int:pk>/assets/<int:asset_id>/', views.asset_download, name='deliverable_asset'),
    path('deliverables/plans/<int:pk>/', views.plan_detail, name='delivery_plan'),
    path('deliverables/plans/<int:pk>/do/<slug:verb>/', views.plan_action, name='delivery_plan_action'),
    path('deliverables/plans/<int:plan_id>/items/new/', views.plan_item_form, name='plan_item_create'),
    path('deliverables/plans/items/<int:pk>/edit/', views.plan_item_form, name='plan_item_edit'),
    path('deliverables/plans/items/<int:pk>/do/<slug:verb>/', views.plan_item_action, name='plan_item_action'),
    path('deliverables/contracts/<int:pk>/plan/', views.contract_plan, name='contract_plan'),
    path('deliverables/services/<int:service_id>/', views.service_deliverables, name='service_deliverables'),
    path('deliverables/services/<int:service_id>/new/', views.template_item_form, name='template_item_create'),
    path('deliverables/services/items/<int:pk>/edit/', views.template_item_form, name='template_item_edit'),
    path('deliverables/services/items/<int:pk>/delete/', views.template_item_delete, name='template_item_delete'),
    path('deliverables/clients/<int:pk>/', views.client_summary, name='client_deliverables'),
    path('deliverables/clients/<int:pk>/send/', views.client_summary_send, name='client_summary_send'),
    path('deliverables/clients/<int:pk>/assets/', views.client_assets, name='client_assets'),
    path('api/calendar/<str:token>.ics', views.calendar_feed, name='calendar_feed'),
]
