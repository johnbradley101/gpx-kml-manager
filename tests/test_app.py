"""Run with:  python -m pytest -q   (from the project folder)"""
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gpxkml, ops  # noqa: E402


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


# ---------------------------------------------------------------- parsing
def test_parse_gpx_flattens_segments_and_routes():
    doc, fmt = gpxkml.parse(make_gpx(20), "x.gpx")
    assert fmt == "gpx" and doc["name"] == "Test ride"
    assert [t["name"] for t in doc["tracks"]] == ["Main", "Planned"]
    assert len(doc["tracks"][0]["points"]) == 20
    assert doc["waypoints"][0]["name"] == "Start & cafe"


def test_parse_kml_all_geometry_types():
    doc, fmt = gpxkml.parse(KML, "trip.kml")
    assert fmt == "kml" and doc["name"] == "KML trip"
    assert len(doc["waypoints"]) == 1 and doc["waypoints"][0]["ele"] == 10
    assert [len(t["points"]) for t in doc["tracks"]] == [3, 2]
    assert doc["tracks"][1]["points"][0][3] == "2026-05-01T08:00:00Z"


def test_roundtrip_gpx_kml_gpx_keeps_data():
    doc, _ = gpxkml.parse(make_gpx(10), "x.gpx")
    kml = gpxkml.to_kml(doc).encode()
    doc2, fmt = gpxkml.parse(kml, "x.kml")
    assert fmt == "kml"
    assert len(doc2["tracks"][0]["points"]) == 10
    assert doc2["tracks"][0]["points"][3][3] == doc["tracks"][0]["points"][3][3]  # timestamps survive via gx:Track
    gpx2, _ = gpxkml.parse(gpxkml.to_gpx(doc2).encode(), "y.gpx")
    assert gpx2["tracks"][0]["points"][5][:3] == doc["tracks"][0]["points"][5][:3]


def test_special_characters_are_escaped():
    doc, _ = gpxkml.parse(make_gpx(4, name="A <b> & \"c\""), "x.gpx") if False else (None, None)
    doc = {"name": "A <b> & c", "description": "", "waypoints": [{"name": "x\x01<y>", "desc": "", "lat": 1, "lon": 2, "ele": None}],
           "tracks": [{"name": "t", "points": [[1, 2, None, None], [1.1, 2.1, None, None]]}]}
    for out, fmt in ((gpxkml.to_gpx(doc), "gpx"), (gpxkml.to_kml(doc), "kml")):
        back, _ = gpxkml.parse(out.encode(), "f." + fmt)
        assert back["name"] == "A <b> & c" and back["waypoints"][0]["name"] == "x<y>"


@pytest.mark.parametrize("bad", [b"not xml", b"<foo/>", b"<gpx></gpx>", b"PK\x03\x04garbage"])
def test_bad_input_rejected(bad):
    with pytest.raises(gpxkml.ParseError):
        gpxkml.parse(bad, "bad.gpx")


def test_entity_expansion_rejected():
    evil = b'<?xml version="1.0"?><!DOCTYPE gpx [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><gpx><metadata><name>&b;</name></metadata></gpx>'
    with pytest.raises(gpxkml.ParseError):
        gpxkml.parse(evil, "evil.gpx")


def test_stats_distance_reasonable():
    # 1 degree of latitude is about 111.2 km
    d = gpxkml.distance([[0, 0, None, None], [1, 0, None, None]])
    assert 111000 < d < 111400


# ---------------------------------------------------------------- operations
def fresh():
    doc, _ = gpxkml.parse(make_gpx(20), "x.gpx")
    return doc


def test_crop():
    d = fresh()
    ops.apply(d, {"op": "crop", "track": 0, "start": 5, "end": 14})
    assert len(d["tracks"][0]["points"]) == 10
    assert d["tracks"][0]["points"][0][2] == 100 + 5 * 2


def test_delete_section_split_and_join():
    d = fresh()
    ops.apply(d, {"op": "delete_section", "track": 0, "start": 5, "end": 9, "mode": "split"})
    assert [len(t["points"]) for t in d["tracks"][:2]] == [5, 10]
    assert d["tracks"][0]["name"] == "Main (1)" and d["tracks"][1]["name"] == "Main (2)"
    d = fresh()
    ops.apply(d, {"op": "delete_section", "track": 0, "start": 5, "end": 9, "mode": "join"})
    assert len(d["tracks"][0]["points"]) == 15 and d["tracks"][0]["name"] == "Main"


