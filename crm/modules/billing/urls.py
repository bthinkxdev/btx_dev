from django.urls import path

from . import views

urlpatterns = [
    path('invoices/', views.InvoiceList.as_view(), name='invoice_list'),
    path('invoices/new/', views.invoice_create, name='invoice_create'),
    path('invoices/preview-totals/', views.invoice_preview_totals, name='invoice_preview_totals'),
    path('invoices/remind/', views.invoice_bulk_remind, name='invoice_bulk_remind'),
    path('invoices/<int:pk>/', views.invoice_detail, name='invoice_detail'),
    path('invoices/<int:pk>/edit/', views.invoice_edit, name='invoice_edit'),
    path('invoices/<int:pk>/pdf/', views.invoice_pdf_view, name='invoice_pdf'),
    path('invoices/<int:pk>/do/<slug:action>/', views.invoice_action, name='invoice_action'),
    path('credit-notes/', views.CreditNoteList.as_view(), name='credit_note_list'),
    path('credit-notes/<int:pk>/pdf/', views.credit_note_pdf_view, name='credit_note_pdf'),
    path('receivables/', views.receivables, name='receivables'),
    path('advances/', views.advances, name='advances'),
    path('tds-receivable/', views.TdsList.as_view(), name='tds_receivable'),
    path('tds-receivable/<int:pk>/do/<slug:action>/', views.tds_action, name='tds_action'),
    path('clients/<int:pk>/statement/', views.client_statement, name='client_statement'),
]
