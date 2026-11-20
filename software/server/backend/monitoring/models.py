from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Node(models.Model):
    """A solar ESP32-CAM edge node mounted on a facade."""

    class Status(models.TextChoices):
        ACTIVE = 'active', _('Active')
        MAINTENANCE = 'maintenance', _('Maintenance')
        RETIRED = 'retired', _('Retired')

    code = models.CharField(max_length=32, unique=True, help_text='ID the firmware sends, e.g. "N01".')
    name = models.CharField(max_length=100, blank=True)
    mac_address = models.CharField(max_length=17, blank=True, help_text='ESP-NOW MAC, e.g. "24:6F:28:AA:BB:CC".')
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    mount_height_m = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    installed_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    battery_voltage = models.FloatField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['code']

    def __str__(self):
        return f'{self.code} {self.name}'.strip()

    @property
    def is_online(self):
        if self.last_seen_at is None:
            return False
        limit = timedelta(minutes=settings.NODE_OFFLINE_AFTER_MINUTES)
        return timezone.now() - self.last_seen_at <= limit

    def mark_seen(self, battery_voltage=None, at=None):
        self.last_seen_at = at or timezone.now()
        update_fields = ['last_seen_at']
        if battery_voltage is not None:
            self.battery_voltage = battery_voltage
            update_fields.append('battery_voltage')
        self.save(update_fields=update_fields)


class Heartbeat(models.Model):
    """Periodic "still alive" message; a missing heartbeat flags theft or failure."""

    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name='heartbeats')
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    battery_voltage = models.FloatField(null=True, blank=True)
    rssi = models.IntegerField(null=True, blank=True, help_text='ESP-NOW signal strength at the gateway (dBm).')
    uptime_s = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-received_at']


class SidewalkZone(models.Model):
    """The sidewalk area inside one node's fixed camera view.

    Only detections whose ground point falls inside the polygon count as
    sidewalk occupation; objects on the road or in shop windows are ignored.
    """

    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name='zones')
    name = models.CharField(max_length=100, default='Sidewalk')
    polygon = models.JSONField(help_text='List of [x, y] points, normalized 0-1 to the image size.')
    sidewalk_width_m = models.FloatField(null=True, blank=True, help_text='Real clear width, used for blocked-width estimates.')
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f'{self.node.code} / {self.name}'

    @property
    def area(self):
        """Shoelace area of the polygon, as a share of the whole image (0-1)."""
        points = self.polygon
        total = 0.0
        for i in range(len(points)):
            x1, y1 = points[i]
            x2, y2 = points[(i + 1) % len(points)]
            total += x1 * y2 - x2 * y1
        return abs(total) / 2

    def contains(self, x, y):
        """Ray-casting point-in-polygon test on normalized coordinates."""
        inside = False
        points = self.polygon
        j = len(points) - 1
        for i in range(len(points)):
            xi, yi = points[i]
            xj, yj = points[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
        return inside


def capture_upload_path(instance, filename):
    return f'captures/{instance.node.code}/{timezone.localdate():%Y/%m/%d}/{filename}'


def redacted_upload_path(instance, filename):
    return f'redacted/{instance.node.code}/{timezone.localdate():%Y/%m/%d}/{filename}'


class Capture(models.Model):
    """One photo relayed from a node. Also acts as the YOLO job queue via `status`."""

    class Status(models.TextChoices):
        PENDING = 'pending', _('Awaiting detection')
        PROCESSING = 'processing', _('Processing')
        DONE = 'done', _('Detected')
        FAILED = 'failed', _('Failed')

    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name='captures')
    sequence = models.PositiveIntegerField(help_text='Frame counter sent by the node.')
    captured_at = models.DateTimeField(db_index=True)
    received_at = models.DateTimeField(default=timezone.now)
    image = models.FileField(upload_to=capture_upload_path)
    redacted_image = models.FileField(upload_to=redacted_upload_path, blank=True,
                                      help_text='Copy with faces and plates blurred; the only one shown on the dashboard.')
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    checksum = models.CharField(max_length=64, blank=True, help_text='Checksum of the reassembled JPEG.')

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ['-captured_at']
        constraints = [
            models.UniqueConstraint(fields=['node', 'sequence', 'captured_at'], name='unique_capture_per_node'),
        ]

    def __str__(self):
        return f'{self.node.code} #{self.sequence} @ {self.captured_at:%Y-%m-%d %H:%M}'


class InferenceRun(models.Model):
    """One model pass over one capture.

    Several runs per capture let us compare models on the same image
    (e.g. YOLO26n on COCO vs. YOLOE-26 text prompts vs. our fine-tuned model).
    """

    capture = models.ForeignKey(Capture, on_delete=models.CASCADE, related_name='runs')
    model_name = models.CharField(max_length=100, help_text='e.g. "yolo26n.pt", "yoloe-26s-seg.pt".')
    model_version = models.CharField(max_length=50, blank=True)
    confidence_threshold = models.FloatField(null=True, blank=True)
    inference_ms = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    # Sidewalk found automatically in this image by a segmentation model (no training,
    # no hand-drawn zone): YOLOE prompted with "sidewalk", or YOLO26-sem (Cityscapes).
    sidewalk_model = models.CharField(max_length=100, blank=True)
    sidewalk_polygons = models.JSONField(null=True, blank=True,
                                         help_text='List of polygons, each a list of [x, y] normalized 0-1.')
    sidewalk_coverage = models.FloatField(null=True, blank=True, help_text='Share of the image that is sidewalk (0-1).')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.model_name} on {self.capture}'


