"""GPX / KML / KMZ parsing, writing and statistics (pure functions, no server)."""
import pytest

from app import gpxkml
from helpers import KML, make_gpx, make_kmz


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


def test_name_falls_back_to_the_file_name():
    gpx = b'<gpx version="1.1"><wpt lat="1" lon="2"/></gpx>'
    assert gpxkml.parse(gpx, "path/to/My ride.gpx")[0]["name"] == "My ride"
    assert gpxkml.parse(gpx, "C:\\rides\\Evening.gpx")[0]["name"] == "Evening"
    assert gpxkml.parse(gpx, "")[0]["name"] == "Untitled"


def test_kmz_is_unpacked_and_prefers_doc_kml():
    kmz = make_kmz({"aaa.kml": KML.replace(b"KML trip", b"Other"), "doc.kml": KML, "files/icon.png": b"\x89PNG"})
    doc, fmt = gpxkml.parse(kmz, "trip.kmz")
    assert fmt == "kml" and doc["name"] == "KML trip"


def test_kmz_without_kml_or_with_a_huge_kml_is_rejected(monkeypatch):
    with pytest.raises(gpxkml.ParseError, match="does not contain"):
        gpxkml.parse(make_kmz({"readme.txt": b"hello"}), "a.kmz")
    monkeypatch.setattr(gpxkml, "MAX_UNZIPPED", 10)
    with pytest.raises(gpxkml.ParseError, match="too large"):
        gpxkml.parse(make_kmz({"doc.kml": KML}), "a.kmz")


def test_byte_order_mark_is_accepted_and_out_of_range_points_are_dropped():
    gpx = (
        b'\xef\xbb\xbf<?xml version="1.0"?><gpx version="1.1"><trk><trkseg>'
        b'<trkpt lat="51" lon="0"/><trkpt lat="99" lon="0"/><trkpt lat="51.1" lon="0"/>'
        b"</trkseg></trk></gpx>"
    )
    doc, _ = gpxkml.parse(gpx, "b.gpx")
    assert [p[0] for p in doc["tracks"][0]["points"]] == [51, 51.1]


def test_file_with_only_invalid_points_is_rejected():
    gpx = b'<gpx version="1.1"><trk><trkseg><trkpt lat="99" lon="0"/></trkseg></trk></gpx>'
    with pytest.raises(gpxkml.ParseError, match="no tracks"):
        gpxkml.parse(gpx, "x.gpx")


def test_short_tracks_current_behaviour():
    # Documents today's behaviour: a one-point GPX track is imported (it just cannot be cropped or split),
    # whereas a one-point KML line is ignored.
    gpx, _ = gpxkml.parse(b'<gpx version="1.1"><trk><trkseg><trkpt lat="51" lon="0"/></trkseg></trk></gpx>', "c.gpx")
    assert [len(t["points"]) for t in gpx["tracks"]] == [1]
    kml = b"<kml><Document><Placemark><name>P</name><LineString><coordinates>0,51,0</coordinates></LineString></Placemark></Document></kml>"
    with pytest.raises(gpxkml.ParseError):
        gpxkml.parse(kml, "c.kml")


@pytest.mark.parametrize("bad", [b"not xml", b"<foo/>", b"<gpx></gpx>", b"PK\x03\x04garbage"])
def test_bad_input_rejected(bad):
    with pytest.raises(gpxkml.ParseError):
        gpxkml.parse(bad, "bad.gpx")


def test_entity_expansion_rejected():
    evil = b'<?xml version="1.0"?><!DOCTYPE gpx [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><gpx><metadata><name>&b;</name></metadata></gpx>'
    with pytest.raises(gpxkml.ParseError):
        gpxkml.parse(evil, "evil.gpx")


# ---------------------------------------------------------------- writing
def test_roundtrip_gpx_kml_gpx_keeps_data():
    doc, _ = gpxkml.parse(make_gpx(10), "x.gpx")
    kml = gpxkml.to_kml(doc).encode()
    doc2, fmt = gpxkml.parse(kml, "x.kml")
    assert fmt == "kml"
    assert len(doc2["tracks"][0]["points"]) == 10
    assert doc2["tracks"][0]["points"][3][3] == doc["tracks"][0]["points"][3][3]  # timestamps survive via gx:Track
    gpx2, _ = gpxkml.parse(gpxkml.to_gpx(doc2).encode(), "y.gpx")
    assert gpx2["tracks"][0]["points"][5][:3] == doc["tracks"][0]["points"][5][:3]


