from django.urls import path

from . import views

urlpatterns = [
    path('contracts/', views.ContractList.as_view(), name='contract_list'),
    path('contracts/new/', views.contract_create, name='contract_create'),
    path('contracts/renewals/', views.renewals, name='renewals'),
    path('contracts/billing-schedule/', views.billing_schedule, name='billing_schedule'),
    path('contracts/<int:pk>/', views.contract_detail, name='contract_detail'),
    path('contracts/<int:pk>/edit/', views.contract_edit, name='contract_edit'),
    path('contracts/<int:pk>/do/<slug:action>/', views.contract_action, name='contract_action'),
]
