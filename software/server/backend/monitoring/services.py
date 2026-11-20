"""Write paths into the database, shared by the HTTP API and any in-process caller.

The YOLO manager loop is:
    claim_captures()  ->  run the model  ->  record_inference() or mark_failed()

The dashboard (Wizard of Oz review) loop is:
    report_violation()  ->  review_violation()  ->  send_alert()  ->  record_alert_response()
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from .models import Alert, Capture, Detection, Heartbeat, InferenceRun, Node, Review, Violation


def get_node(code):
    try:
        return Node.objects.get(code=code)
    except Node.DoesNotExist:
        raise ValidationError(f'Unknown node "{code}". Register it in the admin first.')


def record_heartbeat(node, battery_voltage=None, rssi=None, uptime_s=None):
    heartbeat = Heartbeat.objects.create(
        node=node, battery_voltage=battery_voltage, rssi=rssi, uptime_s=uptime_s,
    )
    node.mark_seen(battery_voltage=battery_voltage, at=heartbeat.received_at)
    return heartbeat


def register_capture(node, sequence, captured_at, image_file, checksum='',
                     width=None, height=None, battery_voltage=None):
    """Store a reassembled photo from the gateway and queue it for YOLO.

    Re-sending the same frame (same node, sequence and time) returns the
    existing capture, so the gateway bridge can safely retry after a network cut.
    """
    existing = Capture.objects.filter(node=node, sequence=sequence, captured_at=captured_at).first()
    if existing:
        return existing, False

    capture = Capture(
        node=node, sequence=sequence, captured_at=captured_at,
        checksum=checksum, width=width, height=height,
    )
    capture.image.save(image_file.name, image_file, save=False)
    capture.save()
    node.mark_seen(battery_voltage=battery_voltage)
    return capture, True


def claim_captures(limit=1):
    """Hand out pending captures to a YOLO worker.

    Each capture is claimed with a conditional UPDATE, so two workers never get
    the same image, on SQLite as well as PostgreSQL. Captures left in
    "processing" by a crashed worker are handed out again after the timeout.
    """
    stale_before = timezone.now() - timedelta(minutes=settings.YOLO_CLAIM_TIMEOUT_MINUTES)
    candidates = (
        Capture.objects
        .filter(Q(status=Capture.Status.PENDING) |
                Q(status=Capture.Status.PROCESSING, claimed_at__lt=stale_before))
        .order_by('captured_at')
        .values_list('id', 'status', 'claimed_at')[:limit * 3]
    )

    claimed_ids = []
    for capture_id, status, claimed_at in candidates:
        now = timezone.now()
        updated = Capture.objects.filter(id=capture_id, status=status, claimed_at=claimed_at).update(
            status=Capture.Status.PROCESSING, claimed_at=now, error='', attempts=F('attempts') + 1,
        )
        if updated:
            claimed_ids.append(capture_id)
            if len(claimed_ids) == limit:
                break

    return list(Capture.objects.filter(id__in=claimed_ids).select_related('node').order_by('captured_at'))


def _validate_detection(item, index):
    try:
        x1, y1, x2, y2 = (float(v) for v in item['bbox'])
        confidence = float(item['confidence'])
        class_name = str(item['class_name'])
    except (KeyError, TypeError, ValueError):
        raise ValidationError(
            f'detections[{index}] needs "class_name", "confidence" and "bbox" [x1, y1, x2, y2].'
        )
    if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2, confidence)):
        raise ValidationError(
            f'detections[{index}]: bbox and confidence must be normalized to 0-1 '
            '(use results.boxes.xyxyn from Ultralytics).'
        )
    if x2 < x1 or y2 < y1:
        raise ValidationError(f'detections[{index}]: bbox must be [x1, y1, x2, y2] with x2 >= x1 and y2 >= y1.')
    class_id = item.get('class_id')
    on_sidewalk = item.get('on_sidewalk')
    if on_sidewalk is not None and not isinstance(on_sidewalk, bool):
        raise ValidationError(f'detections[{index}]: "on_sidewalk" must be true, false or null.')
    overlap = item.get('sidewalk_overlap')
    try:
        overlap = None if overlap is None else min(max(float(overlap), 0.0), 1.0)
    except (TypeError, ValueError):
        raise ValidationError(f'detections[{index}]: "sidewalk_overlap" must be a number 0-1.')
    return dict(
        class_id=int(class_id) if class_id is not None else None,
        class_name=class_name, confidence=confidence, x1=x1, y1=y1, x2=x2, y2=y2,
        on_sidewalk=on_sidewalk, sidewalk_overlap=overlap,
    )


MAX_SIDEWALK_POLYGONS = 20
MAX_POLYGON_POINTS = 400


def _validate_sidewalk(sidewalk):
    """{"model": str, "polygons": [[[x, y], ...], ...], "coverage": 0-1} -> model fields."""
    if sidewalk is None:
        return {}
    if not isinstance(sidewalk, dict):
        raise ValidationError('"sidewalk" must be an object with "model", "polygons" and "coverage".')
    polygons = sidewalk.get('polygons') or []
    if not isinstance(polygons, list) or len(polygons) > MAX_SIDEWALK_POLYGONS:
        raise ValidationError(f'"sidewalk.polygons" must be a list of at most {MAX_SIDEWALK_POLYGONS} polygons.')
    cleaned = []
    for i, polygon in enumerate(polygons):
        try:
            points = [[round(float(x), 4), round(float(y), 4)] for x, y in polygon]
        except (TypeError, ValueError):
            raise ValidationError(f'sidewalk.polygons[{i}] must be a list of [x, y] points.')
        if not 3 <= len(points) <= MAX_POLYGON_POINTS:
            raise ValidationError(f'sidewalk.polygons[{i}] needs 3 to {MAX_POLYGON_POINTS} points.')
        if not all(0.0 <= v <= 1.0 for point in points for v in point):
            raise ValidationError(f'sidewalk.polygons[{i}]: points must be normalized to 0-1.')
        cleaned.append(points)
    try:
        coverage = sidewalk.get('coverage')
        coverage = None if coverage is None else min(max(float(coverage), 0.0), 1.0)
    except (TypeError, ValueError):
        raise ValidationError('"sidewalk.coverage" must be a number 0-1.')
    return {
        'sidewalk_model': str(sidewalk.get('model') or '')[:100],
        'sidewalk_polygons': cleaned,
        'sidewalk_coverage': coverage,
    }


@transaction.atomic
def record_inference(capture, model_name, detections, model_version='',
                     confidence_threshold=None, inference_ms=None, width=None, height=None, sidewalk=None):
    """Save one model's detections for a capture and mark the capture done.

    Each detection is tagged with the hand-drawn sidewalk zone its ground point
    (bottom-centre of the box) falls in, if any. When the YOLO manager also
    segmented the sidewalk automatically, `sidewalk` holds its polygons and each
    detection carries its own `on_sidewalk` verdict.
    """
    if not model_name:
        raise ValidationError('"model_name" is required.')
    cleaned = [_validate_detection(item, i) for i, item in enumerate(detections)]
    sidewalk_fields = _validate_sidewalk(sidewalk)

    run = InferenceRun.objects.create(
        capture=capture, model_name=model_name, model_version=model_version,
        confidence_threshold=confidence_threshold, inference_ms=inference_ms, **sidewalk_fields,
    )
    zones = list(capture.node.zones.filter(is_active=True))
    rows = []
    for data in cleaned:
        detection = Detection(run=run, **data)
        x, y = detection.ground_point
        detection.zone = next((zone for zone in zones if zone.contains(x, y)), None)
        rows.append(detection)
    Detection.objects.bulk_create(rows)

    capture.status = Capture.Status.DONE
    capture.processed_at = timezone.now()
    capture.error = ''
    update_fields = ['status', 'processed_at', 'error']
    if width and height and not capture.width:
        capture.width, capture.height = width, height
        update_fields += ['width', 'height']
    capture.save(update_fields=update_fields)
    return run


def mark_failed(capture, error):
    capture.status = Capture.Status.FAILED
    capture.error = str(error)[:2000]
    capture.processed_at = timezone.now()
    capture.save(update_fields=['status', 'error', 'processed_at'])
    return capture


# Violations, reviews and alerts

# Pilot thresholds from the MVP canvas (section 6, Metrics).
CONTINUE_RESPONSE_RATE = 0.60   # >= 60 % of alerts answered within 48 h -> continue
STOP_ACTION_RATE = 0.20         # < 20 % of alerts lead to any action -> stop
NODES_ONLINE_TARGET = 0.80      # >= 80 % of nodes still online -> theft check passed

# Which violation category a YOLO class most likely belongs to.
CLASS_CATEGORY = {
    'chair': Violation.Category.FURNITURE,
    'dining table': Violation.Category.FURNITURE,
    'bench': Violation.Category.FURNITURE,
    'umbrella': Violation.Category.STALL,
    'car': Violation.Category.VEHICLE,
    'motorcycle': Violation.Category.VEHICLE,
    'bicycle': Violation.Category.VEHICLE,
    'truck': Violation.Category.VEHICLE,
    'bus': Violation.Category.VEHICLE,
}


def compute_impact_score(violation):
    """0-100 score from the canvas inputs: blocked width (50), duration (30), repeats (20)."""
    ratio = None
    zone_width = violation.zone.sidewalk_width_m if violation.zone else None
    if violation.blocked_width_m is not None and zone_width:
        ratio = violation.blocked_width_m / zone_width
    elif violation.occupancy_ratio is not None:
        ratio = violation.occupancy_ratio
    width_part = 50 * min(max(ratio or 0, 0), 1)

    hours = violation.duration.total_seconds() / 3600
    duration_part = 30 * min(hours / 8, 1)

    repeat_part = 20 * min((violation.repeat_count - 1) / 5, 1)
    return round(width_part + duration_part + repeat_part, 1)


def refresh_impact_score(violation):
    """A reviewer's score overrides the computed one."""
    override = violation.reviews.exclude(impact_score=None).order_by('-created_at').first()
    violation.impact_score = override.impact_score if override else compute_impact_score(violation)
    violation.save(update_fields=['impact_score'])
    return violation.impact_score


