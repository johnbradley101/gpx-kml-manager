"""The file library, tested in-process against a temporary folder (no server, no sleeping)."""
import io
import logging
import os
import time
import zipfile

import pytest

from app import gpxkml, ops
from helpers import KML, make_gpx, make_kmz


def add(store, data=None, name="ride.gpx"):
    return store.import_bytes(data or make_gpx(20), name)["id"]


def backdate(path, seconds=10):
    t = time.time() - seconds
    os.utime(path, (t, t))


# ---------------------------------------------------------------- import
def test_import_keeps_the_original_and_stores_a_gpx_working_copy(store):
    r = store.import_bytes(KML, "trip.kml")
    assert r["source_format"] == "kml" and r["name"] == "KML trip"
    assert (store.ORIG / f"{r['id']}.kml").read_bytes() == KML
    row, doc = store.load(r["id"])
    assert row["filename"].endswith(".gpx") and (store.LIB / row["filename"]).exists()
    assert len(doc["tracks"]) == 2


def test_kmz_import_keeps_the_kmz_as_the_original(store):
    r = store.import_bytes(make_kmz({"doc.kml": KML}), "trip.kmz")
    assert r["source_format"] == "kml" and (store.ORIG / f"{r['id']}.kmz").exists()


@pytest.mark.parametrize("filename", ["ride.txt", "ride", ""])
def test_unknown_extension_falls_back_to_the_detected_format(store, filename):
    fid = store.import_bytes(make_gpx(4), filename)["id"]
    assert (store.ORIG / f"{fid}.gpx").exists()


def test_unparseable_upload_stores_nothing(store):
    with pytest.raises(gpxkml.ParseError):
        store.import_bytes(b"junk", "x.gpx")
    assert store.list_files() == [] and not list(store.ORIG.iterdir()) and not list(store.LIB.iterdir())


@pytest.mark.parametrize("bad", ["../etc", "..", "", "A" * 12, "a" * 13, "a" * 11, "../../etc/passwd"])
def test_ids_that_are_not_twelve_hex_characters_are_not_found(store, bad):
    with pytest.raises(store.NotFound):
        store.load(bad)


def test_slug_is_filesystem_safe():
    from app import storage

    assert storage._slug("../../etc/passwd") == "etc-passwd"
    assert storage._slug("") == "file" and storage._slug("!!!") == "file"
    assert len(storage._slug("x" * 100)) == 60
    # Accented characters are replaced rather than transliterated (safe, but "Café" becomes "Caf").
    assert storage._slug("Café ride") == "Caf-ride"


# ---------------------------------------------------------------- editing and undo
def test_failed_edit_changes_nothing(store):
    fid = add(store)
    path = store.LIB / store.load(fid)[0]["filename"]
    before = path.read_bytes()
    with pytest.raises(ops.EditError):
        store.edit(fid, {"op": "crop", "track": 0, "start": 9, "end": 3})
    assert path.read_bytes() == before and not store.can_undo(fid)


def test_undo_history_is_capped(store):
    fid = add(store)
    for i in range(store.HIST_KEEP + 5):
        store.edit(fid, {"op": "rename_file", "name": f"v{i}"})
    assert len(list((store.HIST / fid).glob("*.gpx"))) == store.HIST_KEEP
    for _ in range(store.HIST_KEEP):
        store.undo(fid)
    assert not store.can_undo(fid)
    with pytest.raises(ops.EditError):
        store.undo(fid)
    # The five oldest snapshots were pruned, so undo stops at v4 instead of reaching the original name.
    assert store.load(fid)[1]["name"] == "v4"


def test_edit_updates_the_index(store):
    fid = add(store)
    before = store.list_files()[0]
    store.edit(fid, {"op": "crop", "track": 0, "start": 0, "end": 9})
    after = store.list_files()[0]
    assert after["distance_m"] < before["distance_m"]
    store.undo(fid)
    assert store.list_files()[0]["distance_m"] == before["distance_m"]


def test_delete_removes_library_original_and_history(store):
    fid = add(store)
    store.edit(fid, {"op": "rename_file", "name": "renamed"})
    path = store.LIB / store.load(fid)[0]["filename"]
    store.delete(fid)
    assert not path.exists() and not list(store.ORIG.glob(f"{fid}.*")) and not (store.HIST / fid).exists()
    with pytest.raises(store.NotFound):
        store.load(fid)


# ---------------------------------------------------------------- export
def test_export_all_gives_duplicate_titles_unique_file_names(store):
    for _ in range(3):
        add(store)  # the same title three times
    add(store, KML, "trip.kml")
    z = zipfile.ZipFile(io.BytesIO(store.export_all("kml")))
    assert sorted(z.namelist()) == ["KML-trip.kml", "Test-ride-2.kml", "Test-ride-3.kml", "Test-ride.kml"]
    for n in z.namelist():
        gpxkml.parse(z.read(n), n)


def test_export_all_skips_files_that_vanished_from_disk(store):
    gone = add(store)
    add(store, KML, "trip.kml")
    (store.LIB / store.load(gone)[0]["filename"]).unlink()
    assert zipfile.ZipFile(io.BytesIO(store.export_all("gpx"))).namelist() == ["KML-trip.gpx"]


def test_export_rejects_unknown_formats(store):
    with pytest.raises(ops.EditError):
        store.export(add(store), "pdf")


# ---------------------------------------------------------------- rescan: files dropped into the folders
def test_rescan_forgets_vanished_files_and_their_history(store):
    fid = add(store)
    store.edit(fid, {"op": "reverse", "track": 0})
    (store.LIB / store.load(fid)[0]["filename"]).unlink()
    assert store.rescan()["removed"] == 1
    assert store.list_files() == [] and not (store.HIST / fid).exists() and not list(store.ORIG.glob(f"{fid}.*"))


def test_rescan_adopts_gpx_files_dropped_into_the_library(store):
    (store.LIB / "manual.gpx").write_bytes(make_gpx(6, name="Manual"))
    assert store.rescan()["added"] == 1
    assert [f["name"] for f in store.list_files()] == ["Manual"]
    assert store.rescan()["added"] == 0  # already known


def test_unreadable_library_file_is_reported_once_and_retried_after_it_changes(store, caplog):
    p = store.LIB / "junk.gpx"
    p.write_bytes(b"junk")
    with caplog.at_level(logging.WARNING, logger="gpxapp"):
        store.rescan()
        store.rescan()
    assert len([r for r in caplog.records if "unreadable" in r.getMessage()]) == 1
    p.write_bytes(make_gpx(6, name="Fixed"))
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))
    assert store.rescan()["added"] == 1


def test_inbox_waits_for_files_to_settle_and_ignores_other_files(store):
    fresh = store.INBOX / "new.kml"
    hidden = store.INBOX / ".hidden.kml"
    other = store.INBOX / "notes.txt"
    for p, data in ((fresh, KML), (hidden, KML), (other, b"hello")):
        p.write_bytes(data)
    assert store.rescan()["added"] == 0 and fresh.exists()  # still being copied, as far as we can tell
    for p in (fresh, hidden, other):
        backdate(p)
    assert store.rescan()["added"] == 1
    assert not fresh.exists() and hidden.exists() and other.exists()


def test_unparseable_inbox_files_are_moved_to_rejected(store):
    p = store.INBOX / "broken.gpx"
    p.write_bytes(b"junk")
    backdate(p)
    assert store.rescan()["rejected"] == 1
    assert not p.exists() and (store.INBOX / "rejected" / "broken.gpx").exists() and store.list_files() == []
