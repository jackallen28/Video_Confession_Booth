"""Touch screen as the button: tap to record/stop, hold + tap again to delete.

    python3 tests/test_touch.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import logging  # noqa: E402

import pygame  # noqa: E402

from booth import config  # noqa: E402
from booth.app import App  # noqa: E402

logging.basicConfig(level=logging.WARNING)
tmp = tempfile.mkdtemp(prefix="booth-touch-")
cfg = config.load(overrides=dict(config.dev_overrides(tmp), camera="test", log_file=False,
                                 max_video_size="640x480", web_port=0, countdown_from=1,
                                 saving_min_seconds=1, black_seconds=0.2))
assert cfg["input"] == "touch"            # touch screen is the default
os.makedirs(cfg["ssd_mount"])
pygame.display.init()
pygame.font.init()
app = App(cfg)
app.storage.start()
threading.Thread(target=app._prepare_camera, daemon=True).start()
clock = pygame.time.Clock()


def run(until=None, seconds=None, timeout=60):
    end = time.monotonic() + (seconds if seconds is not None else timeout)
    while time.monotonic() < end:
        app.step(time.monotonic())
        clock.tick(30)
        if until and until():
            return
    if until:
        raise TimeoutError(app.state)


def finger(down, fid=0, also_mouse=False):
    """Like a real touch screen: a finger event, maybe plus a copied mouse click."""
    t = pygame.FINGERDOWN if down else pygame.FINGERUP
    pygame.event.post(pygame.event.Event(t, touch_id=1, finger_id=fid, x=0.5, y=0.5,
                                         dx=0, dy=0, pressure=1))
    if also_mouse:
        m = pygame.MOUSEBUTTONDOWN if down else pygame.MOUSEBUTTONUP
        pygame.event.post(pygame.event.Event(m, button=1, pos=(10, 10), touch=True))


def click(down):
    m = pygame.MOUSEBUTTONDOWN if down else pygame.MOUSEBUTTONUP
    pygame.event.post(pygame.event.Event(m, button=1, pos=(10, 10), touch=False))


def tap(**kw):
    finger(True, **kw)
    run(seconds=0.15)
    finger(False, **kw)
    run(seconds=0.1)


run(lambda: app.state == "idle" and app.camera.ok)
assert app.ui.word("idle_text") == "TAP TO RECORD"

# tap (with the duplicate mouse event some screens send) -> countdown -> recording
tap(also_mouse=True)
run(lambda: app.state == "recording")
run(seconds=1.5)
# two fingers at once still count as one press
finger(True, fid=0)
finger(True, fid=1)
run(seconds=0.1)
finger(False, fid=0)
finger(False, fid=1)
run(lambda: app.state == "review")
kept = app.clip
run(lambda: app.state == "idle")

# a mouse click works too (laptop testing / screens that only send clicks)
click(True)
run(seconds=0.1)
click(False)
run(lambda: app.state == "recording")
run(seconds=1.2)
tap()
run(lambda: app.state == "review")
deleted = app.clip
# hold the screen 6 s -> deleted (no confirm), letting go afterwards does nothing
assert app.ui.word("delete_hint_text") == "HOLD THE SCREEN TO DELETE"
finger(True)
run(lambda: app.state == "deleted", timeout=9)
finger(False)
run(seconds=0.3)
assert app.state == "deleted", app.state
run(lambda: app.state == "idle")
time.sleep(1)

names = app.storage.manifest.names()
assert kept in names and deleted not in names, (kept, deleted, names)
app.running = False
if app.camera:
    app.camera.stop()
pygame.quit()
shutil.rmtree(tmp, ignore_errors=True)
print("touch checks OK")
