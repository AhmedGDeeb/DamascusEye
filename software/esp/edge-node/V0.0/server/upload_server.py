"""
Minimal receiver for ESP32-CAM uploads -> SQLite database.
Python 3 standard library only (no pip install needed).

Run:   python upload_server.py
Then set in menuconfig:  Upload URL = http://<PC-IP>:8000/upload
"""
import sqlite3
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = "images.db"
MAX_SIZE = 2 * 1024 * 1024  # 2 MB

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT,
    captured_at TEXT,
    received_at TEXT,
    size_bytes  INTEGER,
    jpeg        BLOB
)""")
db.commit()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/upload":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > MAX_SIZE:
            self.send_error(400, "bad size")
            return
        data = self.rfile.read(length)
        if not data.startswith(b"\xff\xd8"):
            self.send_error(400, "not a JPEG")
            return

        device = self.headers.get("X-Device-Id", "unknown")
        ts = int(self.headers.get("X-Timestamp", "0") or 0)
        captured = (datetime.datetime.fromtimestamp(ts).isoformat()
                    if ts > 1_600_000_000 else None)   # None if clock not synced
        received = datetime.datetime.now().isoformat()

        db.execute("INSERT INTO images (device_id, captured_at, received_at, size_bytes, jpeg) "
                   "VALUES (?,?,?,?,?)", (device, captured, received, len(data), data))
        db.commit()
        print(f"[{received}] {device}: {len(data)} bytes (captured {captured})")

        self.send_response(201)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        # /latest.jpg -> most recent image (quick check from a browser)
        if self.path == "/latest.jpg":
            row = db.execute("SELECT jpeg FROM images ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                self.send_error(404, "no images yet")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(row[0])
        else:
            self.send_error(404)


if __name__ == "__main__":
    print("Listening on 0.0.0.0:8000  (POST /upload, GET /latest.jpg)")
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
