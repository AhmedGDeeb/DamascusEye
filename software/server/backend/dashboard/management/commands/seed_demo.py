"""Fill the database with a week of fake corridor data, to try the dashboard
before real nodes are online.

    python manage.py seed_demo           # add demo data
    python manage.py seed_demo --clear   # remove it again

Everything it creates belongs to nodes whose code starts with "DEMO-".
Images are simple SVG drawings of a sidewalk, not real photos.
"""

import random
from datetime import timedelta

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from monitoring import services
from monitoring.models import Alert, Capture, Heartbeat, Node, Review, SidewalkZone, Violation

PREFIX = 'DEMO-'
SIDEWALK = [[0.0, 0.55], [1.0, 0.5], [1.0, 0.8], [0.0, 0.86]]
W, H = 640, 480

# (code, name, lat, lng, what usually stands on its sidewalk)
NODES = [
    ('DEMO-01', 'شارع الحمراء — مقهى', 33.513100, 36.291700, 'cafe'),
    ('DEMO-02', 'شارع الحمراء — محل ألبسة', 33.513400, 36.292500, 'goods'),
    ('DEMO-03', 'شارع الحمراء — موقف دراجات', 33.513700, 36.293300, 'motorcycles'),
    ('DEMO-04', 'شارع الحمراء — مدخل بناء', 33.514000, 36.294100, 'clear'),
]

SCENES = {
    'cafe': [('dining table', 0.18, 0.58, 0.34, 0.74), ('chair', 0.12, 0.62, 0.18, 0.76),
             ('chair', 0.34, 0.61, 0.40, 0.75), ('dining table', 0.52, 0.57, 0.66, 0.72)],
    'goods': [('goods display', 0.56, 0.52, 0.86, 0.74), ('goods display', 0.30, 0.56, 0.44, 0.72)],
    'motorcycles': [('motorcycle', 0.10, 0.56, 0.30, 0.80), ('motorcycle', 0.36, 0.55, 0.55, 0.78)],
    'clear': [],
}

COLORS = {'dining table': '#8a5a3b', 'chair': '#b07a4f', 'goods display': '#c2553a',
          'motorcycle': '#2d3b55', 'person': '#3c6e91'}


