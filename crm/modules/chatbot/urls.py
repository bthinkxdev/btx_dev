from django.urls import path

from . import views

urlpatterns = [
    path('chats/', views.chat_list, name='chat_list'),
    path('chats/<int:pk>/', views.chat_detail, name='chat_detail'),
    path('ai-analyst/', views.ai_analyst, name='ai_analyst'),
    path('ai-analyst/ask/', views.ai_analyst_ask, name='ai_analyst_ask'),
]
