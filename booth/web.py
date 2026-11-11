"""The booth's web page: http://<booth address>/ (10.42.0.1 on the booth Wi-Fi).

Runs inside the booth program (its own threads), so it sees the live state,
the camera frames and the recordings. Everything the page needs is served from
here; it works with no internet.

  /                      the page (booth/web/index.html)
  /api/status            state, drives, camera, clock
  /api/settings          editable settings (GET list, POST changes)
  /api/camera            picture controls + mic gain (GET), /api/camera/control, /api/mic (POST)
  /api/videos            recordings;  /video/<name>  (?dl=1 to download), /thumb/<name>
  /all-videos.zip        every video in one download
  /preview.mjpg          live camera;  /snapshot.jpg
  /api/action            record, restart, shutdown, clock, camera_reset (POST)
  /api/log               the end of the booth's log
"""

import base64
import datetime
import hmac
import http.server
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import zipfile

from . import config, devices, media, settings

log = logging.getLogger("booth.web")

HERE = os.path.dirname(os.path.abspath(__file__))
NAME_OK = re.compile(r"^[\w\-. ]+\.mp4$")
CHUNK = 256 * 1024

# Standard JPEG Huffman tables. Many webcams leave them out of their MJPEG
# frames (decoders are expected to know them); browsers need them.
STD_DHT = base64.b64decode(
    "/8QBogAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoLAQADAQEBAQEBAQEBAAAAAAAAAQIDBAUGBwgJCgsQAAIBAwMC"
    "BAMFBQQEAAABfQECAwAEEQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNE"
    "RUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TF"
    "xsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+hEAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMi"
    "MoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3"
    "eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP0"
    "9fb3+Pn6")


def browser_jpeg(data):
    """Adds the standard Huffman tables if the frame has none."""
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return data
        marker = data[i + 1]
        if marker == 0xC4:
            return data
        if marker == 0xDA:
            return data[:i] + STD_DHT + data[i:]
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return data


class Web:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.thumb_dir = os.path.join(self.cfg["state_dir"], "thumbs")
        self._thumb_lock = threading.Lock()
        self.streams = 0
        self.server = None

    def start(self):
        port = int(self.cfg.get("web_port") or 0)
        if port <= 0:
            return
        handler = type("Handler", (Handler,), {"web": self})
        try:
            self.server = http.server.ThreadingHTTPServer(("", port), handler)
        except OSError as e:
            log.warning("web page not started on port %s: %s", port, e)
            return
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, name="web", daemon=True).start()
        log.info("web page on port %s", port)

    # ------------------------------------------------------------- data
    def status(self):
        app = self.app
        snap = app.storage.snapshot()
        rec = app.recorder
        cam = app.camera
        return {
            "state": app.state,
            "recording_seconds": round(rec.elapsed(), 1) if rec and app.state == "recording" else 0,
            "camera": {"name": devices.camera_name(app.device) if app.device else None,
                       "ok": bool(cam and cam.ok), "mode": app.mode},
            "mic": app.mic,
            "drives": {k: {"present": v["present"], "level": v["level"], "free": v["free"],
                           "free_text": media.human_bytes(v["free"]) if v["present"] else "—",
                           "problems": list(v["errors"].values()), "low": v.get("low")}
                       for k, v in (("ssd", snap["ssd"]), ("sd", snap["sd"]))},
            "background_jobs": snap["pending"],
            "damaged": snap["damaged"],
            "undervolt": snap["undervolt"],
            "videos": len(app.storage.manifest.names()),
            "clock": time.time(),
            "clock_text": datetime.datetime.now().strftime("%a %d %b %Y %H:%M:%S"),
            "restart_needed": app.restart_needed,
            "wifi": self._wifi(),
        }

    def _wifi(self):
        try:
            with open("/etc/confession-booth/wifi-ap.conf") as f:
                m = re.search(r"^NAME=(.*)$", f.read(), re.M)
            return {"name": m.group(1).strip("'\"") if m else None}
        except OSError:
            return None

    def settings_list(self):
        out = []
        for f in settings.FIELDS:
            item = dict(f, value=self.cfg.get(f["key"]))
            if f["type"] == "resolution":
                modes = devices.list_modes(self.app.device) if self.app.device else []
                fmt = "mjpeg" if any(m["format"] == "mjpeg" for m in modes) else "yuyv422"
                capw, caph = config.size_tuple(self.cfg["max_video_size"])
                item["options"] = ["max"] + [f"{m['w']}x{m['h']}" for m in modes
                                             if m["format"] == fmt and m["w"] <= capw
                                             and m["h"] <= caph]
            if f["type"] == "strftime":
                now = datetime.datetime.now()
                item["examples"] = {o: now.strftime(o) for o in f["options"]}
            out.append(item)
        return out

    def save_settings(self, changes):
        path = self.cfg["settings_file"]
        saved = config.load_saved_settings(path)
        applied, errors = {}, {}
        for k, v in changes.items():
            try:
                v = settings.validate(k, v)
            except (ValueError, TypeError) as e:
                errors[k] = str(e)
                continue
            saved[k] = v
            old = self.cfg.get(k)
            self.cfg[k] = v
            applied[k] = v
            how = settings.BY_KEY[k]["apply"]
            if old != v and how == "restart":
                self.app.restart_needed = True
            elif old != v and how == "camera":
                self.app.events.put(("camera_restart", None))
        if applied:
            self._write(path, saved)
        return applied, errors

    def save_control(self, name, value):
        path = self.cfg["settings_file"]
        saved = config.load_saved_settings(path)
        controls = dict(saved.get("v4l2_controls", {}))
        if value is None:
            controls = {}
        else:
            controls[name] = value
        saved["v4l2_controls"] = controls
        self.cfg["v4l2_controls"] = controls
        self._write(path, saved)

    @staticmethod
    def _write(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        media.write_atomic(path, json.dumps(data, indent=1, sort_keys=True) + "\n")

    def videos(self):
        st = self.app.storage
        out = []
        for name in st.manifest.names():
            e = st.manifest.entry(name) or {}
            path = st.video_path(name)
            if not path:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            copies = e.get("copies", {})
            dur = max([c.get("duration") or 0 for c in copies.values()] + [0]) or e.get("seconds")
            out.append({"name": name, "size": size, "size_text": media.human_bytes(size),
                        "seconds": dur, "state": e.get("state"),
                        "created": e.get("created"),
                        "on": sorted(d.name for d in st.drives
                                     if d.present and os.path.exists(d.path(name)))})
        out.sort(key=lambda v: v["name"], reverse=True)
        return out

    def thumbnail(self, name, path):
        os.makedirs(self.thumb_dir, exist_ok=True)
        thumb = os.path.join(self.thumb_dir, name + ".jpg")
        if os.path.exists(thumb) and os.path.getmtime(thumb) >= os.path.getmtime(path):
            return thumb
        with self._thumb_lock:      # one at a time, low priority
            dur = media.probe(path).get("duration") or 0
            at = min(5.0, dur / 2) if dur else 0
            r = subprocess.run(media.low_priority() + [
                "ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{at:.1f}", "-i", path,
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", thumb],
                capture_output=True, timeout=60)
        return thumb if r.returncode == 0 and os.path.exists(thumb) else None

    def log_tail(self, lines=200):
        path = os.path.join(self.cfg["state_dir"], "booth.log")
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 60000))
                return f.read().decode(errors="replace").splitlines()[-lines:]
        except OSError:
            return ["(no log file)"]


