from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="browser tests need playwright (pip install -r requirements-dev.txt)")
from playwright.sync_api import expect  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "sample.gpx"  # 20 points, point i at lat 51+0.001*i


def screen_xy(page, index):
    """Viewport pixel position of track point `index` on the real Leaflet map."""
    return page.evaluate(
        """(i) => {
            const p = activeTrack().points[i];
            const r = map.getContainer().getBoundingClientRect();
            const c = map.latLngToContainerPoint([p[0], p[1]]);
            return [r.left + c.x, r.top + c.y];
        }""",
        index,
    )


def sel(page):
    return page.evaluate("[state.selStart, state.selEnd]")


def n_points(page):
    return page.evaluate("state.cur.tracks[0].points.length")


def open_sample(page, base_url):
    page.goto(base_url)
    page.set_input_files("#file-input", str(SAMPLE))
    page.wait_for_function("state.cur && state.cur.name === 'Sample ride'")
    page.locator("#map").scroll_into_view_if_needed()
    assert page.evaluate("typeof L.version") == "string", "real Leaflet should be loaded"
    # let the fit-to-track animation finish so pixel positions are stable
    page.wait_for_timeout(600)


def test_pick_ab_on_map_then_crop_and_undo(page, base_url):
    open_sample(page, base_url)
    assert sel(page) == [0, 19]
    assert page.locator("#btn-crop").is_disabled()

    page.click("#btn-pick-start")
    x, y = screen_xy(page, 5)
    page.mouse.click(x, y)
    assert sel(page)[0] == 5

    page.click("#btn-pick-end")
    x, y = screen_xy(page, 14)
    page.mouse.click(x, y)
    assert sel(page) == [5, 14]
    expect(page.locator("#sel-summary")).to_contain_text("10 points")
    assert page.locator("#btn-crop").is_enabled()

    page.click("#btn-crop")
    page.wait_for_function("state.cur.tracks[0].points.length === 10")
    assert sel(page) == [0, 9]
    assert page.locator("#btn-undo").is_enabled()

    page.click("#btn-undo")
    page.wait_for_function("state.cur.tracks[0].points.length === 20")
    assert page.errors == []


def test_drag_a_pin_moves_selection(page, base_url):
    open_sample(page, base_url)
    page.evaluate("setStart(5); setEnd(14); refreshSelection()")
    page.wait_for_timeout(300)

    pin = page.locator(".pin-icon .pin.a")
    pin.wait_for()
    box = pin.bounding_box()
    sx, sy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    tx, ty = screen_xy(page, 8)

    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move((sx + tx) / 2, (sy + ty) / 2, steps=8)
    page.mouse.move(tx, ty, steps=8)
    page.mouse.up()

    page.wait_for_function("state.selStart === 8")
    assert sel(page) == [8, 14]
    expect(page.locator("#sel-summary")).to_contain_text("7 points")
    assert page.errors == []


def test_remove_section_splits_track_and_split_at_a(page, base_url):
    open_sample(page, base_url)
    page.evaluate("setStart(5); setEnd(9); refreshSelection()")
    page.click("#btn-del-split")
    page.wait_for_function("state.cur.tracks.length === 2")
    assert page.evaluate("state.cur.tracks.map(t => t.points.length)") == [5, 10]

    page.click("#btn-undo")
    page.wait_for_function("state.cur.tracks.length === 1")
    page.evaluate("setStart(8); refreshSelection()")
    page.click("#btn-split")
    page.wait_for_function("state.cur.tracks.length === 2")
    assert page.evaluate("state.cur.tracks.map(t => t.points.length)") == [9, 12]
    assert page.errors == []


def test_clicking_map_in_waypoint_mode_adds_waypoint(page, base_url):
    open_sample(page, base_url)
    page.once("dialog", lambda d: d.accept("Pub"))
    page.click("#btn-add-wpt")
    x, y = screen_xy(page, 10)
    page.mouse.click(x + 25, y - 25)
    page.wait_for_function("state.cur.waypoints.length === 2")
    assert page.evaluate("state.cur.waypoints[1].name") == "Pub"
    assert page.errors == []
