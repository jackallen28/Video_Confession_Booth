"""Fitting the connected screen: drawing size, and restarting (only when
nobody is recording) after a screen is plugged in, swapped or unplugged.

    python3 tests/test_display.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import logging  # noqa: E402

import pygame  # noqa: E402

from booth import config, display  # noqa: E402
from booth.app import App  # noqa: E402

logging.basicConfig(level=logging.WARNING)

# --- drawing size for big screens
assert display.render_size((1920, 1080), "1280x720") == (1280, 720)
assert display.render_size((3840, 2160), "1280x720") == (1280, 720)
assert display.render_size((1080, 1920), "1280x720") == (720, 1280)      # portrait TV
assert display.render_size((800, 480), "1280x720") == (800, 480)         # small: as is
assert display.render_size((1024, 600), "1280x720") == (1024, 600)

# --- reading the kernel's connector list
with tempfile.TemporaryDirectory() as fake:
    for name, status, modes in (("card1-HDMI-A-1", "connected", "1920x1080\n1280x720\n"),
                                ("card1-Composite-1", "unknown", ""),
                                ("card1-DSI-1", "disconnected", "")):
        os.makedirs(os.path.join(fake, name))
        open(os.path.join(fake, name, "status"), "w").write(status + "\n")
        open(os.path.join(fake, name, "modes"), "w").write(modes)
    real_glob = display.glob.glob
    with mock.patch.object(display.glob, "glob",
                           lambda p: real_glob(p.replace("/sys/class/drm", fake))):
        assert display.signature() == (("card1-HDMI-A-1", "connected", "1920x1080"),)
        assert display.has_screen()
        open(os.path.join(fake, "card1-HDMI-A-1", "status"), "w").write("disconnected\n")
        assert display.signature() == () and not display.has_screen()

# --- the booth restarts for a new screen, but never mid-recording
tmp = tempfile.mkdtemp(prefix="booth-display-")
cfg = config.load(overrides=dict(config.dev_overrides(tmp), camera="test", log_file=False,
                                 max_video_size="640x480", web_port=0, countdown_from=1,
                                 saving_min_seconds=0.5, black_seconds=0.2))
os.makedirs(cfg["ssd_mount"])
pygame.display.init()
pygame.font.init()
app = App(cfg)
app.storage.start()
threading.Thread(target=app._prepare_camera, daemon=True).start()


def run(until, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end and app.running:
        app.step(time.monotonic())
        time.sleep(1 / 30)
        if until():
            return
    if not until():
        raise TimeoutError(app.state)


run(lambda: app.state == "idle" and app.camera.ok)
app.button.key(True)
app.step(time.monotonic())
app.button.key(False)
run(lambda: app.state == "recording")
app.events.put(("display_changed", (("card1-HDMI-A-1", "connected", "1280x720"),)))
end = time.monotonic() + 2
run(lambda: time.monotonic() > end)
assert app.running and app.state == "recording", "must not restart while recording"
app.button.key(True)
app.step(time.monotonic())
app.button.key(False)
run(lambda: not app.running, timeout=60)        # finished saving -> restarts
assert app.exit_code == 3
assert app.storage.manifest.names(), "the recording was kept"
app.camera.stop()
pygame.quit()
shutil.rmtree(tmp, ignore_errors=True)
print("display checks OK")
