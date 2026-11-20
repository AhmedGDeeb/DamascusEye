"""Damascus Eye ESP bridge: ESP32-CAM preview gallery -> dashboard.

The Edge-Node firmware (referance-project/Edge-Node) in its default AP mode
creates its own WiFi network ("EdgeNode-CAM") and keeps the last kept images
in PSRAM, listed on http://192.168.4.1/. Connect this computer to that network
and run:

    python esp_bridge.py

Every new image is downloaded and posted to the backend (/api/captures/), so it
appears in the dashboard and in the YOLO queue. A heartbeat is sent while the
board answers, so the node shows as online.

Standard library only: runs with any Python 3.10+, including the Django venv.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger('esp-bridge')

# One gallery entry as written by index_get() in Edge-Node/main/main.c:
#   <p>#12 &middot; 2026-11-17 10:00:00 &middot; change 3.4% &middot; 41 KB<br><img src='/img?n=12'></p>
ENTRY = re.compile(
    r"<p>#(?P<seq>\d+) &middot; (?P<when>[^&<]*) &middot; (?P<change>[^&<]*) &middot; (?P<kb>\d+) KB<br>"
    r"<img src='/img\?n=(?P=seq)'>"
)
TITLE = re.compile(r'<title>(.*?)</title>', re.S)
SEEN_LIMIT = 500  # image hashes remembered across restarts, to never upload twice


# ESP32 side

def http_get(url, timeout):
    with urlopen(Request(url, headers={'Cache-Control': 'no-cache'}), timeout=timeout) as response:
        return response.read()


def read_gallery(esp_url, timeout):
    """Return (device_id, entries oldest first) from the board's preview page."""
    html = http_get(f'{esp_url}/', timeout).decode('utf-8', 'replace')
    title = TITLE.search(html)
    entries = [
        {
            'seq': int(m['seq']),
            'when': m['when'].strip(),
            'change': unescape(m['change']).strip(),
        }
        for m in ENTRY.finditer(html)
    ]
    entries.sort(key=lambda e: e['seq'])
    return (title.group(1).strip() if title else None), entries


