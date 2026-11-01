"""HTTP API and static UI (Starlette)."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import gpxkml, ops, storage

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("gpxapp")

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024
RESCAN_SECONDS = int(os.environ.get("RESCAN_SECONDS", "30"))
AUTH_USER = os.environ.get("APP_USER", "")
AUTH_PASSWORD = os.environ.get("APP_PASSWORD", "")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status


def _err(request: Request, exc: ApiError) -> Response:
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


def _bad_request(request: Request, exc: Exception) -> Response:
    return JSONResponse({"detail": str(exc)}, status_code=400)


def _not_found(request: Request, exc: Exception) -> Response:
    return JSONResponse({"detail": str(exc) or "Not found"}, status_code=404)


# --------------------------------------------------------------------------
# middleware
# --------------------------------------------------------------------------
async def security(request: Request, call_next):
    # Optional HTTP basic auth (enabled when APP_USER and APP_PASSWORD are both set).
    if AUTH_USER and AUTH_PASSWORD and request.url.path != "/api/health":
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(user.encode(), AUTH_USER.encode()) & secrets.compare_digest(
                    pw.encode(), AUTH_PASSWORD.encode()
                )
            except Exception:
                ok = False
        if not ok:
            return Response(
                "Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="GPX-KML Manager"'}
            )

    # Refuse cross-site state-changing requests.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
            return Response("Cross-origin request refused", status_code=403)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _detail(fid: str) -> dict:
    row, doc = storage.load(fid)
    return {
        "id": row["id"],
        "name": doc["name"],
        "source_format": row["source_format"],
        "created": row["created"],
        "updated": row["updated"],
        "can_undo": storage.can_undo(fid),
        "stats": gpxkml.doc_stats(doc),
        "tracks": [
            {"name": t["name"], "points": t["points"], "stats": gpxkml.track_stats(t["points"])}
            for t in doc["tracks"]
        ],
        "waypoints": doc["waypoints"],
    }


def _download(data: bytes, filename: str, media: str) -> Response:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "").replace("?", "_")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
    return Response(data, media_type=media, headers={"Content-Disposition": disposition})


async def _read_upload(upload) -> bytes:
    if upload.size is not None and upload.size > MAX_UPLOAD:
        raise ApiError(f"File is larger than the {MAX_UPLOAD // (1024 * 1024)} MB limit", 413)
    data = await upload.read()
    if len(data) > MAX_UPLOAD:
        raise ApiError(f"File is larger than the {MAX_UPLOAD // (1024 * 1024)} MB limit", 413)
    return data


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise ApiError("Request body must be JSON") from None
    if not isinstance(body, dict):
        raise ApiError("Request body must be a JSON object")
    return body


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def index(request: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def list_files(request: Request):
    return JSONResponse({"files": await run_in_threadpool(storage.list_files)})


async def upload_files(request: Request):
    form = await request.form()
    uploads = [u for u in form.getlist("files") if hasattr(u, "read")]
    if not uploads:
        raise ApiError("No files were uploaded")
    results = []
    for u in uploads:
        try:
            data = await _read_upload(u)
            item = await run_in_threadpool(storage.import_bytes, data, u.filename or "upload")
            results.append({"filename": u.filename, "ok": True, **item})
        except (gpxkml.ParseError, ApiError) as exc:
            msg = exc.message if isinstance(exc, ApiError) else str(exc)
            results.append({"filename": u.filename, "ok": False, "error": msg})
    return JSONResponse({"results": results})


async def get_file(request: Request):
    return JSONResponse(await run_in_threadpool(_detail, request.path_params["fid"]))


async def edit_file(request: Request):
    fid = request.path_params["fid"]
    body = await _json_body(request)
    await run_in_threadpool(storage.edit, fid, body)
    return JSONResponse(await run_in_threadpool(_detail, fid))


async def undo_file(request: Request):
    fid = request.path_params["fid"]
    await run_in_threadpool(storage.undo, fid)
    return JSONResponse(await run_in_threadpool(_detail, fid))


async def delete_file(request: Request):
    await run_in_threadpool(storage.delete, request.path_params["fid"])
    return JSONResponse({"ok": True})


async def export_file(request: Request):
    fmt = request.query_params.get("format", "gpx").lower()
    data, name, media = await run_in_threadpool(storage.export, request.path_params["fid"], fmt)
    return _download(data, name, media)


async def export_all(request: Request):
    fmt = request.query_params.get("format", "gpx").lower()
    if fmt not in ("gpx", "kml"):
        raise ApiError("format must be 'gpx' or 'kml'")
    data = await run_in_threadpool(storage.export_all, fmt)
    return _download(data, f"gpx-kml-library-{fmt}.zip", "application/zip")


async def convert(request: Request):
    """Stateless conversion: upload one file, get it back in the other format (nothing is stored)."""
    to = request.query_params.get("to", "").lower()
    if to not in ("gpx", "kml"):
        raise ApiError("'to' must be 'gpx' or 'kml'")
    form = await request.form()
    uploads = [u for u in form.getlist("file") if hasattr(u, "read")]
    if len(uploads) != 1:
        raise ApiError("Upload exactly one file in the 'file' field")
    data = await _read_upload(uploads[0])
    doc, _ = await run_in_threadpool(gpxkml.parse, data, uploads[0].filename or "")
    out, ext, media = await run_in_threadpool(storage.render, doc, to)
    return _download(out, f"{storage._slug(doc['name'])}.{ext}", media)


async def rescan(request: Request):
    return JSONResponse(await run_in_threadpool(storage.rescan))


# --------------------------------------------------------------------------
async def _watch():
    while True:
        await asyncio.sleep(max(5, RESCAN_SECONDS))
        try:
            await run_in_threadpool(storage.rescan)
        except Exception:
            log.exception("Background rescan failed")


@asynccontextmanager
async def lifespan(app):
    await run_in_threadpool(storage.init)
    task = asyncio.create_task(_watch())
    try:
        yield
    finally:
        task.cancel()


routes = [
    Route("/", index),
    Route("/api/health", health),
    Route("/api/files", list_files, methods=["GET"]),
    Route("/api/files", upload_files, methods=["POST"]),
    Route("/api/files/{fid}", get_file, methods=["GET"]),
    Route("/api/files/{fid}", delete_file, methods=["DELETE"]),
    Route("/api/files/{fid}/edit", edit_file, methods=["POST"]),
    Route("/api/files/{fid}/undo", undo_file, methods=["POST"]),
    Route("/api/files/{fid}/export", export_file, methods=["GET"]),
    Route("/api/export-all", export_all, methods=["GET"]),
    Route("/api/convert", convert, methods=["POST"]),
    Route("/api/rescan", rescan, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(BaseHTTPMiddleware, dispatch=security)],
    exception_handlers={
        ApiError: _err,
        gpxkml.ParseError: _bad_request,
        ops.EditError: _bad_request,
        storage.NotFound: _not_found,
    },
    lifespan=lifespan,
)
