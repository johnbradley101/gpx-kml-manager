"""Edit operations on the document model (pure functions, no server)."""
import pytest

from app import gpxkml, ops
from helpers import make_gpx


def fresh():
    """Tracks: 'Main' (20 points) and 'Planned' (2 points); one waypoint."""
    doc, _ = gpxkml.parse(make_gpx(20), "x.gpx")
    return doc


def three_tracks():
    d = fresh()
    d["tracks"].append({"name": "Extra", "points": [[52, 0, None, None], [52.1, 0, None, None]]})
    return d


def lens(d):
    return [len(t["points"]) for t in d["tracks"]]


def raises_edit_error(doc, req):
    with pytest.raises(ops.EditError):
        ops.apply(doc, req)


# ---------------------------------------------------------------- track operations
def test_crop():
    d = fresh()
    ops.apply(d, {"op": "crop", "track": 0, "start": 5, "end": 14})
    assert len(d["tracks"][0]["points"]) == 10
    assert d["tracks"][0]["points"][0][2] == 100 + 5 * 2


def test_crop_needs_at_least_two_points():
    raises_edit_error(fresh(), {"op": "crop", "track": 0, "start": 4, "end": 4})


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
    raises_edit_error(fresh(), {"op": "delete_section", "track": 0, "start": 0, "end": 19})


def test_delete_section_split_drops_a_single_leftover_point():
    # Documents today's behaviour: one point before the cut cannot form a track, so it is discarded and the
    # rest stays a single track (no "(1)"/"(2)" split). Decide whether that is what users should get.
    d = fresh()
    ops.apply(d, {"op": "delete_section", "track": 0, "start": 1, "end": 5})
    assert lens(d) == [14, 2] and d["tracks"][0]["name"] == "Main"


def test_delete_section_join_refuses_to_leave_fewer_than_two_points():
    raises_edit_error(fresh(), {"op": "delete_section", "track": 0, "start": 1, "end": 19, "mode": "join"})


def test_delete_section_rejects_unknown_mode():
    raises_edit_error(fresh(), {"op": "delete_section", "track": 0, "start": 2, "end": 4, "mode": "shred"})


def test_split_shares_split_point():
    d = fresh()
    ops.apply(d, {"op": "split", "track": 0, "index": 7})
    a, b = d["tracks"][0]["points"], d["tracks"][1]["points"]
    assert len(a) == 8 and len(b) == 13 and a[-1] == b[0]


@pytest.mark.parametrize("index", [0, 19, 20, -1])
def test_split_refuses_first_last_and_out_of_range_points(index):
    raises_edit_error(fresh(), {"op": "split", "track": 0, "index": index})


def test_reverse_drops_times_and_keeps_elevation():
    d = fresh()
    first = d["tracks"][0]["points"][0]
    ops.apply(d, {"op": "reverse", "track": 0})
    last = d["tracks"][0]["points"][-1]
    assert last[:3] == first[:3] and all(p[3] is None for p in d["tracks"][0]["points"])


def test_merge_rename_delete():
    d = fresh()
    ops.apply(d, {"op": "merge", "tracks": [0, 1]})
    assert len(d["tracks"]) == 1 and len(d["tracks"][0]["points"]) == 22
    ops.apply(d, {"op": "rename_track", "track": 0, "name": "  New  "})
    assert d["tracks"][0]["name"] == "New"
    ops.apply(d, {"op": "delete_track", "track": 0})
    assert d["tracks"] == []


def test_merge_uses_the_order_given_and_leaves_other_tracks_alone():
    d = three_tracks()
    ops.apply(d, {"op": "merge", "tracks": [2, 0]})
    assert [t["name"] for t in d["tracks"]] == ["Extra", "Planned"]
    assert lens(d) == [22, 2] and d["tracks"][0]["points"][0][0] == 52  # Extra's points come first


def test_merge_of_non_adjacent_tracks():
    d = three_tracks()
    ops.apply(d, {"op": "merge", "tracks": [0, 2]})
    assert [t["name"] for t in d["tracks"]] == ["Main", "Planned"] and lens(d) == [22, 2]


@pytest.mark.parametrize("tracks", [[0], [], [0, 0], [0, 9], [True, 0], ["0", "1"], None, "01"])
def test_merge_rejects_bad_track_lists(tracks):
    raises_edit_error(three_tracks(), {"op": "merge", "tracks": tracks})


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "crop", "track": 0, "start": 9, "end": 3},
        {"op": "crop", "track": 0, "start": 0, "end": 99},
        {"op": "crop", "track": 0, "start": -1, "end": 5},
        {"op": "crop", "track": "0", "start": 0, "end": 5},
        {"op": "crop", "track": True, "start": 0, "end": 5},
        {"op": "crop", "track": 9, "start": 0, "end": 5},
        {"op": "rename_track", "track": 0, "name": "  "},
        {"op": "rename_track", "track": 0, "name": 5},
        {"op": "rename_file", "name": ""},
        {"op": "delete_track", "track": 5},
        {"op": "nope"},
        {},
    ],
)
def test_bad_track_requests_are_rejected(bad):
    raises_edit_error(fresh(), bad)


# ---------------------------------------------------------------- waypoints and names
def test_waypoint_ops():
    d = fresh()
    ops.apply(d, {"op": "add_waypoint", "lat": 51.2, "lon": -0.4, "name": "Pub"})
    assert d["waypoints"][-1]["name"] == "Pub"
    ops.apply(d, {"op": "update_waypoint", "waypoint": 0, "name": "Renamed"})
    ops.apply(d, {"op": "delete_waypoint", "waypoint": 1})
    assert [w["name"] for w in d["waypoints"]] == ["Renamed"]


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "add_waypoint", "lat": 99, "lon": 0},
        {"op": "add_waypoint", "lat": 1, "lon": 181},
        {"op": "add_waypoint", "lat": float("nan"), "lon": 0},
        {"op": "add_waypoint", "lat": True, "lon": 2},
        {"op": "add_waypoint", "lat": "1", "lon": 2},
        {"op": "add_waypoint", "lat": 1, "lon": 2, "name": 5},
        {"op": "update_waypoint", "waypoint": 0, "name": 5},
        {"op": "update_waypoint", "waypoint": 0, "desc": None},
        {"op": "update_waypoint", "waypoint": 7, "name": "x"},
        {"op": "delete_waypoint", "waypoint": True},
        {"op": "delete_waypoint", "waypoint": 3},
    ],
)
def test_bad_waypoint_requests_are_rejected(bad):
    raises_edit_error(fresh(), bad)


def test_text_is_trimmed_and_capped():
    d = fresh()
    ops.apply(d, {"op": "add_waypoint", "lat": 1, "lon": 2, "name": "  " + "x" * 300 + "  ", "desc": "y" * 3000})
    w = d["waypoints"][-1]
    assert len(w["name"]) == 200 and len(w["desc"]) == 2000
    ops.apply(d, {"op": "rename_file", "name": "z" * 300})
    assert len(d["name"]) == 200
