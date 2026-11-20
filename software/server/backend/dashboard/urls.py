from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.overview, name='overview'),

    path('violations/', views.violation_list, name='violation_list'),
    path('violations/<int:pk>/', views.violation_detail, name='violation_detail'),
    path('violations/<int:pk>/review/', views.violation_review, name='violation_review'),
    path('violations/<int:pk>/alert/', views.violation_alert, name='violation_alert'),
    path('violations/<int:pk>/resolve/', views.violation_resolve, name='violation_resolve'),

    path('captures/', views.capture_list, name='capture_list'),
    path('captures/feed/', views.capture_feed, name='capture_feed'),
    path('captures/<int:pk>/', views.capture_detail, name='capture_detail'),
    path('captures/<int:pk>/requeue/', views.capture_requeue, name='capture_requeue'),

    path('nodes/', views.node_list, name='node_list'),
    path('nodes/new/', views.node_form, name='node_create'),
    path('nodes/<int:pk>/', views.node_detail, name='node_detail'),
    path('nodes/<int:pk>/edit/', views.node_form, name='node_edit'),
    path('nodes/<int:node_pk>/zones/new/', views.zone_form, name='zone_create'),
    path('nodes/<int:node_pk>/zones/<int:pk>/', views.zone_form, name='zone_edit'),
    path('nodes/<int:node_pk>/zones/<int:pk>/delete/', views.zone_delete, name='zone_delete'),

    path('alerts/', views.alert_list, name='alert_list'),
    path('alerts/<int:pk>/response/', views.alert_response, name='alert_response'),

    path('hotspots/', views.hotspots, name='hotspots'),
]
