import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from . import services
from .models import Capture, Detection, Node, SidewalkZone

TEST_MEDIA = tempfile.mkdtemp()
API = {'HTTP_X_API_KEY': 'test-key'}


def fake_jpeg(name='frame.jpg'):
    return SimpleUploadedFile(name, b'\xff\xd8\xff\xe0fake-jpeg\xff\xd9', content_type='image/jpeg')


@override_settings(MEDIA_ROOT=TEST_MEDIA, DAMASCUS_EYE_API_KEY='test-key')
class PipelineTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA, ignore_errors=True)

    def setUp(self):
        self.node = Node.objects.create(code='N01', name='Corridor test node')
        # Sidewalk = bottom half of the image.
        self.zone = SidewalkZone.objects.create(
            node=self.node, polygon=[[0, 0.5], [1, 0.5], [1, 1], [0, 1]], sidewalk_width_m=3,
        )

    def upload(self, sequence=1, captured_at='2026-11-17T10:00:00+03:00'):
        return self.client.post('/api/captures/', {
            'node': 'N01', 'sequence': sequence, 'captured_at': captured_at,
            'battery_voltage': '3.9', 'image': fake_jpeg(),
        }, **API)

    def test_rejects_missing_api_key(self):
        response = self.client.post('/api/yolo/claim/', '{}', content_type='application/json')
        self.assertEqual(response.status_code, 401)

    def test_upload_queues_capture_and_updates_node(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        capture = Capture.objects.get()
        self.assertEqual(capture.status, Capture.Status.PENDING)
        self.node.refresh_from_db()
        self.assertEqual(self.node.battery_voltage, 3.9)
        self.assertTrue(self.node.is_online)

    def test_upload_retry_is_idempotent(self):
        self.upload()
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['created'])
        self.assertEqual(Capture.objects.count(), 1)

    def test_unknown_node_is_rejected(self):
        response = self.client.post('/api/heartbeats/', {'node': 'N99'}, content_type='application/json', **API)
        self.assertEqual(response.status_code, 400)

    def test_claim_then_results_full_cycle(self):
        self.upload()
        claim = self.client.post('/api/yolo/claim/', {'limit': 5}, content_type='application/json', **API).json()
        self.assertEqual(len(claim['captures']), 1)
        capture_id = claim['captures'][0]['id']
        self.assertEqual(Capture.objects.get(pk=capture_id).status, Capture.Status.PROCESSING)

        # Nothing left to claim while it is being processed.
        again = self.client.post('/api/yolo/claim/', {}, content_type='application/json', **API).json()
        self.assertEqual(again['captures'], [])

        response = self.client.post(f'/api/yolo/results/{capture_id}/', {
            'model_name': 'yolo26n.pt', 'inference_ms': 41.5,
            'detections': [
                {'class_id': 0, 'class_name': 'person', 'confidence': 0.9, 'bbox': [0.1, 0.4, 0.2, 0.8]},
                {'class_id': 56, 'class_name': 'chair', 'confidence': 0.7, 'bbox': [0.5, 0.1, 0.6, 0.3]},
            ],
        }, content_type='application/json', **API)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['on_sidewalk'], 1)

        capture = Capture.objects.get(pk=capture_id)
        self.assertEqual(capture.status, Capture.Status.DONE)
        person = Detection.objects.get(class_name='person')
        chair = Detection.objects.get(class_name='chair')
        self.assertEqual(person.zone, self.zone)   # feet at y=0.8 -> on the sidewalk
        self.assertIsNone(chair.zone)              # bottom at y=0.3 -> off the sidewalk

    def test_results_reject_pixel_coordinates(self):
        self.upload()
        capture = Capture.objects.get()
        response = self.client.post(f'/api/yolo/results/{capture.id}/', {
            'model_name': 'yolo26n.pt',
            'detections': [{'class_name': 'person', 'confidence': 0.9, 'bbox': [10, 200, 80, 400]}],
        }, content_type='application/json', **API)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Detection.objects.exists())

    def test_failure_is_recorded(self):
        self.upload()
        capture = Capture.objects.get()
        self.client.post(f'/api/yolo/results/{capture.id}/', {'error': 'corrupt JPEG'},
                         content_type='application/json', **API)
        capture.refresh_from_db()
        self.assertEqual(capture.status, Capture.Status.FAILED)
        self.assertEqual(capture.error, 'corrupt JPEG')

    def test_stale_claim_is_handed_out_again(self):
        self.upload()
        [capture] = services.claim_captures()
        Capture.objects.filter(pk=capture.pk).update(claimed_at=timezone.now() - timedelta(hours=1))
        [reclaimed] = services.claim_captures()
        self.assertEqual(reclaimed.pk, capture.pk)
        self.assertEqual(reclaimed.attempts, 2)


@override_settings(MEDIA_ROOT=TEST_MEDIA, DAMASCUS_EYE_API_KEY='test-key')
class SidewalkSegmentationTests(TestCase):
    def setUp(self):
        self.node = Node.objects.create(code='S01')
        self.capture = services.register_capture(self.node, 1, timezone.now(), fake_jpeg())[0]

    def post(self, payload):
        return self.client.post(f'/api/yolo/results/{self.capture.id}/', payload,
                                content_type='application/json', **API)

    def test_automatic_sidewalk_and_per_object_verdict_are_stored(self):
        response = self.post({
            'model_name': 'yolo26n.pt',
            'sidewalk': {'model': 'yoloe-26s-seg.pt', 'coverage': 0.3,
                         'polygons': [[[0, 0.6], [1, 0.6], [1, 1], [0, 1]]]},
            'detections': [
                {'class_name': 'chair', 'confidence': 0.8, 'bbox': [0.1, 0.5, 0.2, 0.8],
                 'on_sidewalk': True, 'sidewalk_overlap': 0.9},
                {'class_name': 'car', 'confidence': 0.7, 'bbox': [0.5, 0.2, 0.9, 0.5],
                 'on_sidewalk': False, 'sidewalk_overlap': 0.0},
            ],
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['on_sidewalk'], 1)
        run = self.capture.runs.get()
        self.assertEqual(run.sidewalk_model, 'yoloe-26s-seg.pt')
        self.assertEqual(run.sidewalk_polygons, [[[0, 0.6], [1, 0.6], [1, 1], [0, 1]]])
        chair = run.detections.get(class_name='chair')
        self.assertTrue(chair.is_on_sidewalk)
        self.assertEqual(chair.sidewalk_overlap, 0.9)
        # Occupancy now uses the detected sidewalk: chair area 0.03 / sidewalk 0.3.
        self.assertEqual(services.estimate_occupancy(None, list(run.detections.all()), run), 0.1)

    def test_without_automatic_sidewalk_the_drawn_zone_decides(self):
        SidewalkZone.objects.create(node=self.node, polygon=[[0, 0.5], [1, 0.5], [1, 1], [0, 1]])
        self.post({'model_name': 'yolo26n.pt', 'detections': [
            {'class_name': 'person', 'confidence': 0.9, 'bbox': [0.1, 0.4, 0.2, 0.8]},
        ]})
        detection = self.capture.runs.get().detections.get()
        self.assertIsNone(detection.on_sidewalk)
        self.assertTrue(detection.is_on_sidewalk)

    def test_bad_sidewalk_polygons_are_rejected(self):
        response = self.post({'model_name': 'yolo26n.pt', 'detections': [],
                              'sidewalk': {'polygons': [[[0, 0], [2, 0], [1, 1]]]}})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.capture.runs.exists())
