"""Fixtures shared by the backend and browser tests."""
import pytest

from helpers import Client, start_server, stop_server


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Remember each phase's report on the test item (the browser tests use this to take screenshots)."""
    outcome = yield
    setattr(item, "rep_" + call.when, outcome.get_result())


@pytest.fixture()
def make_server(tmp_path, request):
    """Start the real app in a subprocess. Call it once per test, optionally with environment variables."""
    started = []

    def make(extra_env=None):
        proc, base = start_server(tmp_path, extra_env)
        started.append(proc)
        return Client(base), tmp_path / "data"

    yield make
    for proc in started:
        stop_server(proc)
    rep = getattr(request.node, "rep_call", None)
    if started and rep is not None and rep.failed:
        # The server's own output is the first thing you want when an HTTP test fails.
        print("\n--- server log ---\n" + (tmp_path / "server.log").read_text(errors="replace"))


@pytest.fixture()
def server(make_server):
    """(client, data_dir) for a server with default settings."""
    return make_server()


@pytest.fixture()
def store(tmp_path):
    """The storage module pointed at an empty temporary data folder (no server, no sleeps)."""
    from app import storage

    storage.init(tmp_path)
    return storage
