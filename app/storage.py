"""File-based library with a small SQLite index.

Layout under DATA_DIR:
    library/    one GPX file per item  (<slug>__<id>.gpx)  - the working copy
    originals/  the file exactly as it was uploaded (<id>.<ext>)
    history/    <id>/<timestamp>.gpx snapshots used by "undo"
    inbox/      drop .gpx/.kml/.kmz files here (e.g. via File Station); they are imported automatically
    index.db    SQLite index (rebuildable from library/)

Everything is stored as GPX internally; KML/KMZ is converted on import and
generated on export.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import gpxkml, ops

log = logging.getLogger("gpxapp")

ID_RE = re.compile(r"^[0-9a-f]{12}$")
HIST_KEEP = 20
INBOX_SETTLE_SECONDS = 3
IMPORT_EXTS = (".gpx", ".kml", ".kmz")

DATA: Path
LIB: Path
ORIG: Path
HIST: Path
INBOX: Path
DBFILE: Path
_lock = threading.RLock()
_bad_files: set[tuple[str, float]] = set()


class NotFound(LookupError):
    pass


def configure(data_dir: str | os.PathLike | None = None) -> None:
    global DATA, LIB, ORIG, HIST, INBOX, DBFILE
    DATA = Path(data_dir or os.environ.get("DATA_DIR", "/data"))
    LIB, ORIG, HIST, INBOX = (DATA / n for n in ("library", "originals", "history", "inbox"))
    DBFILE = DATA / "index.db"
    _bad_files.clear()


def init(data_dir=None) -> None:
    configure(data_dir)
    for d in (LIB, ORIG, HIST, INBOX):
        d.mkdir(parents=True, exist_ok=True)
    with _db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS files(
                 id TEXT PRIMARY KEY, name TEXT NOT NULL, filename TEXT NOT NULL UNIQUE,
                 source_format TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
                 n_tracks INTEGER, n_waypoints INTEGER, distance_m REAL, start_time TEXT)"""
        )
    rescan()


# --------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DBFILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")[:60] or "file"


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _check_id(fid: str) -> str:
    if not isinstance(fid, str) or not ID_RE.match(fid):
        raise NotFound("No such file")
    return fid


def _insert(con, fid: str, doc: dict, filename: str, fmt: str) -> None:
    st = gpxkml.doc_stats(doc)
    now = _now()
    con.execute(
        "INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
        (fid, doc["name"], filename, fmt, now, now, st["tracks"], st["waypoints"], st["distance_m"], st["start_time"]),
    )


def _update_row(con, fid: str, doc: dict) -> None:
    st = gpxkml.doc_stats(doc)
    con.execute(
        "UPDATE files SET name=?, updated=?, n_tracks=?, n_waypoints=?, distance_m=?, start_time=? WHERE id=?",
        (doc["name"], _now(), st["tracks"], st["waypoints"], st["distance_m"], st["start_time"], fid),
    )


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------
def import_bytes(data: bytes, filename: str) -> dict:
    """Import an uploaded GPX/KML/KMZ. Raises gpxkml.ParseError for unusable files."""
    doc, fmt = gpxkml.parse(data, filename)
    with _lock:
        fid = uuid.uuid4().hex[:12]
        ext = Path(filename).suffix.lower()
        if ext not in IMPORT_EXTS:
            ext = "." + fmt
        (ORIG / f"{fid}{ext}").write_bytes(data)
        fname = f"{_slug(doc['name'])}__{fid}.gpx"
        _atomic_write(LIB / fname, gpxkml.to_gpx(doc))
        with _db() as con:
            _insert(con, fid, doc, fname, fmt)
    return {"id": fid, "name": doc["name"], "source_format": fmt}


def _forget(fid: str) -> None:
    shutil.rmtree(HIST / fid, ignore_errors=True)
    for p in ORIG.glob(f"{fid}.*"):
        p.unlink(missing_ok=True)


