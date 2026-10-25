from django.urls import path

from crm.modules.payroll import views as pv

from . import views

urlpatterns = [
    path('hr/employees/', views.EmployeeList.as_view(), name='employee_list'),
    path('hr/employees/new/', views.employee_form, name='employee_create'),
    path('hr/employees/<int:pk>/', views.employee_detail, name='employee_detail'),
    path('hr/employees/<int:pk>/edit/', views.employee_form, name='employee_edit'),
    path('hr/employees/<int:pk>/do/<slug:action>/', views.employee_action, name='employee_action'),
    path('hr/attendance/', views.attendance, name='attendance'),
    path('hr/leave/', views.leave_list, name='leave_list'),
    path('hr/approvals/', views.hr_approvals, name='hr_approvals'),
    path('hr/holidays/', views.holiday_list, name='holiday_list'),
    path('payroll/', pv.payroll_list, name='payroll_list'),
    path('payroll/adjustment/', pv.adjustment_add, name='payroll_adjustment'),
    path('payroll/structures/', pv.salary_structures, name='salary_structures'),
    path('payroll/my-payslips/', pv.my_payslips, name='my_payslips'),
    path('payroll/payslips/<int:pk>/pdf/', pv.payslip_pdf_view, name='payslip_pdf'),
    path('payroll/commissions/', pv.commission_list, name='commission_list'),
    path('payroll/<int:pk>/', pv.payroll_detail, name='payroll_detail'),
    path('payroll/<int:pk>/do/<slug:action>/', pv.payroll_action, name='payroll_action'),
]
