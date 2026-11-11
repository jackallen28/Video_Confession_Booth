"""End-to-end run without a Pi: test camera + test tone, SDL dummy display.
Presses the "button", records, saves screenshots of every screen, then holds
the button to shut down. Checks both copies were verified.

    python3 tests/run_headless.py [out_dir] [--size 800x480]
"""

import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import logging  # noqa: E402

import pygame  # noqa: E402

from booth import config  # noqa: E402
from booth.app import App  # noqa: E402

out = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "/tmp/booth-test"
size = sys.argv[sys.argv.index("--size") + 1] if "--size" in sys.argv else "800x480"
data = os.path.join(out, "data")
shutil.rmtree(out, ignore_errors=True)
os.makedirs(out)
logging.basicConfig(level=logging.INFO, format="%(relativeCreated)7.0f %(name)s: %(message)s")

cfg = config.load(overrides=dict(config.dev_overrides(data), camera="test",
                                 window_size=size, hold_shutdown_seconds=6,
                                 log_file=False, scrub_interval=5, write_test_interval=1))
os.makedirs(cfg["ssd_mount"])   # the "SSD" is a plain folder here
pygame.display.init()
pygame.font.init()
app = App(cfg)
app.storage.start()
app.camera.start()

shots = {}
plan = [  # (time, action)
    (2.5, "shot:idle"),
    (3.0, "down"), (3.1, "shot:idle_pressed"), (3.2, "up"),
    (3.6, "shot:countdown_3"), (4.85, "shot:countdown_2"), (6.3, "shot:countdown_1"),
    (7.12, "shot:clapper_open"), (7.47, "shot:action"), (8.0, "shot:action_after"),
    (10.5, "shot:recording"), (12.0, "down"), (12.1, "up"), (12.15, "shot:saving"),
    (13.0, "shot:saved"),
    (20.0, "down"), (23.0, "shot:hold"),
    (26.2, "shot:shutdown"),
]
t0 = time.monotonic()
clock = pygame.time.Clock()
i = 0
while app.running and time.monotonic() - t0 < 60:
    now = time.monotonic()
    while i < len(plan) and now - t0 >= plan[i][0]:
        act = plan[i][1]
        if act == "down":
            app.button.key(True)
        elif act == "up":
            app.button.key(False)
        else:
            shots[act[5:]] = True
        i += 1
    app._handle_input(now)
    app._handle_events()
    app._update(now)
    app._draw(now)
    for name in list(shots):
        pygame.image.save(app.screen, os.path.join(out, f"{name}.png"))
        del shots[name]
    if app.state == "shutdown" and app.shutdown_stage == "ejected":
        pygame.image.save(app.screen, os.path.join(out, "ejected.png"))
    pygame.display.flip()
    clock.tick(30)

m = json.load(open(os.path.join(data, "state", "manifest.json")))
print(json.dumps(m, indent=1))
for e in m["recordings"].values():
    assert e["state"] == "safe", e
    for c in e["copies"].values():
        assert c["status"] == "verified", c
sums = open(os.path.join(data, "ssd", "Confessions", "SHA256SUMS")).read()
print(sums)
print("OK, screenshots in", out)
