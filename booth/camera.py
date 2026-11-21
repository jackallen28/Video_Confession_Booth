"""The camera runs all the time in one ffmpeg process with two outputs:

  1. the camera's own JPEG frames, untouched (stdout, multipart JPEG) - these
     are what gets recorded, so recording costs almost no CPU;
  2. a small decoded RGB picture the size of the screen (a second pipe) for
     the live preview.

If the camera is unplugged or ffmpeg stops, it is restarted every few seconds.
"""

import logging
import os
import subprocess
import threading
import time

from . import devices, video

log = logging.getLogger("booth.camera")


class Camera:
    def __init__(self, cfg, device, mode, preview_size, log_dir):
        """mode: {"format", "w", "h", "fps"} from video.choose()."""
        self.cfg = cfg
        self.mode = mode
        self.pw, self.ph = preview_size
        self.log_dir = log_dir
        lowres = cfg["preview_lowres"]
        if lowres == "auto":
            # Decode the mirror at the smallest size that is still at least as big as
            # the screen (sharp, not stretched): 720p on a 1024x600 screen = full size.
            big, small = max(mode["w"], mode["h"]), min(mode["w"], mode["h"])
            need_big, need_small = max(preview_size), min(preview_size)
            lowres = next((n for n in (2, 1)
                           if big >> n >= need_big and small >> n >= need_small), 0)
        self.lowres = int(lowres)
        self.device = device
        self._proc = None
        self._lock = threading.Lock()
        self._frame = None          # latest preview frame (bytes, RGB)
        self._frame_seq = 0
        self._last_jpeg = 0.0
        self.latest_jpeg = None     # newest camera frame as sent (for the web page)
        self._sinks = []            # callables getting every JPEG frame
        self._stop = threading.Event()
        self._fails = 0
        self._thread = threading.Thread(target=self._supervise, name="camera", daemon=True)

    # ---------------------------------------------------------------- public
    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._kill()

    def latest(self):
        with self._lock:
            return self._frame_seq, self._frame

    @property
    def ok(self):
        return self._proc is not None and self._proc.poll() is None and \
            self.latest_jpeg is not None and time.monotonic() - self._last_jpeg < 3.0

    def _controls(self):
        """Saved camera settings, plus: keep the full frame rate in dim light (many
        webcams halve it to expose longer, which makes recordings look jerky)."""
        controls = dict(self.cfg["v4l2_controls"])
        if self.cfg.get("steady_framerate", True) and self.device and self.device != "test":
            have = {c["name"] for c in devices.list_controls(self.device)}
            for name in ("exposure_dynamic_framerate", "exposure_auto_priority"):
                if name in have and name not in controls:
                    controls[name] = 0
        if self.device and self.device != "test" and "power_line_frequency" not in controls:
            hz = int(self.cfg.get("mains_hz") or 0)
            if hz in (50, 60) and "power_line_frequency" in {
                    c["name"] for c in devices.list_controls(self.device)}:
                controls["power_line_frequency"] = 1 if hz == 50 else 2    # no flicker
        return controls

    def add_sink(self, fn):
        with self._lock:
            self._sinks = self._sinks + [fn]

    def remove_sink(self, fn):
        with self._lock:
            self._sinks = [s for s in self._sinks if s is not fn]

    # -------------------------------------------------------------- internal
    def _command(self, preview_fd):
        cfg = self.cfg
        w, h, fps, fmt = self.mode["w"], self.mode["h"], self.mode["fps"], self.mode["format"]
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning"]
        if self.device == "test":
            cmd += ["-re", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps:g}"]
            out_a = ["-c:v", "mjpeg", "-q:v", "4"]
        else:
            if self.lowres and fmt == "mjpeg":
                cmd += ["-lowres", str(self.lowres)]
            # Low delay for the mirror on screen: decode one frame at a time (frame
            # threads would hold back a few frames), and keep only a short queue, so
            # if the Pi is busy for a moment it skips frames instead of falling
            # seconds behind.
            cmd += ["-threads", "1", "-fflags", "nobuffer", "-flags", "low_delay",
                    "-f", "v4l2", "-thread_queue_size", "8",
                    "-input_format", fmt, "-video_size", f"{w}x{h}",
                    "-framerate", f"{fps:g}", "-i", self.device]
            out_a = ["-c:v", "copy"] if fmt == "mjpeg" else ["-c:v", "mjpeg", "-q:v", "4"]
        cmd += ["-map", "0:v"] + out_a + ["-f", "mpjpeg", "pipe:1"]
        # Fill the preview area (cropping the edges), upright for portrait. A
        # camera on its side is turned after shrinking, which costs very little.
        turn = [f for f in video.orient_filters(cfg) if f.startswith("transpose")]
        pw, ph = (self.ph, self.pw) if turn else (self.pw, self.ph)
        vf = [f"fps={devices.preview_fps(cfg):g}",
              f"scale={pw}:{ph}:force_original_aspect_ratio=increase:flags=fast_bilinear",
              f"crop={pw}:{ph}"] + turn
        if cfg["mirror_preview"]:
            vf.append("hflip")
        cmd += ["-map", "0:v", "-vf", ",".join(vf), "-pix_fmt", "rgb24",
                "-f", "rawvideo", f"pipe:{preview_fd}"]
        return cmd

    def _supervise(self):
        while not self._stop.is_set():
            if self.device != "test" and not os.path.exists(self.device or ""):
                found = devices.find_camera(self.cfg["camera"])
                if not found:
                    self._stop.wait(2)
                    continue
                self.device = found
            devices.apply_camera_controls(self.device, self._controls())
            started = time.monotonic()
            self._run_once()
            if self._stop.is_set():
                break
            if time.monotonic() - started < 5:
                self._fails += 1
                if self.lowres and self._fails >= 2:
                    log.warning("camera failing to start, trying without preview_lowres")
                    self.lowres = 0
            else:
                self._fails = 0
            self._stop.wait(2)

    def _run_once(self):
        r, w = os.pipe()
        cmd = self._command(w)
        log.info("camera: %s", " ".join(cmd))
        errlog = open(os.path.join(self.log_dir, "camera-ffmpeg.log"), "ab")
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errlog,
                                          stdin=subprocess.DEVNULL, pass_fds=(w,),
                                          bufsize=0)
        except OSError as e:
            log.error("cannot start ffmpeg: %s", e)
            os.close(r)
            os.close(w)
            errlog.close()
            return
        os.close(w)
        self._last_jpeg = time.monotonic()
        pt = threading.Thread(target=self._read_preview, args=(r,), name="preview", daemon=True)
        pt.start()
        try:
            self._read_jpegs(self._proc.stdout)
        finally:
            self._kill()
            pt.join(timeout=2)
            errlog.close()
            log.warning("camera stopped (exit %s)", self._proc.returncode)

    def _read_jpegs(self, raw):
        import io
        stream = io.BufferedReader(raw, buffer_size=1 << 20)
        while not self._stop.is_set():
            line = stream.readline()
            if not line:
                return
            line = line.strip()
            if not line or line.startswith(b"--"):
                continue
            length = None
            while line:
                key, _, val = line.partition(b":")
                if key.strip().lower() == b"content-length":
                    length = int(val.strip())
                line = stream.readline().strip()
            if length is None:
                continue
            data = stream.read(length)
            if len(data) < length:
                return
            now = time.monotonic()           # when this frame arrived from the camera
            self._last_jpeg = now
            self.latest_jpeg = data
            for sink in self._sinks:
                try:
                    sink(data, now)
                except Exception:  # never let a sink kill the camera
                    log.exception("frame sink failed")

    def _read_preview(self, fd):
        size = self.pw * self.ph * 3
        f = os.fdopen(fd, "rb", buffering=0)
        try:
            buf = bytearray(size)
            view = memoryview(buf)
            while True:
                got = 0
                while got < size:
                    n = f.readinto(view[got:])
                    if not n:
                        return
                    got += n
                with self._lock:
                    self._frame = bytes(buf)
                    self._frame_seq += 1
        finally:
            f.close()

    def restart(self):
        """Restart ffmpeg (e.g. mirror setting changed); the supervisor brings it back."""
        self._kill()

    def _kill(self):
        p = self._proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