def suggest_category(detections):
    for detection in detections:
        if detection.class_name in CLASS_CATEGORY:
            return CLASS_CATEGORY[detection.class_name]
    return Violation.Category.GOODS


def estimate_occupancy(zone, detections, run=None):
    """Rough share of the sidewalk covered by non-person boxes standing on it.

    Uses the automatically detected sidewalk when the run has one, else the
    hand-drawn zone. Overlapping boxes are counted twice, so treat it as an
    upper-bound hint for the reviewer.
    """
    if run is not None and run.sidewalk_coverage:
        sidewalk_area = run.sidewalk_coverage
        covered = sum(d.area for d in detections if d.on_sidewalk and d.class_name != 'person')
    elif zone is not None and zone.area:
        sidewalk_area = zone.area
        covered = sum(d.area for d in detections if d.zone_id == zone.id and d.class_name != 'person')
    else:
        return None
    return round(min(covered / sidewalk_area, 1.0), 2)


@transaction.atomic
def report_violation(capture, category, user=None, zone=None, occupancy_ratio=None,
                     blocked_width_m=None, notes=''):
    """Record an encroachment seen on a capture.

    If the same node already has an open violation of this category seen
    within VIOLATION_MERGE_HOURS, it is extended instead (longer duration,
    one more repeat), so one market stall does not become 50 violations.
    """
    window_start = capture.captured_at - timedelta(hours=settings.VIOLATION_MERGE_HOURS)
    violation = (
        Violation.objects
        .filter(node=capture.node, category=category,
                status__in=[Violation.Status.DETECTED, Violation.Status.CONFIRMED],
                last_seen_at__gte=window_start)
        .order_by('-last_seen_at')
        .first()
    )
    if violation:
        violation.first_seen_at = min(violation.first_seen_at, capture.captured_at)
        violation.last_seen_at = max(violation.last_seen_at, capture.captured_at)
        if not violation.captures.filter(pk=capture.pk).exists():
            violation.repeat_count += 1
        for field, value in (('occupancy_ratio', occupancy_ratio), ('blocked_width_m', blocked_width_m)):
            if value is not None:
                setattr(violation, field, max(getattr(violation, field) or 0, value))
        if notes:
            violation.notes = f'{violation.notes}\n{notes}'.strip()
        violation.save()
        created = False
    else:
        violation = Violation.objects.create(
            node=capture.node, zone=zone or capture.node.zones.filter(is_active=True).first(),
            category=category, first_seen_at=capture.captured_at, last_seen_at=capture.captured_at,
            occupancy_ratio=occupancy_ratio, blocked_width_m=blocked_width_m, notes=notes, created_by=user,
        )
        created = True
    violation.captures.add(capture)
    refresh_impact_score(violation)
    return violation, created


