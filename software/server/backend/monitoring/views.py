"""JSON API for the gateway bridge and the YOLO manager.

Every request must send the shared key in the X-API-Key header.
"""

import json
from functools import wraps
from hmac import compare_digest

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import services
from .models import Capture


def api_view(view):
    """API-key check, CSRF exemption and ValidationError -> 400 for machine clients."""
    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        key = request.headers.get('X-API-Key', '')
        if not compare_digest(key, settings.DAMASCUS_EYE_API_KEY):
            return JsonResponse({'error': 'Invalid or missing X-API-Key.'}, status=401)
        try:
            return view(request, *args, **kwargs)
        except ValidationError as exc:
            return JsonResponse({'error': ' '.join(exc.messages)}, status=400)
    return wrapper


def _json_body(request):
    try:
        return json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        raise ValidationError('Request body must be valid JSON.')


def _optional_number(data, name, cast=float):
    value = data.get(name)
    if value in (None, ''):
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise ValidationError(f'"{name}" must be a number.')


def _capture_payload(request, capture):
    return {
        'id': capture.id,
        'node': capture.node.code,
        'sequence': capture.sequence,
        'captured_at': capture.captured_at.isoformat(),
        'image_url': request.build_absolute_uri(capture.image.url),
        'image_path': capture.image.path,
        'attempts': capture.attempts,
    }


# Gateway bridge

@api_view
@require_POST
def heartbeat(request):
    data = _json_body(request)
    node = services.get_node(data.get('node'))
    beat = services.record_heartbeat(
        node,
        battery_voltage=_optional_number(data, 'battery_voltage'),
        rssi=_optional_number(data, 'rssi', int),
        uptime_s=_optional_number(data, 'uptime_s', int),
    )
    return JsonResponse({'id': beat.id, 'node': node.code}, status=201)


@api_view
@require_POST
def upload_capture(request):
    """multipart/form-data: node, sequence, captured_at (ISO 8601), image, [checksum, width, height, battery_voltage]."""
    data = request.POST
    image = request.FILES.get('image')
    if image is None:
        raise ValidationError('"image" file is required.')
    node = services.get_node(data.get('node'))
    captured_at = parse_datetime(data.get('captured_at', ''))
    if captured_at is None:
        raise ValidationError('"captured_at" must be an ISO 8601 datetime, e.g. 2026-11-17T10:30:00+03:00.')
    sequence = _optional_number(data, 'sequence', int)
    if sequence is None:
        raise ValidationError('"sequence" is required.')

    capture, created = services.register_capture(
        node, sequence, captured_at, image,
        checksum=data.get('checksum', ''),
        width=_optional_number(data, 'width', int),
        height=_optional_number(data, 'height', int),
        battery_voltage=_optional_number(data, 'battery_voltage'),
    )
    return JsonResponse({'id': capture.id, 'created': created, 'status': capture.status},
                        status=201 if created else 200)


# YOLO manager

@api_view
@require_POST
def yolo_claim(request):
    """Body: {"limit": 4}. Returns captures now reserved for this worker."""
    data = _json_body(request)
    limit = _optional_number(data, 'limit', int) or 1
    limit = max(1, min(limit, 50))
    captures = services.claim_captures(limit)
    return JsonResponse({'captures': [_capture_payload(request, c) for c in captures]})


@api_view
@require_POST
def yolo_results(request, capture_id):
    """Body: {"model_name", "detections": [{"class_name", "confidence", "bbox": [x1,y1,x2,y2] 0-1}], ...}
    or {"error": "..."} to report a failed image."""
    capture = get_object_or_404(Capture, pk=capture_id)
    data = _json_body(request)

    if data.get('error'):
        services.mark_failed(capture, data['error'])
        return JsonResponse({'id': capture.id, 'status': capture.status})

    detections = data.get('detections')
    if not isinstance(detections, list):
        raise ValidationError('"detections" must be a list (it may be empty).')
    run = services.record_inference(
        capture,
        model_name=data.get('model_name', ''),
        detections=detections,
        model_version=data.get('model_version', ''),
        confidence_threshold=_optional_number(data, 'confidence_threshold'),
        inference_ms=_optional_number(data, 'inference_ms'),
        width=_optional_number(data, 'width', int),
        height=_optional_number(data, 'height', int),
        sidewalk=data.get('sidewalk'),
    )
    on_sidewalk = sum(1 for d in run.detections.all() if d.is_on_sidewalk)
    return JsonResponse({
        'id': capture.id, 'status': Capture.Status.DONE, 'run_id': run.id,
        'detections': run.detections.count(), 'on_sidewalk': on_sidewalk,
    }, status=201)


@api_view
@require_GET
def capture_detail(request, capture_id):
    """Lets the YOLO manager (or a test script) read a capture and its runs back."""
    capture = get_object_or_404(Capture.objects.select_related('node'), pk=capture_id)
    payload = _capture_payload(request, capture)
    payload['status'] = capture.status
    payload['error'] = capture.error
    payload['runs'] = [
        {
            'id': run.id, 'model_name': run.model_name, 'inference_ms': run.inference_ms,
            'detections': [
                {'class_name': d.class_name, 'confidence': d.confidence,
                 'bbox': [d.x1, d.y1, d.x2, d.y2], 'zone': d.zone_id, 'on_sidewalk': d.is_on_sidewalk}
                for d in run.detections.all()
            ],
        }
        for run in capture.runs.prefetch_related('detections')
    ]
    return JsonResponse(payload)
