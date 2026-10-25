from django.urls import path

from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('login/', views.login_view, name='login'),
    path('login/verify/', views.two_factor_view, name='two_factor'),
    path('logout/', views.logout_view, name='logout'),
    path('me/', views.profile_view, name='profile'),
]
