"""Damascus Eye YOLO manager.

Takes queued images from the Django API, runs one or more YOLO models on them
and posts the detections back. Talks to the backend only over HTTP, so it can
run on the same laptop or on a separate machine with a GPU.

    python manager.py run                    # keep processing the queue
    python manager.py run --once             # process what is queued, then exit
    python manager.py run --model yolo26n.pt --model yoloe-26s-seg.pt \
        --prompts "goods display,plastic chair,table,stall,cardboard boxes"
    python manager.py ingest --node N01 photos/   # send phone photos as if a node took them

By default each image is also segmented to find the sidewalk without any training
(--sidewalk yoloe, or --sidewalk sem for YOLO26-sem/Cityscapes), and every object
is marked on or off the sidewalk from the strip under its base.

Settings come from flags or the environment:
    DAMASCUS_EYE_API      default http://127.0.0.1:8000/api
    DAMASCUS_EYE_API_KEY  default dev-api-key (same default as the backend)
"""

import argparse
import glob
import io
import json
import logging
import mimetypes
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger('yolo-manager')

IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.bmp', '.webp'}


# API client

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f'HTTP {status}: {message}')
        self.status = status


class Api:
    """Minimal client for backend/monitoring/views.py (stdlib only)."""

    def __init__(self, base_url, key, timeout=60):
        self.base_url = base_url.rstrip('/')
        self.key = key
        self.timeout = timeout

    def _send(self, method, path, body=None, content_type=None):
        url = path if path.startswith('http') else f'{self.base_url}/{path.lstrip("/")}'
        headers = {'X-API-Key': self.key, 'Accept': 'application/json'}
        if content_type:
            headers['Content-Type'] = content_type
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')
            try:
                detail = json.loads(detail).get('error', detail)
            except ValueError:
                detail = detail[:300]
            raise ApiError(exc.code, detail) from None

    def _json(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        raw = self._send(method, path, body, 'application/json' if body is not None else None)
        return json.loads(raw or b'{}')

    def claim(self, limit):
        return self._json('POST', 'yolo/claim/', {'limit': limit})['captures']

    def submit(self, capture_id, payload):
        return self._json('POST', f'yolo/results/{capture_id}/', payload)

    def fail(self, capture_id, error):
        return self._json('POST', f'yolo/results/{capture_id}/', {'error': str(error)[:2000]})

    def download(self, url):
        return self._send('GET', url)

    def upload_capture(self, fields, file_path):
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        mime = mimetypes.guess_type(file_path.name)[0] or 'application/octet-stream'
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{file_path.name}"\r\n'
            f'Content-Type: {mime}\r\n\r\n'.encode() + file_path.read_bytes() + b'\r\n'
        )
        parts.append(f'--{boundary}--\r\n'.encode())
        raw = self._send('POST', 'captures/', b''.join(parts), f'multipart/form-data; boundary={boundary}')
        return json.loads(raw)


# Models

class Detector:
    """One YOLO model, loaded once and reused for every image."""

    def __init__(self, weights, conf, imgsz, device=None, prompts=None, classes=None):
        try:
            import ultralytics
            from ultralytics import YOLO
        except ImportError:
            raise SystemExit('ultralytics is not installed in this environment. See yolo_manager/README.md '
                             '(it needs the official Python from python.org, not the Inkscape one).')

        self.name = Path(weights).name
        self.version = ultralytics.__version__
        self.conf = conf
        self.imgsz = imgsz
        self.device = device
        self.keep = set(classes) if classes else None
        self.model = YOLO(weights)  # also returns a YOLOE model for yoloe-* weights

        if prompts:
            if not hasattr(self.model, 'set_classes'):
                raise SystemExit(f'--prompts needs an open-vocabulary model (yoloe-*), not {self.name}.')
            try:
                self.model.set_classes(prompts)
            except TypeError:  # older ultralytics: text embeddings passed explicitly
                self.model.set_classes(prompts, self.model.get_text_pe(prompts))

    def detect(self, image):
        result = self.model.predict(image, conf=self.conf, imgsz=self.imgsz, device=self.device, verbose=False)[0]
        names = result.names
        detections = []
        boxes = result.boxes
        for (x1, y1, x2, y2), cls, conf in zip(boxes.xyxyn.tolist(), boxes.cls.tolist(), boxes.conf.tolist()):
            class_name = names[int(cls)]
            if self.keep and class_name not in self.keep:
                continue
            detections.append({
                'class_id': int(cls),
                'class_name': class_name,
                'confidence': round(float(conf), 4),
                # Clamp: boxes touching the edge can come back as 1.0000001.
                'bbox': [round(min(max(v, 0.0), 1.0), 5) for v in (x1, y1, x2, y2)],
            })
        height, width = result.orig_shape
        return {
            'model_name': self.name,
            'model_version': self.version,
            'confidence_threshold': self.conf,
            'inference_ms': round(result.speed.get('inference', 0.0), 1),
            'width': width,
            'height': height,
            'detections': detections,
        }


