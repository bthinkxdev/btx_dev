from django.urls import path

from . import public, views

urlpatterns = [
    path('payment-links/', views.LinkList.as_view(), name='link_list'),
    path('payment-links/new/', views.link_create, name='link_create'),
    path('payment-links/tax-preview/', views.link_tax_preview, name='link_tax_preview'),
    path('payment-links/<int:pk>/', views.link_detail, name='link_detail'),
    path('payment-links/<int:pk>/do/<slug:action>/', views.link_action, name='link_action'),
    path('payments/', views.PaymentList.as_view(), name='payment_list'),
    path('payments/verify/', views.VerifyQueue.as_view(), name='payment_verify_queue'),
    path('payments/record/', views.payment_record, name='payment_record'),
    path('payments/allocation/', views.allocation, name='allocation'),
    path('payments/<int:pk>/', views.payment_detail, name='payment_detail'),
    path('payments/<int:pk>/receipt/', views.receipt_pdf_view, name='receipt_pdf'),
    path('payments/<int:pk>/allocate/', views.allocate_payment, name='allocate_payment'),
    path('payments/<int:pk>/do/<slug:action>/', views.payment_action, name='payment_action'),
    path('refunds/', views.RefundList.as_view(), name='refund_list'),
    path('refunds/<int:pk>/do/<slug:action>/', views.refund_action, name='refund_action'),
    path('gateway-events/', views.GatewayEventList.as_view(), name='gateway_events'),
    path('gateway-events/<int:pk>/', views.event_detail, name='event_detail'),
    path('gateway-events/<int:pk>/do/<slug:action>/', views.event_action, name='event_action'),
    path('settlements/', views.settlement_list, name='settlement_list'),
    # Public (no login)
    path('pay/<str:token>/', public.pay_page, name='pay_page'),
    path('pay/<str:token>/return/', public.pay_return, name='pay_return'),
    path('pay/<str:token>/status/', public.pay_status, name='pay_status'),
    path('pay/<str:token>/receipt/<int:pk>/', public.pay_receipt, name='pay_receipt'),
    path('pay/<str:token>/sandbox/<str:order>/', public.pay_sandbox, name='pay_sandbox'),
    path('webhooks/gateway/<slug:provider>/', public.gateway_webhook, name='gateway_webhook'),
]