def jpeg_size(data):
    """(width, height) from the JPEG SOF header, or (None, None)."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], 'big'), int.from_bytes(data[i + 5:i + 7], 'big')
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], 'big')
    return None, None


def capture_time(when):
    """The board's own timestamp when its clock was synced (STA mode), else now.

    In AP mode the board has no internet, shows "time not synced", and the
    arrival time at this computer is used instead (accurate to the poll interval).
    """
    try:
        return datetime.strptime(when, '%Y-%m-%d %H:%M:%S').astimezone()
    except ValueError:
        return datetime.now().astimezone()


# Backend side

class BackendError(Exception):
    def __init__(self, status, message):
        super().__init__(f'HTTP {status}: {message}')
        self.status = status


class Backend:
    def __init__(self, api_url, key, timeout=30):
        self.api_url = api_url.rstrip('/')
        self.key = key
        self.timeout = timeout

    def _post(self, path, body, content_type):
        request = Request(f'{self.api_url}/{path}', data=body, method='POST',
                          headers={'X-API-Key': self.key, 'Content-Type': content_type})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b'{}')
        except HTTPError as exc:
            detail = exc.read().decode('utf-8', 'replace')
            try:
                detail = json.loads(detail).get('error', detail)
            except ValueError:
                detail = detail[:300]
            raise BackendError(exc.code, detail) from None

    def heartbeat(self, node):
        return self._post('heartbeats/', json.dumps({'node': node}).encode(), 'application/json')

    def upload(self, fields, filename, jpeg):
        boundary = uuid.uuid4().hex
        body = b''.join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields.items() if value is not None
        )
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
                 f'Content-Type: image/jpeg\r\n\r\n').encode() + jpeg + f'\r\n--{boundary}--\r\n'.encode()
        return self._post('captures/', body, f'multipart/form-data; boundary={boundary}')


# Bridge

class SeenImages:
    """Hashes of uploaded images, saved to disk so a restart never re-uploads the gallery."""

    def __init__(self, path):
        self.path = path
        try:
            self.hashes = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.hashes = []
        self._set = set(self.hashes)

    def __contains__(self, digest):
        return digest in self._set

    def add(self, digest):
        self.hashes = (self.hashes + [digest])[-SEEN_LIMIT:]
        self._set = set(self.hashes)
        self.path.write_text(json.dumps(self.hashes), encoding='utf-8')


def run(args):
    esp_url = args.esp.rstrip('/')
    backend = Backend(args.api, args.key)
    node = args.node
    seen = None
    esp_up = None          # last known reachability, to log changes only
    last_heartbeat = 0.0
    uploaded = 0

    log.info('Watching %s every %ss -> %s', esp_url, args.poll, backend.api_url)
    while True:
        try:
            device_id, entries = read_gallery(esp_url, args.timeout)
        except (URLError, OSError) as exc:
            if esp_up is not False:
                log.warning('Board not reachable at %s (%s). Is this computer connected to its WiFi?',
                            esp_url, getattr(exc, 'reason', exc))
            esp_up = False
            if args.once:
                return 1
            time.sleep(args.poll)
            continue

        if esp_up is not True:
            log.info('Board "%s" reachable, %d image(s) in its gallery.', device_id or '?', len(entries))
        esp_up = True

        if node is None:
            node = device_id or 'esp32cam-01'
            log.info('Using node code "%s" (from the board\'s Device ID; change with --node).', node)
        if seen is None:
            seen = SeenImages(Path(args.state_dir) / f'.seen-{node}.json')

        try:
            if time.monotonic() - last_heartbeat >= args.heartbeat:
                backend.heartbeat(node)
                last_heartbeat = time.monotonic()

            for entry in entries:
                try:
                    jpeg = http_get(f"{esp_url}/img?n={entry['seq']}", args.timeout)
                except HTTPError as exc:
                    if exc.code == 404:   # overwritten in the board's ring buffer meanwhile
                        continue
                    raise
                if not jpeg.startswith(b'\xff\xd8'):
                    log.warning('Image #%s is not a JPEG, skipped.', entry['seq'])
                    continue
                digest = hashlib.sha256(jpeg).hexdigest()
                if digest in seen:
                    continue

                width, height = jpeg_size(jpeg)
                reply = backend.upload({
                    'node': node,
                    'sequence': entry['seq'],
                    'captured_at': capture_time(entry['when']).isoformat(),
                    'checksum': digest,
                    'width': width,
                    'height': height,
                }, f"{node}_{entry['seq']:05d}.jpg", jpeg)
                seen.add(digest)
                uploaded += reply.get('created', False)
                log.info('Image #%s (%s, %d KB) -> capture %s%s', entry['seq'], entry['change'],
                         len(jpeg) // 1024, reply['id'], '' if reply.get('created') else ' (already there)')
        except BackendError as exc:
            if exc.status == 401:
                raise SystemExit('The backend rejected the API key. Set --key or DAMASCUS_EYE_API_KEY.')
            if 'Unknown node' in str(exc):
                raise SystemExit(f'Node "{node}" is not registered. Add it in the dashboard '
                                 f'(Nodes -> Add node, code "{node}") or pass --node.')
            log.error('Backend error: %s', exc)
        except (URLError, OSError) as exc:
            log.error('Transfer failed: %s (will retry)', getattr(exc, 'reason', exc))

        if args.once:
            log.info('Done: %d new image(s) sent.', uploaded)
            return 0
        time.sleep(args.poll)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Send ESP32-CAM gallery images to the Damascus Eye dashboard.')
    parser.add_argument('--esp', default='http://192.168.4.1', help='Board address (default http://192.168.4.1).')
    parser.add_argument('--node', help='Node code in the dashboard (default: the board\'s Device ID).')
    parser.add_argument('--api', default=os.environ.get('DAMASCUS_EYE_API', 'http://127.0.0.1:8000/api'),
                        help='Backend API base URL.')
    parser.add_argument('--key', default=os.environ.get('DAMASCUS_EYE_API_KEY', 'dev-api-key'),
                        help='API key (X-API-Key).')
    parser.add_argument('--poll', type=float, default=5, help='Seconds between checks (default 5).')
    parser.add_argument('--heartbeat', type=float, default=60, help='Seconds between heartbeats (default 60).')
    parser.add_argument('--timeout', type=float, default=10, help='HTTP timeout to the board in seconds.')
    parser.add_argument('--state-dir', default=str(Path(__file__).resolve().parent),
                        help='Where to remember already-sent images.')
    parser.add_argument('--once', action='store_true', help='Send what the board has now, then exit.')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)-7s %(message)s', datefmt='%H:%M:%S')
    try:
        return run(args)
    except KeyboardInterrupt:
        log.info('Stopped.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
