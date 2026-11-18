"""End-to-end run without a Pi: test camera + test tone, SDL dummy display.

Records a clip and keeps it, records a second one and deletes it (hold 3 s,
press again), then holds the button to shut down. Saves a screenshot of every
screen and checks the kept clip is a verified MP4 on both "drives".

    python3 tests/run_headless.py [out_dir] [--size 800x480] [--timestamp] [--set key=value ...]
    e.g. --set orientation=portrait --set screen_rotation=none
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

from booth import config, media  # noqa: E402
from booth.app import App  # noqa: E402

args = [a for a in sys.argv[1:]]
extra = {}
for i, a in enumerate(args):
    if a == "--set":
        k, _, v = args[i + 1].partition("=")
        extra[k] = v
size = args[args.index("--size") + 1] if "--size" in args else "800x480"
out = args[0] if args and not args[0].startswith("--") else "/tmp/booth-test"
data = os.path.join(out, "data")
shutil.rmtree(out, ignore_errors=True)
os.makedirs(out)
logging.basicConfig(level=logging.INFO, format="%(relativeCreated)7.0f %(name)s: %(message)s")

cfg = config.load(overrides=dict(config.dev_overrides(data), camera="test", window_size=size,
                                 hold_shutdown_seconds=8, log_file=False,
                                 timestamp="--timestamp" in args, max_video_size="1280x720",
                                 **extra))
os.makedirs(cfg["ssd_mount"])   # the "SSD" is a plain folder here
pygame.display.init()
pygame.font.init()
app = App(cfg)
app.storage.start()
from booth.app import threading  # noqa: E402
threading.Thread(target=app._prepare_camera, daemon=True).start()
clock = pygame.time.Clock()


def step():
    app.step(time.monotonic())
    pygame.display.flip()
    clock.tick(30)


def run(seconds=None, until=None, timeout=90):
    end = time.monotonic() + (seconds if seconds is not None else timeout)
    while time.monotonic() < end and app.running:
        step()
        if until and until():
            return
    if until:
        raise TimeoutError(f"state is {app.state}")


def shot(name):
    pygame.image.save(app.screen, os.path.join(out, f"{name}.png"))
    if app.screen_turn:          # what the physical (turned) screen shows
        pygame.image.save(app.display, os.path.join(out, f"{name}-display.png"))


def press(hold=0.1):
    app.button.key(True)
    run(hold)
    app.button.key(False)
    step()


def state(s):
    return lambda: app.state == s


run(until=lambda: app.state == "idle" and app.camera.ok)
run(1.0)
shot("01_idle")
print("recording mode:", app.mode)

# ---- clip 1: keep
press()
run(0.4); shot("02_countdown")
run(until=state("recording")); run(0.15); shot("03_recording_starts")
assert not app._status_visible(app._snap), "free-space bar should be hidden"
run(4.0); shot("04_recording")
press()
run(0.12); shot("05_tv_off_squash")
run(0.2); shot("06_tv_off_line")
run(0.25); shot("07_tv_off_dot")
run(1.0); shot("08_black")
run(until=state("review")); run(0.25); shot("09_saving_fade_in")
run(1.5); shot("10_saving")
run(until=state("done")); run(0.3); shot("11_saved")
run(until=state("idle"))
kept = sorted(app.storage.manifest.names())

# ---- clip 2: delete
press()
run(until=state("recording")); run(2.0)
press()
run(until=state("review")); run(0.6)
app.button.key(True)                       # hold 6 s: deleted, no confirm press
run(3.0); shot("12_delete_hold")
run(until=state("deleted"), timeout=10); run(0.3); shot("14_deleted")
app.button.key(False); step()              # letting go afterwards does nothing
run(until=state("idle"))

# ---- wait for the background checks, then hold to shut down
app.storage.wait_idle(60)
run(0.5)
app.button.key(True)
run(4.0); shot("15_hold_shutdown")
run(until=state("shutdown"))
run(until=lambda: app.shutdown_stage == "ejected"); shot("16_ejected")
run(until=lambda: not app.running, timeout=10)

m = json.load(open(os.path.join(data, "state", "manifest.json")))
print(json.dumps(m, indent=1))
recs = m["recordings"]
assert len(recs) == 2, recs
for name, e in recs.items():
    if name in kept:
        assert e["state"] == "safe", e
        drives = ("ssd/Confessions", "sd") if cfg["storage_mode"] == "both" else ("ssd/Confessions",)
        for d in drives:
            p = media.probe(os.path.join(data, d, name))
            assert p["ok"] and p["audio"], p
        if cfg["storage_mode"] == "ssd_first":
            assert not os.path.exists(os.path.join(data, "sd", name)), "SSD only"
    else:
        assert e["state"] == "deleted", e
        for d in ("ssd/Confessions", "sd"):
            left = [f for f in os.listdir(os.path.join(data, d)) if f.startswith(name[:-4])]
            assert not left, left
print(sorted(os.listdir(os.path.join(data, "sd"))))
print("OK, screenshots in", out)
