from django.urls import path

from . import views

urlpatterns = [
    path('quotations/', views.QuotationList.as_view(), name='quotation_list'),
    path('quotations/new/', views.quotation_create, name='quotation_create'),
    path('quotations/preview-totals/', views.quotation_preview_totals, name='quotation_preview_totals'),
    path('quotations/<int:pk>/', views.quotation_detail, name='quotation_detail'),
    path('quotations/<int:pk>/edit/', views.quotation_edit, name='quotation_edit'),
    path('quotations/<int:pk>/pdf/', views.quotation_pdf_view, name='quotation_pdf'),
    path('quotations/<int:pk>/compare/', views.quotation_compare, name='quotation_compare'),
    path('quotations/<int:pk>/do/<slug:action>/', views.quotation_action, name='quotation_action'),
    path('q/<str:token>/', views.public_quotation, name='public_quotation'),
    path('q/<str:token>/pdf/', views.public_quotation_pdf, name='public_quotation_pdf'),
]