def rescan() -> dict:
    """Sync the index with the folders: drop vanished files, adopt dropped-in files, import the inbox."""
    added = removed = rejected = 0
    with _lock:
        with _db() as con:
            rows = con.execute("SELECT id, filename FROM files").fetchall()
            for r in rows:
                if not (LIB / r["filename"]).exists():
                    con.execute("DELETE FROM files WHERE id=?", (r["id"],))
                    _forget(r["id"])
                    removed += 1
            known = {r[0] for r in con.execute("SELECT filename FROM files")}

            for p in sorted(LIB.glob("*.gpx")):
                if p.name in known:
                    continue
                key = (p.name, p.stat().st_mtime)
                if key in _bad_files:
                    continue
                try:
                    doc, _ = gpxkml.parse(p.read_bytes(), p.name)
                except gpxkml.ParseError as exc:
                    log.warning("Ignoring unreadable file in library: %s (%s)", p.name, exc)
                    _bad_files.add(key)
                    continue
                _insert(con, uuid.uuid4().hex[:12], doc, p.name, "gpx")
                added += 1

        now = time.time()
        for p in sorted(INBOX.iterdir()):
            if not p.is_file() or p.name.startswith(".") or p.suffix.lower() not in IMPORT_EXTS:
                continue
            if now - p.stat().st_mtime < INBOX_SETTLE_SECONDS:
                continue  # probably still being copied
            try:
                import_bytes(p.read_bytes(), p.name)
                p.unlink()
                added += 1
            except gpxkml.ParseError as exc:
                log.warning("Rejected inbox file %s: %s", p.name, exc)
                bad = INBOX / "rejected"
                bad.mkdir(exist_ok=True)
                shutil.move(str(p), str(bad / p.name))
                rejected += 1
    return {"added": added, "removed": removed, "rejected": rejected}


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------
def list_files() -> list[dict]:
    with _db() as con:
        rows = con.execute("SELECT * FROM files ORDER BY updated DESC, name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def load(fid: str) -> tuple[dict, dict]:
    _check_id(fid)
    with _db() as con:
        row = con.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if row is None:
        raise NotFound("No such file")
    path = LIB / row["filename"]
    try:
        doc, _ = gpxkml.parse(path.read_bytes(), row["name"])
    except FileNotFoundError:
        raise NotFound("The file has disappeared from the library folder") from None
    return dict(row), doc


def can_undo(fid: str) -> bool:
    d = HIST / _check_id(fid)
    return d.is_dir() and any(d.glob("*.gpx"))


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------
def _snapshot(fid: str, row: dict) -> None:
    d = HIST / fid
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LIB / row["filename"], d / f"{time.time_ns()}.gpx")
    snaps = sorted(d.glob("*.gpx"))
    for old in snaps[:-HIST_KEEP]:
        old.unlink(missing_ok=True)


def edit(fid: str, req: dict) -> None:
    with _lock:
        row, doc = load(fid)
        ops.apply(doc, req)  # raises ops.EditError before anything is written
        _snapshot(fid, row)
        _atomic_write(LIB / row["filename"], gpxkml.to_gpx(doc))
        with _db() as con:
            _update_row(con, fid, doc)


def undo(fid: str) -> None:
    with _lock:
        row, _ = load(fid)
        d = HIST / fid
        snaps = sorted(d.glob("*.gpx")) if d.is_dir() else []
        if not snaps:
            raise ops.EditError("Nothing to undo")
        last = snaps[-1]
        data = last.read_bytes()
        doc, _fmt = gpxkml.parse(data, row["name"])
        _atomic_write(LIB / row["filename"], data.decode("utf-8"))
        last.unlink()
        with _db() as con:
            _update_row(con, fid, doc)


def delete(fid: str) -> None:
    with _lock:
        row, _ = load(fid)
        (LIB / row["filename"]).unlink(missing_ok=True)
        with _db() as con:
            con.execute("DELETE FROM files WHERE id=?", (fid,))
        _forget(fid)


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------
def render(doc: dict, fmt: str) -> tuple[bytes, str, str]:
    """Return (bytes, extension, media type) for fmt 'gpx' or 'kml'."""
    if fmt == "gpx":
        return gpxkml.to_gpx(doc).encode("utf-8"), "gpx", "application/gpx+xml"
    if fmt == "kml":
        return gpxkml.to_kml(doc).encode("utf-8"), "kml", "application/vnd.google-earth.kml+xml"
    raise ops.EditError("format must be 'gpx' or 'kml'")


def export(fid: str, fmt: str) -> tuple[bytes, str, str]:
    _, doc = load(fid)
    data, ext, media = render(doc, fmt)
    return data, f"{_slug(doc['name'])}.{ext}", media


def export_all(fmt: str) -> bytes:
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in list_files():
            try:
                _, doc = load(r["id"])
            except NotFound:
                continue
            data, ext, _ = render(doc, fmt)
            name, n = f"{_slug(doc['name'])}.{ext}", 1
            while name in used:
                n += 1
                name = f"{_slug(doc['name'])}-{n}.{ext}"
            used.add(name)
            zf.writestr(name, data)
    return buf.getvalue()
