"""The web page and its API against a running (headless) booth.

    python3 tests/test_web.py [--keep]     (--keep: leave it running on :8080 to look at)
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import logging  # noqa: E402

import pygame  # noqa: E402

from booth import config, web  # noqa: E402
from booth.app import App  # noqa: E402

PORT = 8080
BASE = f"http://127.0.0.1:{PORT}"
KEEP = "--keep" in sys.argv
logging.basicConfig(level=logging.WARNING)

tmp = tempfile.mkdtemp(prefix="booth-web-")
cfg = config.load(overrides=dict(config.dev_overrides(tmp), camera="test", log_file=True,
                                 max_video_size="640x480", web_port=PORT, countdown_from=1,
                                 saving_min_seconds=1, black_seconds=0.5))
os.makedirs(cfg["ssd_mount"])
pygame.display.init()
pygame.font.init()
app = App(cfg)
failures = []


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, dict(r.headers), r.read()


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def wait(cond, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    raise TimeoutError


def checks():
    try:
        wait(lambda: app.state == "idle" and app.camera and app.camera.ok)
        s, h, body = get("/")
        assert s == 200 and b"Confession Booth" in body and b"http" not in body.split(b"<script>")[0] \
            .replace(b"http-equiv", b""), "page must not load anything from the internet"
        st = json.loads(get("/api/status")[2])
        assert st["state"] == "idle" and st["camera"]["ok"], st

        # settings: live text change, validation, restart flag
        r = post("/api/settings", {"idle_text": "PRESS ME", "countdown_from": 99,
                                   "framerate": "nope"})
        assert r["applied"]["idle_text"] == "PRESS ME" and app.cfg["idle_text"] == "PRESS ME"
        assert r["applied"]["countdown_from"] == 10 and "framerate" in r["errors"], r
        post("/api/settings", {"countdown_from": 1})
        r = post("/api/settings", {"video_size": "640x480"})
        assert r["restart_needed"] is True
        saved = json.load(open(cfg["settings_file"]))
        assert saved["idle_text"] == "PRESS ME" and saved["video_size"] == "640x480"
        fields = json.loads(get("/api/settings")[2])["fields"]
        res = [f for f in fields if f["key"] == "video_size"][0]
        assert "max" in res["options"] and "640x480" in res["options"], res["options"]

        # snapshot + live stream (browser-ready JPEGs)
        s, h, jpg = get("/snapshot.jpg")
        assert h["Content-Type"] == "image/jpeg" and jpg[:2] == b"\xff\xd8" and b"\xff\xc4" in jpg
        with urllib.request.urlopen(BASE + "/preview.mjpg?fps=5", timeout=10) as r:
            assert r.headers["Content-Type"].startswith("multipart/x-mixed-replace")
            chunk = r.read(20000)
            assert chunk.count(b"--frame") >= 1 and b"\xff\xd8" in chunk

        # record from the web page
        post("/api/action", {"action": "record"})
        wait(lambda: app.state == "recording")
        time.sleep(2.5)
        post("/api/action", {"action": "record"})
        wait(lambda: app.state == "idle", 60)
        app.storage.wait_idle(60)
        vids = json.loads(get("/api/videos")[2])["videos"]
        assert len(vids) == 1 and vids[0]["state"] == "safe", vids
        name = vids[0]["name"]
        path = app.storage.video_path(name)

        # full download, range request (needed by iPhone Safari), thumbnail
        s, h, data = get(f"/video/{name}?dl=1")
        assert data == open(path, "rb").read() and "attachment" in h["Content-Disposition"]
        s, h, part = get(f"/video/{name}", {"Range": "bytes=100-199"})
        assert s == 206 and len(part) == 100 and part == data[100:200], (s, h)
        assert h["Content-Range"] == f"bytes 100-199/{len(data)}"
        s, h, thumb = get(f"/thumb/{name}")
        assert thumb[:2] == b"\xff\xd8"
        try:
            get("/video/..%2F..%2Fetc%2Fpasswd.mp4")
            raise AssertionError("path traversal not refused")
        except urllib.error.HTTPError as e:
            assert e.code == 404

        # download all
        s, h, z = get("/all-videos.zip")
        with zipfile.ZipFile(io.BytesIO(z)) as zf:
            assert zf.namelist() == [name] and zf.read(name) == data

        # wipe all videos: refused without DELETE, then everything gone
        try:
            post("/api/videos/wipe", {"confirm": "delete"})
            raise AssertionError("wipe without DELETE was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400
        assert json.loads(get("/api/videos")[2])["videos"], "still there"
        r = post("/api/videos/wipe", {"confirm": "DELETE"})
        assert r["ok"] and r["files"] >= 1, r
        v = json.loads(get("/api/videos")[2])
        assert v["videos"] == [] and v["deleted"] == [] and not os.path.exists(path)

        # log, clock (test mode leaves the computer's clock alone)
        assert json.loads(get("/api/log")[2])["lines"]
        assert post("/api/action", {"action": "clock", "time": time.time()})["ok"]
        # SSD tab: drive info; format needs FORMAT typed; test mode never touches disks
        s = json.loads(get("/api/ssd")[2])
        assert s["drive"]["found"] is False and s["drive"]["test_mode"] and not s["running"], s
        assert "FORMAT" in post("/api/ssd", {"action": "format"})["error"]
        r = post("/api/ssd", {"action": "format", "confirm": "FORMAT"})
        assert not r["ok"] and "only run on the Pi" in r["error"], r
        assert post("/api/ssd", {"action": "clear"})["ok"]
        page = get("/")[2].decode()
        assert 'data-tab="ssd"' in page and 'id="ssdFormat"' in page
        print("web checks OK")
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        failures.append(e)
    finally:
        if not KEEP:
            app.running = False


assert web.browser_jpeg(b"\xff\xd8\xff\xdb\x00\x04ab\xff\xda\x00\x02xyz").count(b"\xff\xc4") == 1
threading.Thread(target=checks, daemon=True).start()
try:
    app.run()
finally:
    pygame.quit()
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if failures else 0)