# Sidewalk segmentation (no training)

SIDEWALK_PROMPTS = ['sidewalk', 'pavement']
SIDEWALK_METHODS = {
    # Open-vocabulary segmentation prompted with the words above.
    'yoloe': 'yoloe-26s-seg.pt',
    # Semantic segmentation pretrained on Cityscapes; class 1 is "sidewalk".
    'sem': 'yolo26n-sem.pt',
}
CITYSCAPES_SIDEWALK = 1
MIN_REGION = 0.003         # ignore sidewalk blobs smaller than 0.3 % of the image
ON_SIDEWALK_OVERLAP = 0.4  # footprint share that must be sidewalk to count as "on it"


class SidewalkSegmenter:
    """Finds the sidewalk in an image with a pretrained model, then decides
    for each detected object whether it stands on it."""

    def __init__(self, method, conf, imgsz, device=None, weights=None):
        from ultralytics import YOLO

        self.method = method
        self.conf = conf
        self.imgsz = imgsz
        self.device = device
        weights = weights or SIDEWALK_METHODS[method]
        self.name = Path(weights).name
        self.model = YOLO(weights)
        if method == 'yoloe':
            try:
                self.model.set_classes(SIDEWALK_PROMPTS)
            except TypeError:
                self.model.set_classes(SIDEWALK_PROMPTS, self.model.get_text_pe(SIDEWALK_PROMPTS))

    def _raw_mask(self, image):
        """Boolean (H, W) sidewalk mask at the original image size."""
        import cv2
        import numpy as np

        width, height = image.size
        if self.method == 'sem':
            result = self.model.predict(image, imgsz=self.imgsz, device=self.device, verbose=False)[0]
            data = result.semantic_mask.data
            labels = data.cpu().numpy() if hasattr(data, 'cpu') else np.asarray(data)
            if labels.shape != (height, width):
                labels = cv2.resize(labels, (width, height), interpolation=cv2.INTER_NEAREST)
            return labels == CITYSCAPES_SIDEWALK

        result = self.model.predict(image, conf=self.conf, imgsz=self.imgsz, device=self.device,
                                    retina_masks=True, verbose=False)[0]
        mask = np.zeros((height, width), dtype=bool)
        if result.masks is not None:
            for instance in result.masks.data.cpu().numpy():
                if instance.shape != mask.shape:
                    instance = cv2.resize(instance, (width, height), interpolation=cv2.INTER_NEAREST)
                mask |= instance > 0.5
        return mask

    def segment(self, image):
        """Return (filled mask, payload for the backend).

        People, chairs and goods standing on the sidewalk cut holes in the raw
        mask. Closing small gaps and filling every enclosed hole gives the
        sidewalk including what stands on it, which is what we test against.
        """
        import cv2
        import numpy as np

        raw = self._raw_mask(image).astype(np.uint8)
        height, width = raw.shape
        size = max(3, int(0.02 * min(height, width)) | 1)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
        closed = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = [c for c in contours if cv2.contourArea(c) >= MIN_REGION * height * width]
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:20]
        filled = np.zeros_like(closed)
        cv2.drawContours(filled, contours, -1, 1, thickness=cv2.FILLED)

        polygons = []
        for contour in contours:
            outline = cv2.approxPolyDP(contour, 0.004 * cv2.arcLength(contour, True), True)
            points = [[round(x / width, 4), round(y / height, 4)] for [[x, y]] in outline[:400]]
            if len(points) >= 3:
                polygons.append(points)
        return filled.astype(bool), {
            'model': self.name,
            'polygons': polygons,
            'coverage': round(float(filled.mean()), 4),
        }

    @staticmethod
    def footprint_overlap(mask, bbox):
        """Share of the strip under an object's base that is sidewalk.

        The strip spans the middle 70 % of the box width, from a little above
        the bottom edge (feet, wheels, table legs) to a little below it.
        """
        height, width = mask.shape
        x1, y1, x2, y2 = bbox
        box_w, box_h = (x2 - x1) * width, (y2 - y1) * height
        left = int(x1 * width + 0.15 * box_w)
        right = max(left + 1, int(x2 * width - 0.15 * box_w))
        top = int(y2 * height - 0.08 * box_h)
        bottom = max(top + 1, int(y2 * height + 0.03 * height))
        strip = mask[max(top, 0):min(bottom, height), max(left, 0):min(right, width)]
        return float(strip.mean()) if strip.size else 0.0


