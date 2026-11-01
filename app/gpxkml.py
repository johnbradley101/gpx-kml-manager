"""GPX / KML parsing, writing and geometry helpers.

Internal document model (plain dicts and lists, easy to serialise):

    {"name": str, "description": str,
     "waypoints": [{"name", "desc", "lat", "lon", "ele"}],
     "tracks": [{"name": str, "points": [[lat, lon, ele|None, time|None], ...]}]}

Simplifications made on import:
  * a GPX track with several <trkseg> is flattened into one point list
  * GPX routes (<rte>) and KML LineStrings / LinearRings become tracks
  * KML gx:Track (with timestamps) becomes a track with times
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from defusedxml import ElementTree as DET

EARTH_R = 6371008.8
MAX_UNZIPPED = 200 * 1024 * 1024
_BAD_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


class ParseError(ValueError):
    """The uploaded data is not a usable GPX/KML file."""


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(el, name: str):
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return None


def _float(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _valid(lat, lon) -> bool:
    return lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180


def stem(filename: str) -> str:
    base = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------
def parse(data: bytes, filename: str = "") -> tuple[dict, str]:
    """Parse GPX, KML or KMZ bytes. Returns (document, source_format)."""
    if data[:2] == b"PK":
        data = _unzip_kml(data)
    try:
        root = DET.fromstring(data)
    except Exception as exc:  # malformed XML, forbidden entities, ...
        raise ParseError(f"Not a valid GPX/KML file ({exc.__class__.__name__})") from exc

    kind = _local(root.tag).lower()
    if kind == "gpx":
        doc, fmt = _parse_gpx(root), "gpx"
    elif kind == "kml":
        doc, fmt = _parse_kml(root), "kml"
    else:
        raise ParseError("Unrecognised file: expected a <gpx> or <kml> document")

    if not doc["tracks"] and not doc["waypoints"]:
        raise ParseError("The file contains no tracks, routes or waypoints")
    doc["name"] = (doc["name"] or stem(filename) or "Untitled").strip()[:200]
    return doc, fmt


def _unzip_kml(data: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            kmls = [i for i in zf.infolist() if i.filename.lower().endswith(".kml")]
            if not kmls:
                raise ParseError("The archive does not contain a .kml file")
            kmls.sort(key=lambda i: (i.filename.lower() != "doc.kml", i.filename))
            info = kmls[0]
            if info.file_size > MAX_UNZIPPED:
                raise ParseError("The KML inside the archive is too large")
            return zf.read(info)
    except zipfile.BadZipFile as exc:
        raise ParseError("Not a valid KMZ archive") from exc


def _gpx_point(el):
    lat, lon = _float(el.get("lat")), _float(el.get("lon"))
    if not _valid(lat, lon):
        return None
    return [lat, lon, _float(_child_text(el, "ele")), _child_text(el, "time") or None]


def _parse_gpx(root) -> dict:
    doc = {"name": "", "description": "", "waypoints": [], "tracks": []}
    for child in root:
        n = _local(child.tag)
        if n == "metadata":
            doc["name"] = _child_text(child, "name") or ""
            doc["description"] = _child_text(child, "desc") or ""
        elif n == "wpt":
            p = _gpx_point(child)
            if p:
                doc["waypoints"].append(
                    {
                        "name": _child_text(child, "name") or "",
                        "desc": _child_text(child, "desc") or "",
                        "lat": p[0],
                        "lon": p[1],
                        "ele": p[2],
                    }
                )
        elif n == "trk":
            pts = []
            for seg in child:
                if _local(seg.tag) == "trkseg":
                    for tp in seg:
                        if _local(tp.tag) == "trkpt":
                            p = _gpx_point(tp)
                            if p:
                                pts.append(p)
            if pts:
                doc["tracks"].append(
                    {"name": _child_text(child, "name") or f"Track {len(doc['tracks']) + 1}", "points": pts}
                )
        elif n == "rte":
            pts = [p for p in (_gpx_point(rp) for rp in child if _local(rp.tag) == "rtept") if p]
            if pts:
                doc["tracks"].append(
                    {"name": _child_text(child, "name") or f"Route {len(doc['tracks']) + 1}", "points": pts}
                )
    return doc


def _kml_coords(text) -> list:
    pts = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        lon, lat = _float(parts[0]), _float(parts[1])
        if _valid(lat, lon):
            pts.append([lat, lon, _float(parts[2]) if len(parts) > 2 else None, None])
    return pts


def _kml_gx_track(el) -> list:
    whens = [(c.text or "").strip() for c in el if _local(c.tag) == "when"]
    coords = [(c.text or "").split() for c in el if _local(c.tag) == "coord"]
    pts = []
    for i, parts in enumerate(coords):
        if len(parts) < 2:
            continue
        lon, lat = _float(parts[0]), _float(parts[1])
        if _valid(lat, lon):
            pts.append(
                [
                    lat,
                    lon,
                    _float(parts[2]) if len(parts) > 2 else None,
                    (whens[i] or None) if i < len(whens) else None,
                ]
            )
    return pts


def _parse_kml(root) -> dict:
    doc = {"name": "", "description": "", "waypoints": [], "tracks": []}
    for el in root.iter():
        if _local(el.tag) in ("Document", "Folder"):
            n = _child_text(el, "name")
            if n:
                doc["name"] = n
                doc["description"] = _child_text(el, "description") or ""
                break

    for pm in root.iter():
        if _local(pm.tag) != "Placemark":
            continue
        name = _child_text(pm, "name") or ""
        desc = _child_text(pm, "description") or ""
        for g in pm.iter():
            n = _local(g.tag)
            if n == "Point":
                pts = _kml_coords(_child_text(g, "coordinates"))
                if pts:
                    doc["waypoints"].append(
                        {"name": name, "desc": desc, "lat": pts[0][0], "lon": pts[0][1], "ele": pts[0][2]}
                    )
            elif n in ("LineString", "LinearRing"):
                pts = _kml_coords(_child_text(g, "coordinates"))
                if len(pts) >= 2:
                    doc["tracks"].append({"name": name or f"Track {len(doc['tracks']) + 1}", "points": pts})
            elif n == "Track":
                pts = _kml_gx_track(g)
                if len(pts) >= 2:
                    doc["tracks"].append({"name": name or f"Track {len(doc['tracks']) + 1}", "points": pts})
    return doc


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------
def _x(s) -> str:
    return escape(_BAD_XML_CHARS.sub("", str(s if s is not None else "")))


def _num(v, places: int) -> str:
    s = f"{v:.{places}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def to_gpx(doc: dict) -> str:
    o = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="GPX-KML Manager" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <metadata>",
        f"    <name>{_x(doc.get('name'))}</name>",
    ]
    if doc.get("description"):
        o.append(f"    <desc>{_x(doc['description'])}</desc>")
    o.append("  </metadata>")
    for w in doc["waypoints"]:
        o.append(f'  <wpt lat="{_num(w["lat"], 7)}" lon="{_num(w["lon"], 7)}">')
        if w.get("ele") is not None:
            o.append(f"    <ele>{_num(w['ele'], 2)}</ele>")
        if w.get("name"):
            o.append(f"    <name>{_x(w['name'])}</name>")
        if w.get("desc"):
            o.append(f"    <desc>{_x(w['desc'])}</desc>")
        o.append("  </wpt>")
    for t in doc["tracks"]:
        o.append("  <trk>")
        o.append(f"    <name>{_x(t['name'])}</name>")
        o.append("    <trkseg>")
        for lat, lon, ele, tm in t["points"]:
            head = f'      <trkpt lat="{_num(lat, 7)}" lon="{_num(lon, 7)}"'
            if ele is None and not tm:
                o.append(head + "/>")
                continue
            o.append(head + ">")
            if ele is not None:
                o.append(f"        <ele>{_num(ele, 2)}</ele>")
            if tm:
                o.append(f"        <time>{_x(tm)}</time>")
            o.append("      </trkpt>")
        o.append("    </trkseg>")
        o.append("  </trk>")
    o.append("</gpx>")
    return "\n".join(o) + "\n"


def to_kml(doc: dict) -> str:
    o = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">',
        "<Document>",
        f"  <name>{_x(doc.get('name'))}</name>",
    ]
    if doc.get("description"):
        o.append(f"  <description>{_x(doc['description'])}</description>")
    o.append('  <Style id="trk"><LineStyle><color>ff0000ff</color><width>4</width></LineStyle></Style>')
    for w in doc["waypoints"]:
        coord = f"{_num(w['lon'], 7)},{_num(w['lat'], 7)}"
        if w.get("ele") is not None:
            coord += f",{_num(w['ele'], 2)}"
        o.append("  <Placemark>")
        o.append(f"    <name>{_x(w.get('name'))}</name>")
        if w.get("desc"):
            o.append(f"    <description>{_x(w['desc'])}</description>")
        o.append(f"    <Point><coordinates>{coord}</coordinates></Point>")
        o.append("  </Placemark>")
    for t in doc["tracks"]:
        pts = t["points"]
        o.append("  <Placemark>")
        o.append(f"    <name>{_x(t['name'])}</name>")
        o.append("    <styleUrl>#trk</styleUrl>")
        if pts and all(p[3] for p in pts):
            o.append("    <gx:Track>")
            for p in pts:
                o.append(f"      <when>{_x(p[3])}</when>")
            for lat, lon, ele, _ in pts:
                c = f"{_num(lon, 7)} {_num(lat, 7)}"
                if ele is not None:
                    c += f" {_num(ele, 2)}"
                o.append(f"      <gx:coord>{c}</gx:coord>")
            o.append("    </gx:Track>")
        else:
            o.append("    <LineString>")
            o.append("      <tessellate>1</tessellate>")
            o.append("      <coordinates>")
            for lat, lon, ele, _ in pts:
                c = f"{_num(lon, 7)},{_num(lat, 7)}"
                if ele is not None:
                    c += f",{_num(ele, 2)}"
                o.append("        " + c)
            o.append("      </coordinates>")
            o.append("    </LineString>")
        o.append("  </Placemark>")
    o.append("</Document>")
    o.append("</kml>")
    return "\n".join(o) + "\n"


# --------------------------------------------------------------------------
# geometry / statistics
# --------------------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def distance(points) -> float:
    return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))


def parse_time(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ascent_descent(eles, threshold: float = 3.0):
    asc = desc = 0.0
    ref = None
    for e in eles:
        if ref is None:
            ref = e
            continue
        d = e - ref
        if d >= threshold:
            asc += d
            ref = e
        elif d <= -threshold:
            desc -= d
            ref = e
    return asc, desc


def track_stats(points) -> dict:
    eles = [p[2] for p in points if p[2] is not None]
    asc, desc = _ascent_descent(eles)
    times = [(p[3], parse_time(p[3])) for p in points if p[3]]
    times = [(raw, dt) for raw, dt in times if dt]
    duration = None
    if len(times) >= 2:
        duration = max(0.0, (times[-1][1] - times[0][1]).total_seconds())
    return {
        "points": len(points),
        "distance_m": round(distance(points), 1),
        "ascent_m": round(asc),
        "descent_m": round(desc),
        "min_ele": round(min(eles), 1) if eles else None,
        "max_ele": round(max(eles), 1) if eles else None,
        "start_time": times[0][0] if times else None,
        "end_time": times[-1][0] if times else None,
        "duration_s": duration,
    }


def doc_stats(doc: dict) -> dict:
    per = [track_stats(t["points"]) for t in doc["tracks"]]
    lats = [p[0] for t in doc["tracks"] for p in t["points"]] + [w["lat"] for w in doc["waypoints"]]
    lons = [p[1] for t in doc["tracks"] for p in t["points"]] + [w["lon"] for w in doc["waypoints"]]
    starts = sorted(s["start_time"] for s in per if s["start_time"] and parse_time(s["start_time"]))
    durs = [s["duration_s"] for s in per if s["duration_s"] is not None]
    return {
        "tracks": len(doc["tracks"]),
        "waypoints": len(doc["waypoints"]),
        "points": sum(s["points"] for s in per),
        "distance_m": round(sum(s["distance_m"] for s in per), 1),
        "ascent_m": sum(s["ascent_m"] for s in per),
        "descent_m": sum(s["descent_m"] for s in per),
        "start_time": starts[0] if starts else None,
        "duration_s": sum(durs) if durs else None,
        "bounds": [[min(lats), min(lons)], [max(lats), max(lons)]] if lats else None,
    }