@transaction.atomic
def review_violation(violation, user, decision, impact_score=None, notes=''):
    review = Review.objects.create(
        violation=violation, reviewer=user, decision=decision, impact_score=impact_score, notes=notes,
    )
    violation.status = (Violation.Status.CONFIRMED if decision == Review.Decision.CONFIRM
                        else Violation.Status.REJECTED)
    violation.save(update_fields=['status'])
    refresh_impact_score(violation)
    return review


def alert_message(violation, link=''):
    """Ready-to-paste WhatsApp text for the Directorate's inspectors.

    Always Arabic, whatever language the reviewer uses the dashboard in.
    """
    with translation.override('ar'):
        return _alert_lines(violation, link)


def _alert_lines(violation, link):
    node = violation.node
    first = timezone.localtime(violation.first_seen_at)
    lines = [
        'تنبيه إشغال رصيف — Damascus Eye',
        f'الموقع: {node.name or node.code} ({node.code})',
        f'النوع: {violation.get_category_display()}',
        f'درجة التأثير: {violation.impact_score or 0:.0f} / 100',
        f'أول رصد: {first:%Y-%m-%d %H:%M}، عدد مرات الرصد: {violation.repeat_count}',
    ]
    if node.latitude is not None and node.longitude is not None:
        lines.append(f'الخريطة: https://maps.google.com/?q={node.latitude},{node.longitude}')
    if link:
        lines.append(f'التفاصيل: {link}')
    return '\n'.join(lines)


