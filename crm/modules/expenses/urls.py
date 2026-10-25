from django.urls import path

from . import views

urlpatterns = [
    path('expenses/', views.ExpenseList.as_view(), name='expense_list'),
    path('expenses/new/', views.expense_create, name='expense_create'),
    path('expenses/approvals/', views.ApprovalQueue.as_view(), name='expense_approvals'),
    path('expenses/recurring/', views.recurring_list, name='recurring_expense_list'),
    path('expenses/tds-payable/', views.tds_payable, name='tds_payable'),
    path('expenses/<int:pk>/', views.expense_detail, name='expense_detail'),
    path('expenses/<int:pk>/edit/', views.expense_edit, name='expense_edit'),
    path('expenses/<int:pk>/do/<slug:action>/', views.expense_action, name='expense_action'),
    path('vendors/', views.VendorList.as_view(), name='vendor_list'),
    path('vendors/new/', views.vendor_form, name='vendor_create'),
    path('vendors/<int:pk>/', views.vendor_detail, name='vendor_detail'),
    path('vendors/<int:pk>/edit/', views.vendor_form, name='vendor_edit'),
]
