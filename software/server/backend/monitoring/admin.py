from django.contrib import admin

from .models import (
    Alert, Capture, Detection, Heartbeat, InferenceRun, Node, Review, SidewalkZone, Violation,
)


class SidewalkZoneInline(admin.TabularInline):
    model = SidewalkZone
    extra = 0


@admin.register(Node)
class NodeAdmin(admin.ModelAdmin):
    list_display = ['code', 'name', 'status', 'is_online', 'last_seen_at', 'battery_voltage']
    list_filter = ['status']
    search_fields = ['code', 'name', 'mac_address']
    inlines = [SidewalkZoneInline]

    @admin.display(boolean=True)
    def is_online(self, obj):
        return obj.is_online


@admin.register(Heartbeat)
class HeartbeatAdmin(admin.ModelAdmin):
    list_display = ['node', 'received_at', 'battery_voltage', 'rssi', 'uptime_s']
    list_filter = ['node']


@admin.register(SidewalkZone)
class SidewalkZoneAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'sidewalk_width_m', 'is_active']
    list_filter = ['node', 'is_active']


class InferenceRunInline(admin.TabularInline):
    model = InferenceRun
    extra = 0
    fields = ['model_name', 'model_version', 'inference_ms', 'created_at']
    readonly_fields = fields
    show_change_link = True


@admin.register(Capture)
class CaptureAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'node', 'status', 'attempts', 'received_at', 'processed_at']
    list_filter = ['status', 'node']
    date_hierarchy = 'captured_at'
    inlines = [InferenceRunInline]
    actions = ['requeue']

    @admin.action(description='Send back to the YOLO queue')
    def requeue(self, request, queryset):
        count = queryset.update(status=Capture.Status.PENDING, claimed_at=None, error='')
        self.message_user(request, f'{count} capture(s) re-queued.')


class DetectionInline(admin.TabularInline):
    model = Detection
    extra = 0
    fields = ['class_name', 'confidence', 'x1', 'y1', 'x2', 'y2', 'zone']
    readonly_fields = fields


@admin.register(InferenceRun)
class InferenceRunAdmin(admin.ModelAdmin):
    list_display = ['capture', 'model_name', 'model_version', 'inference_ms', 'created_at']
    list_filter = ['model_name']
    inlines = [DetectionInline]


@admin.register(Detection)
class DetectionAdmin(admin.ModelAdmin):
    list_display = ['class_name', 'confidence', 'run', 'zone']
    list_filter = ['class_name', 'run__model_name', ('zone', admin.EmptyFieldListFilter)]


class ReviewInline(admin.TabularInline):
    model = Review
    extra = 0


class AlertInline(admin.TabularInline):
    model = Alert
    extra = 0


@admin.register(Violation)
class ViolationAdmin(admin.ModelAdmin):
    list_display = ['__str__', 'category', 'status', 'impact_score', 'repeat_count', 'first_seen_at', 'last_seen_at']
    list_filter = ['status', 'category', 'node']
    filter_horizontal = ['captures']
    inlines = [ReviewInline, AlertInline]


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ['violation', 'channel', 'sent_at', 'response', 'responded_at', 'within_48h']
    list_filter = ['channel', 'response']

    @admin.display(boolean=True, description='< 48 h')
    def within_48h(self, obj):
        return obj.responded_within_48h
