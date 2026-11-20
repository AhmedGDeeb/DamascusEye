from django.urls import path

from . import views

app_name = 'monitoring'

urlpatterns = [
    path('heartbeats/', views.heartbeat, name='heartbeat'),
    path('captures/', views.upload_capture, name='upload_capture'),
    path('captures/<int:capture_id>/', views.capture_detail, name='capture_detail'),
    path('yolo/claim/', views.yolo_claim, name='yolo_claim'),
    path('yolo/results/<int:capture_id>/', views.yolo_results, name='yolo_results'),
]