def test_delete_section_at_end_just_truncates_and_whole_track_errors():
    d = fresh()
    ops.apply(d, {"op": "delete_section", "track": 0, "start": 10, "end": 19})
    assert len(d["tracks"][0]["points"]) == 10 and d["tracks"][0]["name"] == "Main"
    with pytest.raises(ops.EditError):
        ops.apply(fresh(), {"op": "delete_section", "track": 0, "start": 0, "end": 19})


def test_split_shares_split_point():
    d = fresh()
    ops.apply(d, {"op": "split", "track": 0, "index": 7})
    a, b = d["tracks"][0]["points"], d["tracks"][1]["points"]
    assert len(a) == 8 and len(b) == 13 and a[-1] == b[0]
    with pytest.raises(ops.EditError):
        ops.apply(fresh(), {"op": "split", "track": 0, "index": 0})


def test_reverse_merge_rename_delete():
    d = fresh()
    first = d["tracks"][0]["points"][0][:2]
    ops.apply(d, {"op": "reverse", "track": 0})
    assert d["tracks"][0]["points"][-1][:2] == first and d["tracks"][0]["points"][0][3] is None
    ops.apply(d, {"op": "merge", "tracks": [0, 1]})
    assert len(d["tracks"]) == 1 and len(d["tracks"][0]["points"]) == 22
    ops.apply(d, {"op": "rename_track", "track": 0, "name": "  New  "})
    assert d["tracks"][0]["name"] == "New"
    ops.apply(d, {"op": "delete_track", "track": 0})
    assert d["tracks"] == []


def test_waypoint_ops_and_validation():
    d = fresh()
    ops.apply(d, {"op": "add_waypoint", "lat": 51.2, "lon": -0.4, "name": "Pub"})
    assert d["waypoints"][-1]["name"] == "Pub"
    ops.apply(d, {"op": "update_waypoint", "waypoint": 0, "name": "Renamed"})
    ops.apply(d, {"op": "delete_waypoint", "waypoint": 1})
    assert [w["name"] for w in d["waypoints"]] == ["Renamed"]
    for bad in (
        {"op": "crop", "track": 0, "start": 9, "end": 3},
        {"op": "crop", "track": 0, "start": 0, "end": 99},
        {"op": "crop", "track": "0", "start": 0, "end": 5},
        {"op": "crop", "track": 9, "start": 0, "end": 5},
        {"op": "add_waypoint", "lat": 99, "lon": 0},
        {"op": "rename_track", "track": 0, "name": "  "},
        {"op": "nope"},
    ):
        with pytest.raises(ops.EditError):
            ops.apply(fresh(), bad)


# ---------------------------------------------------------------- server
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

    def json(self, method, path, obj=None):
        body = json.dumps(obj).encode() if obj is not None else None
        st, data, _ = self.call(method, path, body, {"Content-Type": "application/json"} if obj is not None else None)
        return st, json.loads(data)

    def upload(self, path, fields):
        body, ctype = multipart(fields)
        return self.call("POST", path, body, {"Content-Type": ctype})


def start_server(tmp_path, extra_env=None):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, DATA_DIR=str(tmp_path), RESCAN_SECONDS="5", **(extra_env or {}))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            return proc, base
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("server did not start")


@pytest.fixture()
def server(tmp_path):
    proc, base = start_server(tmp_path)
    yield Client(base), tmp_path
    proc.terminate()
    proc.wait(5)


