from django.urls import path

from . import views

urlpatterns = [
    path('images/', views.home, name='image_home'),
    path('images/list/', views.ImageList.as_view(), name='image_list'),
    path('images/upload/', views.upload, name='image_upload'),
    path('images/upload/file/', views.upload_file, name='image_upload_file'),
    path('images/upload/send/', views.upload_send, name='image_upload_send'),
    path('images/upload/voice/', views.upload_voice, name='image_upload_voice'),
    path('images/voice/<int:pk>/', views.voice_file, name='image_voice'),
    path('images/<int:pk>/voice/', views.voice_post, name='image_voice_post'),
    path('images/submissions/', views.submission_list, name='image_submissions'),
    path('images/submissions/<int:pk>/', views.submission_detail, name='image_submission'),
    path('images/submissions/<int:pk>/add/', views.submission_add, name='image_submission_add'),
    path('images/submissions/<int:pk>/bulk/', views.submission_bulk, name='image_submission_bulk'),
    path('images/team/', views.team, name='image_team'),
    path('images/files/<int:pk>/', views.file, name='image_file'),
    path('images/files/<int:pk>/thumb/', views.thumb, name='image_thumb'),
    path('images/clients/<int:pk>/logins/', views.client_logins, name='image_client_logins'),
    path('images/<int:pk>/', views.detail, name='image_detail'),
    path('images/<int:pk>/do/<slug:verb>/', views.action, name='image_action'),
]
