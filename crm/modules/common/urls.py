from django.urls import path

from crm.core import views as core_views

from . import views

urlpatterns = [
    path('search/', views.search, name='search'),
    path('notifications/', views.notification_list, name='notification_list'),
    path('notifications/<int:pk>/', views.notification_open, name='notification_open'),
    path('notifications/<int:pk>/snooze/', views.notification_snooze, name='notification_snooze'),
    path('notifications/read-all/', views.notifications_read_all, name='notifications_read_all'),
    path('approvals/', views.approval_list, name='approval_list'),
    path('approvals/<int:pk>/', views.approval_detail, name='approval_detail'),
    path('approvals/<int:pk>/decide/', views.approval_decide, name='approval_decide'),
    path('files/upload/<slug:model_name>/<int:pk>/', views.attachment_upload, name='attachment_upload'),
    path('files/<int:pk>/', views.attachment_open, name='attachment_open'),
    path('files/<int:pk>/archive/', views.attachment_archive, name='attachment_archive'),
    path('files/download/<str:token>/', views.file_download, name='file_download'),
    path('filters/save/', core_views.staff_required(core_views.save_filter_view), name='saved_filter_save'),
    path('filters/<int:pk>/delete/', core_views.staff_required(core_views.delete_filter_view), name='saved_filter_delete'),
    # PWA: installable app + web push (no login required for the first two — the browser
    # fetches them to decide whether the app can be installed, including from the login page).
    path('manifest.webmanifest', views.manifest, name='manifest'),
    path('service-worker.js', views.service_worker, name='service_worker'),
    path('push/subscribe/', views.push_subscribe, name='push_subscribe'),
    path('push/unsubscribe/', views.push_unsubscribe, name='push_unsubscribe'),
]
