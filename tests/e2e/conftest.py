"""Browser tests: real server + real Leaflet + headless Chromium.

Needs:  pip install playwright && playwright install chromium
and Leaflet in app/static/vendor/leaflet (the Docker build / README explain how to fetch it).
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHOTS = Path(__file__).resolve().parent / "screenshots"
LEAFLET = ROOT / "app" / "static" / "vendor" / "leaflet" / "leaflet.js"


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    setattr(item, "rep_" + call.when, outcome.get_result())


@pytest.fixture(scope="session")
def base_url(tmp_path_factory):
    if not LEAFLET.exists():
        msg = f"Leaflet not found at {LEAFLET} - fetch it first (see README, 'Development')"
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail(msg)
        pytest.skip(msg)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, DATA_DIR=str(tmp_path_factory.mktemp("data")))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("server did not start")
    yield url
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture()
def page(browser, request):
    ctx = browser.new_context(viewport={"width": 1400, "height": 1000})
    pg = ctx.new_page()
    # No internet needed: map tiles are not what is being tested.
    pg.route("**/*tile.openstreetmap.org/**", lambda r: r.abort())
    pg.route("**/*opentopomap.org/**", lambda r: r.abort())
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed:
        SHOTS.mkdir(exist_ok=True)
        pg.screenshot(path=str(SHOTS / f"{request.node.name}.png"), full_page=True)
    ctx.close()