def send_alert(violation, user, channel=Alert.Channel.WHATSAPP):
    if violation.status != Violation.Status.CONFIRMED:
        raise ValidationError(_('Alerts can only be sent for a confirmed violation.'))
    return Alert.objects.create(violation=violation, channel=channel, sent_by=user)


@transaction.atomic
def record_alert_response(alert, response, notes=''):
    alert.response = response
    alert.response_notes = notes
    alert.responded_at = None if response == Alert.Response.NONE else (alert.responded_at or timezone.now())
    alert.save(update_fields=['response', 'response_notes', 'responded_at'])
    if response == Alert.Response.RESOLVED:
        Violation.objects.filter(pk=alert.violation_id).update(status=Violation.Status.RESOLVED)
    return alert


def pilot_metrics(since=None):
    """Continue / adjust / stop verdict from the MVP canvas, over alerts sent since `since`."""
    alerts = Alert.objects.all()
    if since is not None:
        alerts = alerts.filter(sent_at__gte=since)
    alerts = list(alerts)
    total = len(alerts)
    within_48h = sum(1 for a in alerts if a.responded_within_48h)
    acted = sum(1 for a in alerts if a.response in (Alert.Response.DISPATCHED, Alert.Response.RESOLVED))

    nodes = list(Node.objects.exclude(status=Node.Status.RETIRED))
    online = sum(1 for n in nodes if n.is_online)

    response_rate = within_48h / total if total else None
    action_rate = acted / total if total else None
    if total == 0:
        verdict = 'no_data'
    elif response_rate >= CONTINUE_RESPONSE_RATE:
        verdict = 'continue'
    elif action_rate < STOP_ACTION_RATE:
        verdict = 'stop'
    else:
        verdict = 'adjust'

    return {
        'alerts_total': total,
        'responded_within_48h': within_48h,
        'acted': acted,
        'response_rate': response_rate,
        'action_rate': action_rate,
        'verdict': verdict,
        'nodes_total': len(nodes),
        'nodes_online': online,
        'nodes_online_rate': online / len(nodes) if nodes else None,
        'continue_threshold': CONTINUE_RESPONSE_RATE,
        'stop_threshold': STOP_ACTION_RATE,
        'nodes_online_target': NODES_ONLINE_TARGET,
    }
