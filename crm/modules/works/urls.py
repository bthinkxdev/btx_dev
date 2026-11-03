from django.urls import path

from . import views

urlpatterns = [
    path('works/', views.WorkList.as_view(), name='work_list'),
    path('works/new/', views.work_form, name='work_create'),
    path('works/<int:pk>/', views.work_form, name='work_edit'),
    path('works/<int:pk>/toggle/', views.work_toggle, name='work_toggle'),
    path('works/<int:pk>/delete/', views.work_delete, name='work_delete'),
]
