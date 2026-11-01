"""Edit operations on the internal document model.

Every operation mutates the document in place and raises EditError on bad
input. Callers load a fresh document per request and only save on success, so
a failed operation never leaves a half-edited file behind.
"""
from __future__ import annotations


class EditError(ValueError):
    pass


def _int(req: dict, key: str) -> int:
    v = req.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise EditError(f"'{key}' must be an integer")
    return v


def _track(doc: dict, i: int) -> dict:
    if not 0 <= i < len(doc["tracks"]):
        raise EditError("No such track")
    return doc["tracks"][i]


def _selection(track: dict, start: int, end: int) -> tuple[int, int]:
    n = len(track["points"])
    if not 0 <= start <= end < n:
        raise EditError("Invalid selection")
    return start, end


def _name(value, what: str = "Name") -> str:
    if not isinstance(value, str) or not value.strip():
        raise EditError(f"{what} must not be empty")
    return value.strip()[:200]


def _coord(req: dict, key: str, lo: float, hi: float) -> float:
    v = req.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise EditError(f"'{key}' must be a number between {lo} and {hi}")
    return float(v)


# ---- track operations ----------------------------------------------------
def crop(doc, req):
    """Keep only points start..end (inclusive) of a track."""
    i = _int(req, "track")
    t = _track(doc, i)
    s, e = _selection(t, _int(req, "start"), _int(req, "end"))
    if e - s < 1:
        raise EditError("The selection must contain at least 2 points")
    t["points"] = t["points"][s : e + 1]


def delete_section(doc, req):
    """Remove points start..end (inclusive).

    mode "split" (default): the remainder becomes two tracks (before / after).
    mode "join": the two remainders are joined into a single track.
    """
    i = _int(req, "track")
    t = _track(doc, i)
    s, e = _selection(t, _int(req, "start"), _int(req, "end"))
    mode = req.get("mode", "split")
    if mode not in ("split", "join"):
        raise EditError("mode must be 'split' or 'join'")
    pts = t["points"]
    left, right = pts[:s], pts[e + 1 :]
    if mode == "join":
        joined = left + right
        if len(joined) < 2:
            raise EditError("Nothing meaningful would remain; delete the track instead")
        t["points"] = joined
        return
    pieces = [p for p in (left, right) if len(p) >= 2]
    if not pieces:
        raise EditError("Nothing meaningful would remain; delete the track instead")
    if len(pieces) == 1:
        t["points"] = pieces[0]
        return
    base = t["name"]
    doc["tracks"][i : i + 1] = [
        {"name": f"{base} (1)", "points": pieces[0]},
        {"name": f"{base} (2)", "points": pieces[1]},
    ]


def split(doc, req):
    """Split a track in two at point `index` (the point belongs to both halves)."""
    i = _int(req, "track")
    t = _track(doc, i)
    idx = _int(req, "index")
    n = len(t["points"])
    if not 0 < idx < n - 1:
        raise EditError("Choose a point that is not the first or last point")
    base = t["name"]
    doc["tracks"][i : i + 1] = [
        {"name": f"{base} (1)", "points": t["points"][: idx + 1]},
        {"name": f"{base} (2)", "points": t["points"][idx:]},
    ]


def reverse(doc, req):
    """Reverse the direction of a track. Timestamps would run backwards, so they are dropped."""
    t = _track(doc, _int(req, "track"))
    t["points"] = [[p[0], p[1], p[2], None] for p in reversed(t["points"])]


def rename_track(doc, req):
    _track(doc, _int(req, "track"))["name"] = _name(req.get("name"))


def delete_track(doc, req):
    i = _int(req, "track")
    _track(doc, i)
    del doc["tracks"][i]


def merge(doc, req):
    """Concatenate the listed tracks (in the order given) into one track."""
    idxs = req.get("tracks")
    if (
        not isinstance(idxs, list)
        or len(idxs) < 2
        or len(set(idxs)) != len(idxs)
        or any(isinstance(x, bool) or not isinstance(x, int) for x in idxs)
    ):
        raise EditError("'tracks' must list at least two distinct track indexes")
    tracks = [_track(doc, i) for i in idxs]
    merged = {"name": tracks[0]["name"], "points": [p for t in tracks for p in t["points"]]}
    first = min(idxs)
    doc["tracks"] = [t for k, t in enumerate(doc["tracks"]) if k not in set(idxs)]
    doc["tracks"].insert(first, merged)


# ---- waypoint operations -------------------------------------------------
def add_waypoint(doc, req):
    name = req.get("name", "")
    desc = req.get("desc", "")
    if not isinstance(name, str) or not isinstance(desc, str):
        raise EditError("name and desc must be text")
    doc["waypoints"].append(
        {
            "name": name.strip()[:200],
            "desc": desc.strip()[:2000],
            "lat": _coord(req, "lat", -90, 90),
            "lon": _coord(req, "lon", -180, 180),
            "ele": None,
        }
    )


def _waypoint(doc, req) -> dict:
    i = _int(req, "waypoint")
    if not 0 <= i < len(doc["waypoints"]):
        raise EditError("No such waypoint")
    return doc["waypoints"][i]


def update_waypoint(doc, req):
    w = _waypoint(doc, req)
    if "name" in req:
        if not isinstance(req["name"], str):
            raise EditError("name must be text")
        w["name"] = req["name"].strip()[:200]
    if "desc" in req:
        if not isinstance(req["desc"], str):
            raise EditError("desc must be text")
        w["desc"] = req["desc"].strip()[:2000]


def delete_waypoint(doc, req):
    _waypoint(doc, req)
    del doc["waypoints"][_int(req, "waypoint")]


# ---- file operations -----------------------------------------------------
def rename_file(doc, req):
    doc["name"] = _name(req.get("name"))


OPS = {
    "crop": crop,
    "delete_section": delete_section,
    "split": split,
    "reverse": reverse,
    "rename_track": rename_track,
    "delete_track": delete_track,
    "merge": merge,
    "add_waypoint": add_waypoint,
    "update_waypoint": update_waypoint,
    "delete_waypoint": delete_waypoint,
    "rename_file": rename_file,
}


def apply(doc: dict, req: dict) -> dict:
    fn = OPS.get(req.get("op"))
    if fn is None:
        raise EditError(f"Unknown operation {req.get('op')!r}")
    fn(doc, req)
    return doc