class Handler(http.server.BaseHTTPRequestHandler):
    web = None                       # set by Web.start
    protocol_version = "HTTP/1.1"
    server_version = "ConfessionBooth"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---------------------------------------------------------- helpers
    def _authorised(self):
        pw = self.web.cfg.get("web_password") or ""
        if not pw:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, given = base64.b64decode(h[6:]).decode().partition(":")
                if hmac.compare_digest(given, pw):
                    return True
            except ValueError:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Confession Booth"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100000:
            raise ValueError("too big")
        return json.loads(self.rfile.read(n) or b"{}")

    def _not_found(self):
        self._json({"error": "not found"}, 404)

    def _video_name(self, raw):
        name = urllib.parse.unquote(raw)
        if not NAME_OK.match(name) or name not in self.web.app.storage.manifest.names():
            return None, None
        path = self.web.app.storage.video_path(name)
        return (name, path) if path else (None, None)

    # ------------------------------------------------------------- GET
    def do_GET(self):
        if not self._authorised():
            return
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        p = url.path
        web = self.web
        try:
            if p in ("/", "/index.html"):
                with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif p == "/api/status":
                self._json(web.status())
            elif p == "/api/settings":
                self._json({"fields": web.settings_list(), "restart_needed": web.app.restart_needed})
            elif p == "/api/camera":
                dev = web.app.device
                controls = [c for c in devices.list_controls(dev) if c["type"] != "button"] \
                    if dev else []
                card = devices.mic_card(web.app.mic)
                self._json({"controls": controls, "mic": {"card": card,
                                                          "levels": devices.mic_levels(card)}})
            elif p == "/api/videos":
                self._json({"videos": web.videos()})
            elif p.startswith("/video/"):
                name, path = self._video_name(p[len("/video/"):])
                if not path:
                    return self._not_found()
                self._send_file(path, "video/mp4", name if "dl" in q else None)
            elif p.startswith("/thumb/"):
                name, path = self._video_name(p[len("/thumb/"):])
                thumb = web.thumbnail(name, path) if path else None
                if not thumb:
                    return self._not_found()
                self._send_file(thumb, "image/jpeg", None, cache=True)
            elif p == "/all-videos.zip":
                self._send_zip()
            elif p == "/snapshot.jpg":
                cam = web.app.camera
                jpg = cam.latest_jpeg if cam else None
                if jpg is None:
                    return self._not_found()
                data = browser_jpeg(jpg)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif p == "/preview.mjpg":
                self._send_preview(float((q.get("fps") or ["4"])[0]))
            elif p == "/api/log":
                self._json({"lines": web.log_tail()})
            else:
                self._not_found()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _send_file(self, path, ctype, download_name, cache=False):
        size = os.path.getsize(path)
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:                                     # last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-cache")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                b = f.read(min(CHUNK, left))
                if not b:
                    break
                self.wfile.write(b)
                left -= len(b)

    def _send_zip(self):
        vids = self.web.videos()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f'attachment; filename="confession-booth-{time.strftime("%Y-%m-%d")}.zip"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED) as z:
            for v in vids:
                path = self.web.app.storage.video_path(v["name"])
                if not path:
                    continue
                info = zipfile.ZipInfo(v["name"], time.localtime(os.path.getmtime(path))[:6])
                with open(path, "rb") as src, z.open(info, "w", force_zip64=True) as dst:
                    shutil.copyfileobj(src, dst, CHUNK)

    def _send_preview(self, fps):
        web = self.web
        if web.streams >= 3:
            return self._json({"error": "too many viewers"}, 503)
        fps = max(0.5, min(10.0, fps))
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        web.streams += 1
        last = None
        try:
            end = time.monotonic() + 1800          # the page reconnects after 30 min
            while time.monotonic() < end and web.app.running:
                cam = web.app.camera
                jpg = cam.latest_jpeg if cam else None
                if jpg is not None and jpg is not last:
                    last = jpg
                    data = browser_jpeg(jpg)
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d"
                                     b"\r\n\r\n" % len(data) + data + b"\r\n")
                    self.wfile.flush()
                time.sleep(1.0 / fps)
        finally:
            web.streams -= 1

    def do_HEAD(self):
        if not self._authorised():
            return
        url = urllib.parse.urlsplit(self.path)
        if url.path.startswith("/video/"):
            name, path = self._video_name(url.path[len("/video/"):])
            if path:
                return self._send_file(path, "video/mp4", None)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._authorised():
            return
        web, app = self.web, self.web.app
        p = urllib.parse.urlsplit(self.path).path
        try:
            body = self._body()
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        try:
            if p == "/api/settings":
                applied, errors = web.save_settings(body)
                self._json({"applied": applied, "errors": errors,
                            "restart_needed": app.restart_needed})
            elif p == "/api/camera/control":
                name, value = str(body.get("name")), int(body.get("value"))
                if not re.match(r"^\w+$", name) or not app.device:
                    return self._json({"error": "bad control"}, 400)
                ok, msg = devices.set_control(app.device, name, value)
                web.save_control(name, value)
                self._json({"ok": ok, "message": msg})
            elif p == "/api/mic":
                card = devices.mic_card(app.mic)
                if not card:
                    return self._json({"error": "no microphone"}, 400)
                ok, msg = devices.set_mic_level(card, str(body.get("name")),
                                                max(0, min(100, int(body.get("percent")))))
                self._json({"ok": ok, "message": msg})
            elif p == "/api/action":
                self._json(self._action(body))
            else:
                self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, TypeError) as e:
            self._json({"error": str(e)}, 400)

    def _action(self, body):
        app = self.web.app
        a = body.get("action")
        if a == "record":                     # same as a press of the big button
            app.events.put(("web_press", None))
            return {"ok": True}
        if a == "restart":
            if app.state not in ("idle", "error", "starting"):
                return {"ok": False, "error": "The booth is busy - try again when it's idle."}
            app.events.put(("restart", None))
            return {"ok": True}
        if a == "shutdown":
            app.events.put(("shutdown", None))
            return {"ok": True}
        if a == "clock":
            t = float(body.get("time"))       # seconds since 1970, from the phone
            if not app.cfg["allow_shutdown"]:     # laptop testing: don't touch its clock
                return {"ok": True, "note": "test mode: clock not changed"}
            r = subprocess.run(["date", "-s", f"@{t:.0f}"], capture_output=True)
            if shutil.which("fake-hwclock"):
                subprocess.run(["fake-hwclock", "save", "force"], capture_output=True)
            log.info("clock set from the web page: %s", time.ctime())
            return {"ok": r.returncode == 0}
        if a == "camera_reset":
            dev = app.device
            for c in devices.list_controls(dev) if dev else []:
                if "default" in c and "inactive" not in c.get("flags", ""):
                    devices.set_control(dev, c["name"], c["default"])
            self.web.save_control(None, None)
            return {"ok": True}
        return {"ok": False, "error": "unknown action"}