def load_image(api, capture):
    """Read the JPEG from disk when the backend is on this machine, else download it."""
    from PIL import Image, UnidentifiedImageError

    path = Path(capture.get('image_path') or '')
    data = path.read_bytes() if path.is_file() else api.download(capture['image_url'])
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except UnidentifiedImageError:
        name = Path(capture.get('image_path') or capture['image_url']).name
        raise ValueError(f'{name} is not a supported image (expected JPEG or PNG)') from None
    return image.convert('RGB')


# Commands

def process(api, detectors, capture, segmenter=None):
    label = f"{capture['node']} #{capture['sequence']} (id {capture['id']})"
    try:
        image = load_image(api, capture)
    except Exception as exc:  # unreadable or missing file: tell the backend, keep going
        log.warning('%s: cannot read image: %s', label, exc)
        api.fail(capture['id'], f'Cannot read image: {exc}')
        return False

    mask = sidewalk = None
    if segmenter is not None:
        try:
            mask, sidewalk = segmenter.segment(image)
            log.info('%s: sidewalk covers %.0f%% of the image (%s)', label, sidewalk['coverage'] * 100, segmenter.name)
        except Exception as exc:
            log.exception('%s: sidewalk segmentation failed', label)
            api.fail(capture['id'], f'{segmenter.name} (sidewalk) failed: {exc}')
            return False

    for detector in detectors:
        try:
            payload = detector.detect(image)
        except Exception as exc:
            log.exception('%s: %s failed', label, detector.name)
            api.fail(capture['id'], f'{detector.name} failed: {exc}')
            return False
        if mask is not None:
            payload['sidewalk'] = sidewalk
            for item in payload['detections']:
                overlap = SidewalkSegmenter.footprint_overlap(mask, item['bbox'])
                item['sidewalk_overlap'] = round(overlap, 3)
                item['on_sidewalk'] = overlap >= ON_SIDEWALK_OVERLAP
        reply = api.submit(capture['id'], payload)
        log.info('%s: %s found %d object(s), %d on the sidewalk, %.0f ms',
                 label, detector.name, reply['detections'], reply['on_sidewalk'], payload['inference_ms'])
    return True