def test_server_full_workflow(server):
    c, data_dir = server
    assert c.call("GET", "/")[0] == 200

    st, data, _ = c.upload("/api/files", [("files", "ride.gpx", make_gpx(20)), ("files", "trip.kml", KML), ("files", "bad.gpx", b"junk")])
    res = json.loads(data)["results"]
    assert [r["ok"] for r in res] == [True, True, False] and "error" in res[2]
    gpx_id, kml_id = res[0]["id"], res[1]["id"]

    st, lst = c.json("GET", "/api/files")
    assert {f["source_format"] for f in lst["files"]} == {"gpx", "kml"}
    assert any((data_dir / "library").glob("*.gpx")) and any((data_dir / "originals").glob("*.kml"))

    st, d = c.json("GET", f"/api/files/{gpx_id}")
    assert st == 200 and len(d["tracks"][0]["points"]) == 20 and not d["can_undo"]

    # crop, then undo
    st, d = c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "crop", "track": 0, "start": 2, "end": 11})
    assert st == 200 and len(d["tracks"][0]["points"]) == 10 and d["can_undo"]
    st, d = c.json("POST", f"/api/files/{gpx_id}/undo")
    assert st == 200 and len(d["tracks"][0]["points"]) == 20 and not d["can_undo"]
    assert c.json("POST", f"/api/files/{gpx_id}/undo")[0] == 400

    # split / delete section / rename
    st, d = c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "split", "track": 0, "index": 9})
    assert [t["name"] for t in d["tracks"]][:2] == ["Main (1)", "Main (2)"]
    st, d = c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "rename_file", "name": "Renamed ride"})
    assert d["name"] == "Renamed ride"
    assert c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "crop", "track": 0, "start": 5, "end": 1})[0] == 400

    # export both formats
    st, body, h = c.call("GET", f"/api/files/{gpx_id}/export?format=kml")
    assert st == 200 and b"<kml" in body and "attachment" in h["Content-Disposition"]
    doc, fmt = gpxkml.parse(body, "x.kml")
    assert fmt == "kml" and len(doc["tracks"]) >= 2
    st, body, _ = c.call("GET", f"/api/files/{kml_id}/export?format=gpx")
    assert st == 200 and gpxkml.parse(body, "x.gpx")[1] == "gpx"
    assert c.call("GET", f"/api/files/{kml_id}/export?format=pdf")[0] == 400
    st, body, _ = c.call("GET", "/api/export-all?format=gpx")
    assert st == 200 and body[:2] == b"PK"

    # stateless conversion stores nothing
    before = len(c.json("GET", "/api/files")[1]["files"])
    st, body, _ = c.upload("/api/convert?to=gpx", [("file", "trip.kml", KML)])
    assert st == 200 and b"<gpx" in body
    assert len(c.json("GET", "/api/files")[1]["files"]) == before

    # errors
    assert c.call("GET", "/api/files/../../etc")[0] == 404  # no route matches: plain-text 404
    assert c.json("GET", "/api/files/aaaaaaaaaaaa")[0] == 404
    assert c.call("POST", f"/api/files/{gpx_id}/edit", b"nope", {"Content-Type": "application/json"})[0] == 400
    assert c.call("POST", f"/api/files/{gpx_id}/edit", b"{}", {"Content-Type": "application/json", "Origin": "http://evil.example"})[0] == 403

    # delete
    assert c.json("DELETE", f"/api/files/{kml_id}")[0] == 200
    assert c.json("GET", f"/api/files/{kml_id}")[0] == 404
    assert not list((data_dir / "originals").glob(f"{kml_id}.*"))


def test_inbox_and_library_dropins_are_picked_up(server):
    c, data_dir = server
    (data_dir / "inbox" / "dropped.kml").write_bytes(KML)
    (data_dir / "inbox" / "broken.gpx").write_bytes(b"junk")
    (data_dir / "library" / "manual.gpx").write_bytes(make_gpx(6, name="Manual"))
    time.sleep(3.5)  # inbox files must settle before import
    st, r = c.json("POST", "/api/rescan")
    assert st == 200
    names = {f["name"] for f in c.json("GET", "/api/files")[1]["files"]}
    assert {"KML trip", "Manual"} <= names
    assert not (data_dir / "inbox" / "dropped.kml").exists()
    assert (data_dir / "inbox" / "rejected" / "broken.gpx").exists()

    # deleting a file in File Station removes it from the index
    for p in (data_dir / "library").glob("manual*.gpx"):
        p.unlink()
    c.json("POST", "/api/rescan")
    assert "Manual" not in {f["name"] for f in c.json("GET", "/api/files")[1]["files"]}


def test_basic_auth(tmp_path):
    proc, base = start_server(tmp_path, {"APP_USER": "me", "APP_PASSWORD": "s3cret"})
    try:
        anon = Client(base)
        assert anon.call("GET", "/api/files")[0] == 401
        assert anon.call("GET", "/api/health")[0] == 200
        assert Client(base, "me:wrong").call("GET", "/api/files")[0] == 401
        assert Client(base, "me:s3cret").call("GET", "/api/files")[0] == 200
    finally:
        proc.terminate()
        proc.wait(5)
