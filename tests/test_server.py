"""The HTTP API, tested against the real app running under uvicorn."""
import json
import os
import time

import pytest

from app import gpxkml
from helpers import KML, Client, make_gpx, make_kmz


@pytest.fixture()
def uploaded(server):
    """A server holding one GPX and one KML file: (client, data_dir, gpx_id, kml_id)."""
    c, data_dir = server
    st, data, _ = c.upload("/api/files", [("files", "ride.gpx", make_gpx(20)), ("files", "trip.kml", KML)])
    res = json.loads(data)["results"]
    assert st == 200 and all(r["ok"] for r in res)
    return c, data_dir, res[0]["id"], res[1]["id"]


def library_file(data_dir, fid):
    (path,) = (data_dir / "library").glob(f"*__{fid}.gpx")
    return path


# ---------------------------------------------------------------- basics and upload
def test_index_health_and_security_headers(server):
    c, _ = server
    st, body, h = c.call("GET", "/")
    assert st == 200 and b"GPX &amp; KML Manager" in body
    assert h["X-Content-Type-Options"] == "nosniff"
    assert c.json("GET", "/api/health") == (200, {"status": "ok"})


def test_upload_reports_each_file_separately(server):
    c, data_dir = server
    st, data, _ = c.upload("/api/files", [("files", "ride.gpx", make_gpx(20)), ("files", "trip.kml", KML), ("files", "bad.gpx", b"junk")])
    res = json.loads(data)["results"]
    assert [r["ok"] for r in res] == [True, True, False] and "error" in res[2]
    st, lst = c.json("GET", "/api/files")
    assert {f["source_format"] for f in lst["files"]} == {"gpx", "kml"}
    assert any((data_dir / "library").glob("*.gpx")) and any((data_dir / "originals").glob("*.kml"))


def test_upload_needs_files_in_the_files_field(server):
    c, _ = server
    assert c.upload("/api/files", [("file", "ride.gpx", make_gpx(4))])[0] == 400
    assert c.call("POST", "/api/files", b"", {"Content-Type": "multipart/form-data; boundary=x"})[0] == 400


def test_upload_size_limit(make_server):
    c, _ = make_server({"MAX_UPLOAD_MB": "1"})
    too_big = b"x" * (1024 * 1024 + 10)
    st, data, _ = c.upload("/api/files", [("files", "big.gpx", too_big), ("files", "ok.gpx", make_gpx(4))])
    res = json.loads(data)["results"]
    assert [r["ok"] for r in res] == [False, True] and "1 MB" in res[0]["error"]
    assert c.upload("/api/convert?to=kml", [("file", "big.gpx", too_big)])[0] == 413


# ---------------------------------------------------------------- reading and editing
def test_get_file_details(uploaded):
    c, _, gpx_id, _ = uploaded
    st, d = c.json("GET", f"/api/files/{gpx_id}")
    assert st == 200 and len(d["tracks"][0]["points"]) == 20 and not d["can_undo"]
    assert d["stats"]["tracks"] == 2 and d["tracks"][0]["stats"]["points"] == 20


def test_edit_and_undo(uploaded):
    c, _, gpx_id, _ = uploaded
    st, d = c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "crop", "track": 0, "start": 2, "end": 11})
    assert st == 200 and len(d["tracks"][0]["points"]) == 10 and d["can_undo"]
    st, d = c.json("POST", f"/api/files/{gpx_id}/undo")
    assert st == 200 and len(d["tracks"][0]["points"]) == 20 and not d["can_undo"]
    assert c.json("POST", f"/api/files/{gpx_id}/undo")[0] == 400

    st, d = c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "split", "track": 0, "index": 9})
    assert [t["name"] for t in d["tracks"]][:2] == ["Main (1)", "Main (2)"]
    st, d = c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "rename_file", "name": "Renamed ride"})
    assert d["name"] == "Renamed ride"
    assert any(f["name"] == "Renamed ride" for f in c.json("GET", "/api/files")[1]["files"])


def test_rejected_edit_leaves_the_stored_file_untouched(uploaded):
    c, data_dir, gpx_id, _ = uploaded
    path = library_file(data_dir, gpx_id)
    before = path.read_bytes()
    st, body = c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "crop", "track": 0, "start": 5, "end": 1})
    assert st == 400 and "detail" in body
    assert path.read_bytes() == before and not c.json("GET", f"/api/files/{gpx_id}")[1]["can_undo"]