@pytest.mark.xfail(
    strict=True,
    reason="Known limitation: to_kml only writes gx:Track (with times) when EVERY point has a time, "
    "so one missing timestamp silently drops all of them. Remove this marker once fixed.",
)
def test_kml_export_keeps_timestamps_when_one_point_has_none():
    doc, _ = gpxkml.parse(make_gpx(6), "x.gpx")
    doc["tracks"][0]["points"][2][3] = None
    back, _ = gpxkml.parse(gpxkml.to_kml(doc).encode(), "x.kml")
    assert any(p[3] for p in back["tracks"][0]["points"])


def test_special_characters_are_escaped():
    doc = {"name": "A <b> & c", "description": "", "waypoints": [{"name": "x\x01<y>", "desc": "", "lat": 1, "lon": 2, "ele": None}],
           "tracks": [{"name": "t", "points": [[1, 2, None, None], [1.1, 2.1, None, None]]}]}
    for out, fmt in ((gpxkml.to_gpx(doc), "gpx"), (gpxkml.to_kml(doc), "kml")):
        back, _ = gpxkml.parse(out.encode(), "f." + fmt)
        assert back["name"] == "A <b> & c" and back["waypoints"][0]["name"] == "x<y>"


def test_tiny_negative_numbers_are_written_as_zero_not_minus_zero():
    doc = {"name": "n", "description": "", "tracks": [],
           "waypoints": [{"name": "w", "desc": "", "lat": -1e-9, "lon": 1e-9, "ele": None}]}
    assert '<wpt lat="0" lon="0">' in gpxkml.to_gpx(doc)


# ---------------------------------------------------------------- statistics
def track(eles, times=None):
    return [[51 + i * 0.001, 0, e, (times[i] if times else None)] for i, e in enumerate(eles)]


def test_distance_reasonable():
    # 1 degree of latitude is about 111.2 km
    d = gpxkml.distance([[0, 0, None, None], [1, 0, None, None]])
    assert 111000 < d < 111400
    assert gpxkml.distance([]) == 0 and gpxkml.distance([[1, 1, None, None]]) == 0


def test_ascent_and_descent_ignore_gps_noise_but_count_real_climbs():
    noisy = gpxkml.track_stats(track([100, 101, 100, 102, 100, 101]))
    assert (noisy["ascent_m"], noisy["descent_m"]) == (0, 0)
    climb = gpxkml.track_stats(track([100, 101, 102, 103, 110, 105, 100]))
    assert (climb["ascent_m"], climb["descent_m"]) == (10, 10)
    assert (climb["min_ele"], climb["max_ele"]) == (100, 110)


def test_track_stats_without_elevation_or_times():
    st = gpxkml.track_stats(track([None, None, None]))
    assert st["points"] == 3 and st["min_ele"] is None and st["ascent_m"] == 0
    assert st["duration_s"] is None and st["start_time"] is None


def test_duration_uses_first_and_last_time_and_ignores_unparseable_ones():
    doc, _ = gpxkml.parse(make_gpx(10), "x.gpx")
    st = gpxkml.track_stats(doc["tracks"][0]["points"])
    assert st["start_time"] == "2026-05-01T08:00:00Z" and st["end_time"] == "2026-05-01T08:00:09Z"
    assert st["duration_s"] == 9.0
    assert st["ascent_m"] == 16  # +2 m per point, counted each time the 3 m threshold is passed
    junk = gpxkml.track_stats(track([1, 2], ["yesterday", "today"]))
    assert junk["duration_s"] is None and junk["start_time"] is None


def test_document_stats_cover_tracks_and_waypoints():
    doc, _ = gpxkml.parse(make_gpx(20), "x.gpx")
    st = gpxkml.doc_stats(doc)
    assert (st["tracks"], st["waypoints"], st["points"]) == (2, 1, 22)
    assert st["bounds"][0] == pytest.approx([51.0, -0.5]) and st["bounds"][1] == pytest.approx([51.6, -0.1])
    assert st["distance_m"] > 0 and st["duration_s"] == 19.0


def test_document_stats_for_waypoints_only():
    st = gpxkml.doc_stats({"name": "w", "tracks": [], "waypoints": [{"lat": 5, "lon": 6, "name": "", "desc": "", "ele": None}]})
    assert st["distance_m"] == 0 and st["duration_s"] is None and st["start_time"] is None
    assert st["bounds"] == [[5, 6], [5, 6]]
