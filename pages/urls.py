from django.urls import path
from . import offer_views, views

app_name = 'pages'

urlpatterns = [
    path('', views.index, name='index'),
    path('services/', views.services, name='services'),
    path('services/ecommerce-development/', views.service_ecommerce, name='service_ecommerce'),
    path('services/digital-marketing/', views.service_digital_marketing, name='service_digital_marketing'),
    path('portfolio/', views.portfolio, name='portfolio'),
    path('about/', views.about, name='about'),
    path('blog/', views.blog, name='blog'),
    path('blog/<slug:slug>/', views.blog_post, name='blog_post'),
    path('privacy-policy/', views.privacy_policy, name='privacy_policy'),
    path('terms/', views.terms, name='terms'),
    path('newsletter/subscribe/', views.newsletter_subscribe, name='newsletter_subscribe'),
    path(
        'newsletter/unsubscribe/<uuid:token>/',
        views.newsletter_unsubscribe,
        name='newsletter_unsubscribe',
    ),
    path('contact/', views.contact, name='contact'),
    path('contact/submit/', views.contact_submit, name='contact_submit'),
    path('services/quote/request/', views.quote_request, name='quote_request'),
    path('services/quote/<slug:package>/pdf/', views.quote_pdf, name='quote_pdf'),
    path('careers/', views.careers, name='careers'),
    path('careers/apply/', views.career_apply, name='career_apply'),

    path('offers/navaratri/', offer_views.navaratri_offer, name='navaratri_offer'),
    path('offers/navaratri/submit/', offer_views.navaratri_offer_submit, name='navaratri_offer_submit'),
    path('offers/navaratri/return/', offer_views.navaratri_offer_return, name='navaratri_offer_return'),
    path('offers/navaratri/webhook/', offer_views.navaratri_offer_webhook, name='navaratri_offer_webhook'),
]