def scene_svg(objects, label):
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
        '<rect width="640" height="480" fill="#d9d2c3"/>',
        '<rect y="0" width="640" height="250" fill="#c9bfa9"/>',
        '<rect x="40" y="70" width="150" height="170" fill="#6d7d86"/><rect x="250" y="70" width="150" height="170" fill="#6d7d86"/>',
        '<rect x="460" y="70" width="140" height="170" fill="#6d7d86"/>',
        '<rect x="40" y="40" width="560" height="22" fill="#9d3d2f"/>',
        f'<polygon points="{" ".join(f"{x * W},{y * H}" for x, y in SIDEWALK)}" fill="#bfb8aa"/>',
        f'<polygon points="0,{0.86 * H} 640,{0.8 * H} 640,480 0,480" fill="#4a4d50"/>',
        f'<line x1="0" y1="{0.86 * H}" x2="640" y2="{0.8 * H}" stroke="#eee" stroke-width="5"/>',
    ]
    for name, x1, y1, x2, y2 in objects:
        color = COLORS.get(name, '#777')
        px, py, pw, ph = x1 * W, y1 * H, (x2 - x1) * W, (y2 - y1) * H
        if name == 'person':
            parts.append(f'<circle cx="{px + pw / 2}" cy="{py + pw * 0.45}" r="{pw * 0.42}" fill="#e0b48f"/>')
            parts.append(f'<rect x="{px}" y="{py + pw}" width="{pw}" height="{ph - pw}" rx="{pw / 3}" fill="{color}"/>')
        elif name == 'motorcycle':
            parts.append(f'<rect x="{px}" y="{py + ph * 0.25}" width="{pw}" height="{ph * 0.45}" rx="10" fill="{color}"/>')
            for cx in (px + pw * 0.2, px + pw * 0.8):
                parts.append(f'<circle cx="{cx}" cy="{py + ph * 0.8}" r="{ph * 0.2}" fill="#111"/>')
        else:
            parts.append(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="4" fill="{color}"/>')
    parts.append(f'<text x="12" y="470" font-family="monospace" font-size="14" fill="#fff">{label}</text>')
    parts.append('</svg>')
    return ''.join(parts)


class Command(BaseCommand):
    help = 'Create (or with --clear, remove) a week of demo data for the dashboard.'

    def add_arguments(self, parser):
        parser.add_argument('--clear', action='store_true', help='Remove all DEMO- nodes and their data.')

    def handle(self, *args, clear=False, **options):
        self.clear()
        if clear:
            self.stdout.write(self.style.SUCCESS('Demo data removed.'))
            return
        self.seed()

    def clear(self):
        demo_nodes = Node.objects.filter(code__startswith=PREFIX)
        for capture in Capture.objects.filter(node__in=demo_nodes):
            capture.image.delete(save=False)
            if capture.redacted_image:
                capture.redacted_image.delete(save=False)
        demo_nodes.delete()

    @transaction.atomic
    def seed(self):
        rng = random.Random(42)
        now = timezone.now()
        start = timezone.localtime(now - timedelta(days=7)).replace(minute=0, second=0, microsecond=0)

        nodes = []
        for code, name, lat, lng, scene in NODES:
            node = Node.objects.create(
                code=code, name=name, latitude=lat, longitude=lng, mount_height_m=3.2,
                installed_at=start - timedelta(days=1), notes='بيانات تجريبية من seed_demo.',
            )
            SidewalkZone.objects.create(node=node, name='الرصيف', polygon=SIDEWALK, sidewalk_width_m=3.5)
            nodes.append((node, scene))

        # DEMO-04 went silent three hours ago: shows up as offline (theft check).
        offline_since = now - timedelta(hours=3)

        for node, scene in nodes:
            last_alive = offline_since if node.code == 'DEMO-04' else now
            t = now - timedelta(hours=48)
            while t <= last_alive:
                hour = timezone.localtime(t).hour
                sun = 1 if 7 <= hour <= 17 else 0
                Heartbeat.objects.create(
                    node=node, received_at=t, rssi=rng.randint(-82, -60), uptime_s=rng.randint(100, 90000),
                    battery_voltage=round(3.65 + 0.35 * sun * (hour - 7) / 10 + rng.uniform(-0.03, 0.03), 2),
                )
                t += timedelta(minutes=20)
            node.last_seen_at = last_alive
            node.battery_voltage = 3.92 if node.code != 'DEMO-04' else 3.71
            node.save()

        captures = []
        sequence = {node.pk: 0 for node, _ in nodes}
        t = start
        while t <= now:
            local_hour = timezone.localtime(t).hour
            open_hours = 10 <= local_hour <= 19
            for node, scene in nodes:
                if node.code == 'DEMO-04' and t > offline_since:
                    continue
                sequence[node.pk] += 1
                objects = list(SCENES[scene]) if open_hours and rng.random() > 0.1 else []
                for _ in range(rng.randint(0, 2)):
                    x = rng.uniform(0.05, 0.9)
                    y = rng.uniform(0.40, 0.62)
                    objects.append(('person', x, y, x + 0.05, y + 0.22))
                label = f'{node.code}  {timezone.localtime(t):%Y-%m-%d %H:%M}'
                capture = Capture(node=node, sequence=sequence[node.pk], captured_at=t, width=W, height=H,
                                  received_at=t + timedelta(seconds=rng.randint(5, 40)))
                capture.image.save(f'{node.code}_{sequence[node.pk]:05d}.svg',
                                   ContentFile(scene_svg(objects, label).encode()), save=False)
                capture.save()
                captures.append((capture, scene, objects, open_hours))
            t += timedelta(hours=3)

        # YOLO results. The newest few stay in the queue; one fails.
        for capture, scene, objects, _ in captures[:-4]:
            detections = []
            for name, x1, y1, x2, y2 in objects:
                if name == 'goods display':
                    continue  # COCO has no "goods" class: only YOLOE text prompts find them.
                detections.append({'class_name': name, 'confidence': round(rng.uniform(0.55, 0.95), 2),
                                   'bbox': [x1, y1, x2, y2]})
            services.record_inference(capture, 'yolo26n.pt', detections, model_version='8.4',
                                      confidence_threshold=0.25, inference_ms=round(rng.uniform(38, 60), 1))
            if scene == 'goods':
                prompted = [{'class_name': name, 'confidence': round(rng.uniform(0.45, 0.85), 2),
                             'bbox': [x1, y1, x2, y2]} for name, x1, y1, x2, y2 in objects]
                services.record_inference(capture, 'yoloe-26s-seg.pt', prompted, model_version='8.4',
                                          confidence_threshold=0.2, inference_ms=round(rng.uniform(90, 130), 1))
        failed = captures[-5][0]
        services.mark_failed(failed, 'JPEG checksum mismatch after ESP-NOW reassembly (12/97 chunks missing).')

        # Wizard of Oz review: the team reports what they see on each image.
        category_for = {'cafe': Violation.Category.FURNITURE, 'goods': Violation.Category.GOODS,
                        'motorcycles': Violation.Category.VEHICLE}
        width_for = {'cafe': 1.6, 'goods': 2.2, 'motorcycles': 1.9}
        for capture, scene, objects, open_hours in captures[:-4]:
            if scene not in category_for or not any(o[0] != 'person' for o in objects):
                continue
            services.report_violation(capture, category_for[scene],
                                      blocked_width_m=round(width_for[scene] + rng.uniform(-0.3, 0.3), 1))

        # Reviews and alerts on older violations; the last day's stay "awaiting review".
        review_before = now - timedelta(hours=20)
        statuses_to_send = [Alert.Response.DISPATCHED, Alert.Response.RESOLVED, Alert.Response.ACKNOWLEDGED,
                            Alert.Response.DISPATCHED, Alert.Response.NONE, Alert.Response.IGNORED]
        for i, violation in enumerate(Violation.objects.filter(node__code__startswith=PREFIX)
                                      .order_by('first_seen_at')):
            if violation.last_seen_at >= review_before:
                continue
            reviewed_at = violation.last_seen_at + timedelta(minutes=40)
            if i % 7 == 3:
                review = services.review_violation(violation, None, Review.Decision.REJECT,
                                                   notes='بضاعة تُنقل إلى المحل، ليست إشغالاً.')
                Review.objects.filter(pk=review.pk).update(created_at=reviewed_at)
                continue
            review = services.review_violation(violation, None, Review.Decision.CONFIRM)
            Review.objects.filter(pk=review.pk).update(created_at=reviewed_at)

            response = statuses_to_send[i % len(statuses_to_send)]
            sent_at = reviewed_at + timedelta(minutes=10)
            delay = timedelta(hours=rng.choice([3, 9, 20, 30, 60]))
            responded_at = None if response == Alert.Response.NONE else min(sent_at + delay, now)
            Alert.objects.create(violation=violation, channel=Alert.Channel.WHATSAPP, sent_at=sent_at,
                                 response=response, responded_at=responded_at)
            if response == Alert.Response.RESOLVED:
                violation.status = Violation.Status.RESOLVED
                violation.save(update_fields=['status'])

        self.stdout.write(self.style.SUCCESS(
            f'Demo data: {len(nodes)} nodes, {len(captures)} captures, '
            f'{Violation.objects.filter(node__code__startswith=PREFIX).count()} violations, '
            f'{Alert.objects.filter(violation__node__code__startswith=PREFIX).count()} alerts.'
        ))