class Detection(models.Model):
    """One bounding box from YOLO. Coordinates are normalized 0-1 (x1, y1 = top-left)."""

    run = models.ForeignKey(InferenceRun, on_delete=models.CASCADE, related_name='detections')
    class_id = models.IntegerField(null=True, blank=True)
    class_name = models.CharField(max_length=64, db_index=True)
    confidence = models.FloatField()
    x1 = models.FloatField()
    y1 = models.FloatField()
    x2 = models.FloatField()
    y2 = models.FloatField()
    zone = models.ForeignKey(SidewalkZone, on_delete=models.SET_NULL, null=True, blank=True, related_name='detections',
                             help_text='Set when the box ground point lies on the sidewalk.')
    on_sidewalk = models.BooleanField(null=True, blank=True,
                                      help_text="Standing on the automatically detected sidewalk (null = not checked).")
    sidewalk_overlap = models.FloatField(null=True, blank=True,
                                         help_text="Share of the object's footprint strip that is sidewalk (0-1).")

    class Meta:
        ordering = ['-confidence']

    def __str__(self):
        return f'{self.class_name} {self.confidence:.2f}'

    @property
    def is_on_sidewalk(self):
        """Automatic sidewalk when it was checked, else the hand-drawn zone."""
        if self.on_sidewalk is not None:
            return self.on_sidewalk
        return self.zone_id is not None

    @property
    def ground_point(self):
        """Bottom-centre of the box: where the object touches the ground."""
        return ((self.x1 + self.x2) / 2, self.y2)

    @property
    def area(self):
        return max(0.0, self.x2 - self.x1) * max(0.0, self.y2 - self.y1)


class Violation(models.Model):
    """A sidewalk encroachment, possibly seen across many captures over time."""

    class Category(models.TextChoices):
        GOODS = 'goods', _('Displayed goods')
        FURNITURE = 'furniture', _('Tables and chairs')
        STALL = 'stall', _('Stall or kiosk')
        VEHICLE = 'vehicle', _('Parked vehicle')
        WASTE = 'waste', _('Waste')
        OTHER = 'other', _('Other')

    class Status(models.TextChoices):
        DETECTED = 'detected', _('Awaiting review')
        CONFIRMED = 'confirmed', _('Confirmed')
        REJECTED = 'rejected', _('Rejected')
        RESOLVED = 'resolved', _('Removed')

    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name='violations')
    zone = models.ForeignKey(SidewalkZone, on_delete=models.SET_NULL, null=True, blank=True, related_name='violations')
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.OTHER)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DETECTED, db_index=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    captures = models.ManyToManyField(Capture, related_name='violations', blank=True)

    # Impact score inputs from the MVP canvas: blocked width, duration, repeats.
    occupancy_ratio = models.FloatField(null=True, blank=True, help_text='Share of the sidewalk zone covered (0-1).')
    blocked_width_m = models.FloatField(null=True, blank=True)
    repeat_count = models.PositiveIntegerField(default=1)
    impact_score = models.FloatField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='violations_created')

    class Meta:
        ordering = ['-impact_score', '-last_seen_at']

    def __str__(self):
        return f'{self.get_category_display()} at {self.node.code}'

    @property
    def duration(self):
        return self.last_seen_at - self.first_seen_at


class Review(models.Model):
    """The human check behind the Wizard of Oz MVP."""

    class Decision(models.TextChoices):
        CONFIRM = 'confirm', _('Confirm')
        REJECT = 'reject', _('Reject')

    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name='reviews')
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='reviews')
    decision = models.CharField(max_length=16, choices=Decision.choices)
    impact_score = models.FloatField(null=True, blank=True, help_text='Reviewer score; overrides the computed one.')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']


class Alert(models.Model):
    """A verified alert sent to the Directorate's inspectors, and what they did with it.

    The pilot's continue/stop metric is the share of alerts with an inspector
    response within 48 hours.
    """

    class Channel(models.TextChoices):
        WHATSAPP = 'whatsapp', _('WhatsApp')
        DASHBOARD = 'dashboard', _('Dashboard')

    class Response(models.TextChoices):
        NONE = 'none', _('No response yet')
        ACKNOWLEDGED = 'acknowledged', _('Acknowledged')
        DISPATCHED = 'dispatched', _('Inspector dispatched')
        RESOLVED = 'resolved', _('Removed on site')
        IGNORED = 'ignored', _('Declined or ignored')

    violation = models.ForeignKey(Violation, on_delete=models.CASCADE, related_name='alerts')
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.WHATSAPP)
    sent_at = models.DateTimeField(default=timezone.now)
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='alerts_sent')
    response = models.CharField(max_length=16, choices=Response.choices, default=Response.NONE)
    responded_at = models.DateTimeField(null=True, blank=True)
    response_notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-sent_at']

    @property
    def responded_within_48h(self):
        if self.responded_at is None or self.response in (self.Response.NONE, self.Response.IGNORED):
            return False
        return self.responded_at - self.sent_at <= timedelta(hours=48)
