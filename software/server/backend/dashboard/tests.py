import json
from io import StringIO
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from monitoring import services
from monitoring.models import Alert, Capture, Node, SidewalkZone, Violation

TEST_MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=TEST_MEDIA)
class DashboardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', stdout=StringIO())
        cls.user = get_user_model().objects.create_user('reviewer', password='pass-12345')

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA, ignore_errors=True)

    def setUp(self):
        self.client.force_login(self.user)

    def test_pages_require_login(self):
        self.client.logout()
        response = self.client.get(reverse('dashboard:overview'))
        self.assertRedirects(response, f"{reverse('login')}?next=/")

    def test_every_page_renders(self):
        violation = Violation.objects.first()
        capture = Capture.objects.filter(runs__isnull=False).first()
        node = Node.objects.first()
        zone = node.zones.first()
        urls = [
            reverse('dashboard:overview'),
            reverse('dashboard:violation_list'),
            reverse('dashboard:violation_list') + '?status=all&category=goods',
            reverse('dashboard:violation_detail', args=[violation.pk]),
            reverse('dashboard:capture_list'),
            reverse('dashboard:capture_list') + '?status=failed',
            reverse('dashboard:capture_detail', args=[capture.pk]),
            reverse('dashboard:node_list'),
            reverse('dashboard:node_create'),
            reverse('dashboard:node_detail', args=[node.pk]),
            reverse('dashboard:node_edit', args=[node.pk]),
            reverse('dashboard:zone_create', args=[node.pk]),
            reverse('dashboard:zone_edit', args=[node.pk, zone.pk]),
            reverse('dashboard:alert_list'),
            reverse('dashboard:hotspots'),
            reverse('dashboard:hotspots') + '?week=1',
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_report_review_alert_and_response_flow(self):
        node = Node.objects.create(code='T01')
        SidewalkZone.objects.create(node=node, polygon=[[0, 0.5], [1, 0.5], [1, 1], [0, 1]], sidewalk_width_m=4)
        capture = Capture.objects.create(node=node, sequence=1, captured_at=timezone.now(), image='x.svg')

        response = self.client.post(reverse('dashboard:capture_detail', args=[capture.pk]), {
            'category': Violation.Category.FURNITURE, 'blocked_width_m': '2', 'notes': '',
        })
        violation = Violation.objects.get(node=node)
        self.assertRedirects(response, reverse('dashboard:violation_detail', args=[violation.pk]))
        self.assertEqual(violation.status, Violation.Status.DETECTED)
        self.assertEqual(violation.impact_score, 25.0)  # 2 m of 4 m blocked -> 50 * 0.5

        # Alerts are refused until the violation is confirmed.
        self.client.post(reverse('dashboard:violation_alert', args=[violation.pk]), {'channel': 'whatsapp'})
        self.assertFalse(Alert.objects.filter(violation=violation).exists())

        self.client.post(reverse('dashboard:violation_review', args=[violation.pk]),
                         {'decision': 'confirm', 'impact_score': '80'})
        violation.refresh_from_db()
        self.assertEqual(violation.status, Violation.Status.CONFIRMED)
        self.assertEqual(violation.impact_score, 80)

        self.client.post(reverse('dashboard:violation_alert', args=[violation.pk]), {'channel': 'whatsapp'})
        alert = Alert.objects.get(violation=violation)

        self.client.post(reverse('dashboard:alert_response', args=[alert.pk]), {'response': 'resolved'})
        alert.refresh_from_db()
        violation.refresh_from_db()
        self.assertTrue(alert.responded_within_48h)
        self.assertEqual(violation.status, Violation.Status.RESOLVED)

    def test_repeat_sightings_extend_one_violation(self):
        node = Node.objects.create(code='T02')
        now = timezone.now()
        first = Capture.objects.create(node=node, sequence=1, captured_at=now - timedelta(hours=3), image='a.svg')
        second = Capture.objects.create(node=node, sequence=2, captured_at=now, image='b.svg')
        services.report_violation(first, Violation.Category.GOODS)
        violation, created = services.report_violation(second, Violation.Category.GOODS)
        self.assertFalse(created)
        self.assertEqual(violation.repeat_count, 2)
        self.assertEqual(violation.duration, timedelta(hours=3))
        self.assertEqual(Violation.objects.filter(node=node).count(), 1)

    def test_zone_editor_saves_polygon(self):
        node = Node.objects.create(code='T03')
        response = self.client.post(reverse('dashboard:zone_create', args=[node.pk]), {
            'name': 'Sidewalk', 'sidewalk_width_m': '3', 'is_active': 'on',
            'polygon': json.dumps([[0, 0.6], [1, 0.55], [1, 0.9]]),
        })
        self.assertRedirects(response, reverse('dashboard:node_detail', args=[node.pk]))
        self.assertEqual(node.zones.get().polygon, [[0, 0.6], [1, 0.55], [1, 0.9]])

    def test_zone_editor_rejects_too_few_points(self):
        node = Node.objects.create(code='T04')
        response = self.client.post(reverse('dashboard:zone_create', args=[node.pk]), {
            'name': 'Sidewalk', 'polygon': json.dumps([[0, 0.6], [1, 0.55]]),
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(node.zones.exists())

    def test_seed_demo_clear_removes_only_demo_data(self):
        Node.objects.create(code='REAL-01')
        call_command('seed_demo', clear=True, stdout=StringIO())
        self.assertEqual(list(Node.objects.values_list('code', flat=True)), ['REAL-01'])

    def test_capture_list_is_live_on_first_page_only(self):
        first = self.client.get(reverse('dashboard:capture_list') + '?node=DEMO-01').content.decode()
        self.assertIn('data-feed-url="/captures/feed/?node=DEMO-01"', first)
        second = self.client.get(reverse('dashboard:capture_list') + '?page=2').content.decode()
        self.assertNotIn('data-feed-url', second)

    def test_live_feed_returns_new_and_finished_captures(self):
        node = Node.objects.create(code='LIVE-01')
        other = Node.objects.create(code='LIVE-02')
        now = timezone.now()
        waiting = Capture.objects.create(node=node, sequence=1, captured_at=now, image='w.jpg')
        latest_id = Capture.objects.order_by('-pk').first().pk
        feed = reverse('dashboard:capture_feed')

        # Nothing new yet.
        data = self.client.get(feed, {'node': 'LIVE-01', 'after': latest_id, 'watch': waiting.pk}).json()
        self.assertEqual((data['new'], data['changed'], data['latest_id']), ([], [], latest_id))

        # A new image for this node arrives (and one for another node, which the filter must hide),
        # and YOLO finishes the waiting one.
        fresh = Capture.objects.create(node=node, sequence=2, captured_at=now, image='f.jpg')
        Capture.objects.create(node=other, sequence=1, captured_at=now, image='o.jpg')
        services.record_inference(waiting, 'yolo26n.pt', [
            {'class_name': 'person', 'confidence': 0.9, 'bbox': [0.1, 0.1, 0.2, 0.5]},
        ])

        data = self.client.get(feed, {'node': 'LIVE-01', 'after': latest_id, 'watch': waiting.pk}).json()
        self.assertEqual([item['id'] for item in data['new']], [fresh.pk])
        self.assertIn(f'data-id="{fresh.pk}"', data['new'][0]['html'])
        self.assertEqual([(item['id'], item['status']) for item in data['changed']], [(waiting.pk, 'done')])
        self.assertEqual(data['latest_id'], fresh.pk)

    def test_live_feed_requires_login_and_valid_parameters(self):
        self.assertEqual(self.client.get(reverse('dashboard:capture_feed'), {'after': 'x'}).status_code, 400)
        self.client.logout()
        response = self.client.get(reverse('dashboard:capture_feed'))
        self.assertEqual(response.status_code, 302)