def test_delete(uploaded):
    c, data_dir, gpx_id, kml_id = uploaded
    assert c.json("DELETE", f"/api/files/{kml_id}")[0] == 200
    assert c.json("GET", f"/api/files/{kml_id}")[0] == 404
    assert not list((data_dir / "originals").glob(f"{kml_id}.*"))
    assert c.json("GET", f"/api/files/{gpx_id}")[0] == 200  # the other file is unaffected


# ---------------------------------------------------------------- export and convert
def test_export_single_files_and_zip(uploaded):
    c, _, gpx_id, kml_id = uploaded
    st, body, h = c.call("GET", f"/api/files/{gpx_id}/export?format=kml")
    assert st == 200 and b"<kml" in body and "attachment" in h["Content-Disposition"]
    doc, fmt = gpxkml.parse(body, "x.kml")
    assert fmt == "kml" and len(doc["tracks"]) >= 2
    st, body, _ = c.call("GET", f"/api/files/{kml_id}/export?format=gpx")
    assert st == 200 and gpxkml.parse(body, "x.gpx")[1] == "gpx"
    assert c.call("GET", f"/api/files/{kml_id}/export?format=pdf")[0] == 400
    st, body, _ = c.call("GET", "/api/export-all?format=gpx")
    assert st == 200 and body[:2] == b"PK"
    assert c.call("GET", "/api/export-all?format=pdf")[0] == 400


def test_download_file_names_are_safe_ascii(uploaded):
    c, _, gpx_id, _ = uploaded
    c.json("POST", f"/api/files/{gpx_id}/edit", {"op": "rename_file", "name": 'Café "ride" ☕ ../x'})
    st, _, h = c.call("GET", f"/api/files/{gpx_id}/export?format=gpx")
    disposition = h["Content-Disposition"]
    name = disposition.split('filename="')[1].split('"')[0]  # a stray quote in the title must not end this early
    assert st == 200 and disposition.isascii() and name.endswith(".gpx")
    assert "/" not in name and "\\" not in name and '"' not in name


def test_convert_is_stateless(uploaded):
    c, _, _, _ = uploaded
    before = len(c.json("GET", "/api/files")[1]["files"])
    st, body, _ = c.upload("/api/convert?to=gpx", [("file", "trip.kml", KML)])
    assert st == 200 and b"<gpx" in body
    st, body, _ = c.upload("/api/convert?to=gpx", [("file", "trip.kmz", make_kmz({"doc.kml": KML}))])
    assert st == 200 and b"<gpx" in body
    assert len(c.json("GET", "/api/files")[1]["files"]) == before


def test_convert_validates_its_input(server):
    c, _ = server
    assert c.upload("/api/convert?to=pdf", [("file", "a.gpx", make_gpx(4))])[0] == 400
    assert c.upload("/api/convert", [("file", "a.gpx", make_gpx(4))])[0] == 400
    assert c.upload("/api/convert?to=kml", [("file", "a.gpx", make_gpx(4)), ("file", "b.gpx", make_gpx(4))])[0] == 400
    assert c.upload("/api/convert?to=kml", [("other", "a.gpx", make_gpx(4))])[0] == 400
    assert c.upload("/api/convert?to=kml", [("file", "a.gpx", b"junk")])[0] == 400


# ---------------------------------------------------------------- error handling and security
@pytest.mark.parametrize("path", ["/api/files/..%2f..%2fetc", "/api/files/AAAAAAAAAAAA", "/api/files/aaaaaaaaaaaaa", "/api/files/aaaaaaaaaaaa"])
def test_unknown_or_malformed_ids_are_404(uploaded, path):
    c, *_ = uploaded
    assert c.call("GET", path)[0] == 404
    assert c.call("DELETE", path)[0] == 404
    assert c.call("GET", path + "/export")[0] == 404


def test_plain_404_for_unknown_routes(server):
    c, _ = server
    assert c.call("GET", "/api/files/../../etc")[0] == 404
    assert c.call("GET", "/api/nope")[0] == 404


