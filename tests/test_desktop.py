"""Running without a command window: nothing to set up off Windows, and Quit in the web UI."""

import sys

from mcsm import desktop

from test_hub import login
from test_web import wait_for


def test_no_setup_needed_off_windows(capsys):
    before = sys.stdout
    desktop.setup()
    assert sys.stdout is before and not desktop.windowless()
    assert desktop.NO_WINDOW == {}
    desktop.show_error("boom")
    assert "boom" in capsys.readouterr().err


def test_quit_from_the_web_ui(hub_env):
    hub, c = hub_env
    login(c)
    assert c.post("/api/hub/quit")[0] == 200
    wait_for(lambda: hub.stop_requested.is_set(), timeout=5)


def test_icon_is_served(hub_env):
    hub, c = hub_env
    status, body, headers = c.get("/icon.png")
    assert status == 200 and headers["Content-Type"] == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"
