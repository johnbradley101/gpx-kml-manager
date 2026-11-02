"""Shared helpers for the backend tests (imported as `helpers`; pytest puts tests/ on sys.path)."""
import base64
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def make_gpx(n=20, name="Test ride", with_time=True):
    pts = []
    for i in range(n):
        t = f"<time>2026-05-01T08:{i // 60:02d}:{i % 60:02d}Z</time>" if with_time else ""
        pts.append(f'<trkpt lat="{51.0 + i * 0.001:.6f}" lon="{-0.5 + i * 0.001:.6f}"><ele>{100 + i * 2}</ele>{t}</trkpt>')
    return (
        '<?xml version="1.0"?><gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">'
        f"<metadata><name>{name}</name></metadata>"
        '<wpt lat="51.0" lon="-0.5"><name>Start &amp; cafe</name><desc>Tea</desc></wpt>'
        f"<trk><name>Main</name><trkseg>{''.join(pts[: n // 2])}</trkseg><trkseg>{''.join(pts[n // 2 :])}</trkseg></trk>"
        '<rte><name>Planned</name><rtept lat="51.5" lon="-0.1"/><rtept lat="51.6" lon="-0.2"/></rte>'
        "</gpx>"
    ).encode()


KML = b"""<?xml version="1.0"?>
<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
<Document><name>KML trip</name>
<Placemark><name>Pin</name><description>A pin</description><Point><coordinates>-0.1,51.5,10</coordinates></Point></Placemark>
<Placemark><name>Line</name><LineString><coordinates>-0.1,51.5,10 -0.2,51.6,20 -0.3,51.7,30</coordinates></LineString></Placemark>
<Placemark><name>Timed</name><gx:Track>
<when>2026-05-01T08:00:00Z</when><when>2026-05-01T08:01:00Z</when>
<gx:coord>-0.1 51.5 10</gx:coord><gx:coord>-0.2 51.6 20</gx:coord></gx:Track></Placemark>
</Document></kml>"""


def make_kmz(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def multipart(fields):
    boundary = uuid.uuid4().hex
    body = b""
    for field, filename, data in fields:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n" + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, base, auth=None):
        self.base, self.auth = base, auth

    def call(self, method, path, body=None, headers=None, auth=True):
        h = dict(headers or {})
        if auth and self.auth:
            h["Authorization"] = "Basic " + base64.b64encode(self.auth.encode()).decode()
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def json(self, method, path, obj=None, headers=None):
        body = json.dumps(obj).encode() if obj is not None else None
        h = {"Content-Type": "application/json"} if obj is not None else {}
        h.update(headers or {})
        st, data, _ = self.call(method, path, body, h)
        return st, json.loads(data)

    def upload(self, path, fields, headers=None):
        body, ctype = multipart(fields)
        return self.call("POST", path, body, {"Content-Type": ctype, **(headers or {})})


def start_server(tmp_path, extra_env=None):
    """Run the real app under uvicorn. Data lives in tmp_path/data, the server log in tmp_path/server.log."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, DATA_DIR=str(data), RESCAN_SECONDS="5", **(extra_env or {}))
    log = open(tmp_path / "server.log", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    proc.log_file = log
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            return proc, base
        except Exception:
            time.sleep(0.1)
    stop_server(proc)
    raise RuntimeError("server did not start:\n" + (tmp_path / "server.log").read_text(errors="replace"))


def stop_server(proc):
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)
    proc.log_file.close()