def test_edit_endpoint_validates_the_request_body(uploaded):
    c, _, gpx_id, _ = uploaded
    url = f"/api/files/{gpx_id}/edit"
    json_h = {"Content-Type": "application/json"}
    assert c.call("POST", url, b"nope", json_h)[0] == 400  # not JSON
    assert c.call("POST", url, b"[]", json_h)[0] == 400  # JSON, but not an object
    assert c.json("POST", url, {"op": "teleport"})[0] == 400
    assert c.json("POST", "/api/files/aaaaaaaaaaaa/edit", {"op": "rename_file", "name": "x"})[0] == 404


def test_cross_origin_writes_are_refused(uploaded):
    c, data_dir, gpx_id, kml_id = uploaded
    evil = {"Origin": "http://evil.example"}
    body = json.dumps({"op": "reverse", "track": 0}).encode()
    assert c.call("POST", f"/api/files/{gpx_id}/edit", body, {"Content-Type": "application/json", **evil})[0] == 403
    assert c.call("DELETE", f"/api/files/{kml_id}", headers=evil)[0] == 403
    assert c.call("POST", "/api/rescan", headers=evil)[0] == 403
    assert c.json("GET", f"/api/files/{kml_id}")[0] == 200  # nothing was deleted
    assert not c.json("GET", f"/api/files/{gpx_id}")[1]["can_undo"]  # nothing was edited
    # Reads are not restricted, and the app's own origin (or no Origin at all) is fine.
    assert c.call("GET", "/api/files", headers=evil)[0] == 200
    assert c.call("POST", "/api/rescan", headers={"Origin": c.base})[0] == 200
    assert c.call("POST", "/api/rescan")[0] == 200


# ---------------------------------------------------------------- authentication
def test_basic_auth_protects_everything_except_health(make_server):
    c, _ = make_server({"APP_USER": "me", "APP_PASSWORD": "pä:ss"})  # password with a colon and a non-ASCII letter
    anon = Client(c.base)
    assert anon.call("GET", "/api/health")[0] == 200
    for path in ("/api/files", "/", "/static/app.js"):
        st, _, h = anon.call("GET", path)
        assert st == 401 and "Basic" in h["WWW-Authenticate"]
    assert anon.call("POST", "/api/rescan")[0] == 401
    assert Client(c.base, "me:wrong").call("GET", "/api/files")[0] == 401
    assert Client(c.base, "you:pä:ss").call("GET", "/api/files")[0] == 401
    assert Client(c.base, "me:pä:ss").call("GET", "/api/files")[0] == 200
    for header in ("Basic !!!not-base64!!!", "Bearer abc", "Basic", "basic bWU6cMOkOnNz extra"):
        assert anon.call("GET", "/api/files", headers={"Authorization": header})[0] == 401


def test_auth_is_only_enabled_when_user_and_password_are_both_set(make_server):
    c, _ = make_server({"APP_USER": "me"})
    assert c.call("GET", "/api/files")[0] == 200


# ---------------------------------------------------------------- folders watched by the server
def test_inbox_and_library_dropins_are_picked_up(server):
    c, data_dir = server
    (data_dir / "inbox" / "dropped.kml").write_bytes(KML)
    (data_dir / "inbox" / "broken.gpx").write_bytes(b"junk")
    (data_dir / "library" / "manual.gpx").write_bytes(make_gpx(6, name="Manual"))
    for p in (data_dir / "inbox").iterdir():
        t = time.time() - 10  # pretend the copy finished a while ago instead of sleeping
        os.utime(p, (t, t))
    st, r = c.json("POST", "/api/rescan")
    assert st == 200 and r["added"] == 2 and r["rejected"] == 1
    names = {f["name"] for f in c.json("GET", "/api/files")[1]["files"]}
    assert {"KML trip", "Manual"} <= names
    assert not (data_dir / "inbox" / "dropped.kml").exists()
    assert (data_dir / "inbox" / "rejected" / "broken.gpx").exists()

    # deleting a file in File Station removes it from the index
    for p in (data_dir / "library").glob("manual*.gpx"):
        p.unlink()
    assert c.json("POST", "/api/rescan")[1]["removed"] == 1
    assert "Manual" not in {f["name"] for f in c.json("GET", "/api/files")[1]["files"]}