def command_run(args):
    api = Api(args.api, args.key)
    prompts = [p.strip() for p in args.prompts.split(',') if p.strip()] if args.prompts else None
    classes = [c.strip() for c in args.classes.split(',') if c.strip()] if args.classes else None

    detectors = []
    for weights in args.model or ['yolo26n.pt']:
        log.info('Loading %s ...', weights)
        is_yoloe = 'yoloe' in Path(weights).stem
        detectors.append(Detector(weights, args.conf, args.imgsz, args.device,
                                  prompts=prompts if is_yoloe else None, classes=classes))
    segmenter = None
    if args.sidewalk != 'off':
        log.info('Loading sidewalk model (%s) ...', args.sidewalk)
        segmenter = SidewalkSegmenter(args.sidewalk, args.sidewalk_conf, args.imgsz, args.device,
                                      weights=args.sidewalk_model)
    log.info('Ready. Backend: %s', api.base_url)

    done = failed = 0
    backoff = args.poll
    try:
        while True:
            try:
                captures = api.claim(args.batch)
                backoff = args.poll
            except ApiError as exc:
                if exc.status == 401:
                    raise SystemExit('The backend rejected the API key. Set --key or DAMASCUS_EYE_API_KEY.')
                log.error('Backend error: %s (retrying in %ss)', exc, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            except URLError as exc:
                log.error('Cannot reach %s: %s (retrying in %ss)', api.base_url, exc.reason, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            if not captures:
                if args.once:
                    break
                time.sleep(args.poll)
                continue

            for capture in captures:
                try:
                    ok = process(api, detectors, capture, segmenter)
                except (ApiError, URLError) as exc:
                    # The capture stays "processing" and is handed out again after the backend timeout.
                    log.error('Could not report capture %s: %s', capture['id'], exc)
                    ok = False
                done += ok
                failed += not ok
    except KeyboardInterrupt:
        log.info('Stopped. Images claimed but not finished return to the queue after the backend timeout.')
    log.info('Processed %d image(s), %d failed.', done, failed)
    return 0 if failed == 0 else 1


def _captured_at(path):
    """EXIF DateTimeOriginal when the photo has it, else the file's modification time."""
    try:
        from PIL import Image
        with Image.open(path) as image:
            exif = image.getexif()
            stamp = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, DateTime
        if stamp:
            return datetime.strptime(stamp, '%Y:%m:%d %H:%M:%S').astimezone()
    except Exception:
        pass
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def _image_size(path):
    try:
        from PIL import Image
        with Image.open(path) as image:
            return image.size
    except Exception:
        return None, None


def command_ingest(args):
    """Upload existing photos (e.g. taken by phone on the corridor) into the queue."""
    api = Api(args.api, args.key)
    files = []
    for pattern in args.paths:
        path = Path(pattern)
        if path.is_dir():
            files += sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES)
        else:
            files += sorted(Path(p) for p in glob.glob(pattern) if Path(p).suffix.lower() in IMAGE_SUFFIXES)
    if not files:
        raise SystemExit('No images found (.jpg, .jpeg, .png, .bmp, .webp).')

    files.sort(key=_captured_at)
    created = 0
    for sequence, path in enumerate(files, start=args.start_seq):
        width, height = _image_size(path)
        fields = {'node': args.node, 'sequence': sequence, 'captured_at': _captured_at(path).isoformat()}
        if width:
            fields.update(width=width, height=height)
        try:
            reply = api.upload_capture(fields, path)
        except ApiError as exc:
            raise SystemExit(f'{path.name}: {exc}')
        created += reply['created']
        log.info('%s -> capture %s (%s)', path.name, reply['id'], 'new' if reply['created'] else 'already uploaded')
    log.info('Uploaded %d new image(s) for node %s. Run "python manager.py run --once" to detect.', created, args.node)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description='Damascus Eye YOLO manager.')
    parser.add_argument('--api', default=os.environ.get('DAMASCUS_EYE_API', 'http://127.0.0.1:8000/api'),
                        help='Backend API base URL.')
    parser.add_argument('--key', default=os.environ.get('DAMASCUS_EYE_API_KEY', 'dev-api-key'),
                        help='API key (X-API-Key).')
    parser.add_argument('-v', '--verbose', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)

    run = commands.add_parser('run', help='Process queued images.')
    run.add_argument('--model', action='append',
                     help='Weights file or name; repeat to compare models on the same images (default yolo26n.pt).')
    run.add_argument('--prompts', help='Comma-separated text prompts for yoloe-* models, e.g. "goods display,stall".')
    run.add_argument('--classes', help='Only keep these class names, e.g. "person,chair,dining table,motorcycle".')
    run.add_argument('--conf', type=float, default=0.25, help='Confidence threshold (default 0.25).')
    run.add_argument('--imgsz', type=int, default=640, help='Inference image size (default 640).')
    run.add_argument('--device', help='"cpu", "0" for the first GPU, ... (default: automatic).')
    run.add_argument('--batch', type=int, default=4, help='Images claimed per request (default 4).')
    run.add_argument('--poll', type=float, default=5, help='Seconds between checks when the queue is empty.')
    run.add_argument('--once', action='store_true', help='Exit when the queue is empty.')
    run.add_argument('--sidewalk', choices=['yoloe', 'sem', 'off'], default='yoloe',
                     help='Find the sidewalk without training: "yoloe" (text prompt "sidewalk", default), '
                          '"sem" (YOLO26-sem, Cityscapes) or "off" (use the hand-drawn zone only).')
    run.add_argument('--sidewalk-conf', type=float, default=0.1,
                     help='Confidence threshold for the yoloe sidewalk masks (default 0.1).')
    run.add_argument('--sidewalk-model', help='Override the sidewalk weights file.')
    run.set_defaults(handler=command_run)

    ingest = commands.add_parser('ingest', help='Upload photos from disk as captures of a node.')
    ingest.add_argument('--node', required=True, help='Node code registered in the dashboard, e.g. N01.')
    ingest.add_argument('--start-seq', type=int, default=1, help='First frame number (default 1).')
    ingest.add_argument('paths', nargs='+', help='Image files, folders or glob patterns.')
    ingest.set_defaults(handler=command_ingest)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S')
    return args.handler(args)


if __name__ == '__main__':
    sys.exit(main())
