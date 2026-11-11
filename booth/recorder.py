"""One recording = one ffmpeg process.

The camera's JPEG frames are handed over on stdin and the microphone is read
from ALSA. ffmpeg encodes H.264 (the Pi's hardware encoder) + AAC, adds the
date/time if switched on, and its tee output writes the same stream to the SSD
and the microSD card at the same time. If one drive fails mid-recording the
other copy carries on.

While recording, files are MPEG-TS (name.ts.part): they survive a power cut
and need no header up front. When the recording stops each copy is turned into
a normal .mp4 (no re-encoding) by storage.finalize().

Both inputs are timestamped with the wall clock (-copyts), so sound and
picture stay in sync even when the webcam lowers its frame rate in dim light.
"""

import logging
import os
import queue
import signal
import subprocess
import threading
import time

from . import video

log = logging.getLogger("booth.recorder")


class Recorder:
    def __init__(self, cfg, camera, mode, log_dir):
        self.cfg = cfg
        self.camera = camera
        self.mode = mode              # {"w", "h", "fps", "encoder"}
        self.log_dir = log_dir
        self.proc = None
        self.name = None
        self.parts = {}               # drive -> name.ts.part path
        self.started = None
        self.action_at = None         # monotonic time of ACTION
        self.frames = 0
        self.dropped = 0
        self.error = None
        self._q = queue.Queue(maxsize=60)
        self._writer = None
        self._errlog_path = os.path.join(log_dir, "last-recording-ffmpeg.log")

    # ------------------------------------------------------------------
    def start(self, name, parts, mic):
        """parts: {"ssd": ".../x.ts.part", "sd": "..."} (at least one)."""
        self.name, self.parts = name, dict(parts)
        self.started = time.time()
        cmd = self._command(mic)
        log.info("recording %s -> %s", name, ", ".join(self.parts.values()))
        log.debug("cmd: %s", " ".join(cmd))
        with open(self._errlog_path, "wb") as errlog:
            try:
                self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=errlog, bufsize=0)
            except OSError as e:
                self.error = str(e)
                return False
        self._writer = threading.Thread(target=self._write_loop, name="rec-writer", daemon=True)
        self._writer.start()
        self.camera.add_sink(self._on_frame)
        return True

    def _command(self, mic):
        cfg, m = self.cfg, self.mode
        t0 = self.started
        # -copyts keeps the wall-clock timestamps of both inputs (so they line
        # up); -itsoffset moves them back so the file starts near zero.
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-copyts",
               "-itsoffset", f"{float(cfg['av_offset']) - t0:.3f}",
               "-thread_queue_size", "512", "-use_wallclock_as_timestamps", "1",
               "-framerate", f"{m['fps']:g}", "-probesize", "5000000", "-analyzeduration", "0",
               "-f", "mjpeg", "-i", "pipe:0"]
        if mic:
            cmd += ["-itsoffset", f"{-t0:.3f}"]
        if mic == "test":
            cmd += ["-re", "-use_wallclock_as_timestamps", "1", "-f", "lavfi",
                    "-i", f"sine=frequency=440:sample_rate={cfg['audio_rate']}"]
        elif mic:
            cmd += ["-thread_queue_size", "4096", "-f", "alsa",
                    "-sample_rate", str(cfg["audio_rate"]),
                    "-channels", str(cfg["audio_channels"]), "-i", mic]
        cmd += ["-map", "0:v"] + (["-map", "1:a"] if mic else [])
        cmd += ["-vf", video.video_filter(cfg, m["w"], m["h"], self.log_dir)]
        cmd += video.encoder_args(cfg, m["encoder"], m["w"], m["h"], m["fps"])
        if mic:
            cmd += ["-c:a", "aac", "-b:a", cfg["audio_bitrate"]]
        cmd += ["-metadata", f"title={os.path.splitext(self.name)[0]}"]
        slaves = "|".join(f"[f=mpegts:onfail=ignore]{p}" for p in self.parts.values())
        cmd += ["-f", "tee", slaves]
        return cmd

    def mark_action(self):
        self.action_at = time.monotonic()

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def elapsed(self):
        return 0.0 if self.action_at is None else time.monotonic() - self.action_at

    # ------------------------------------------------------------------
    def _on_frame(self, jpeg):
        try:
            self._q.put_nowait(jpeg)
        except queue.Full:
            self.dropped += 1

    def _write_loop(self):
        pipe = self.proc.stdin
        try:
            while True:
                jpeg = self._q.get()
                if jpeg is None:
                    break
                pipe.write(jpeg)
                self.frames += 1
        except (BrokenPipeError, OSError) as e:
            self.error = self.error or f"recorder pipe: {e}"
        finally:
            try:
                pipe.close()
            except OSError:
                pass

    def _finish_process(self, timeout=30):
        self.camera.remove_sink(self._on_frame)
        while True:
            try:
                self._q.put(None, timeout=2)
                break
            except queue.Full:       # writer stuck: drop queued frames so it sees the end
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass
        if self._writer:
            self._writer.join(timeout=10)
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)   # ffmpeg finishes the files cleanly
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            log.error("ffmpeg did not finish, killing it")
            self.proc.kill()
            self.proc.wait()

    def stop(self):
        """Blocks until ffmpeg has written and closed the files. Returns a dict."""
        duration = self.elapsed()
        self._finish_process()
        log.info("recording %s stopped: %.1fs, %d frames, %d dropped, exit %s",
                 self.name, duration, self.frames, self.dropped, self.proc.returncode)
        return {"name": self.name, "parts": self.parts, "duration": duration,
                "frames": self.frames, "dropped": self.dropped,
                "exit": self.proc.returncode, "error": self.error,
                "ffmpeg_log": self.ffmpeg_log()}

    def ffmpeg_log(self):
        try:
            with open(self._errlog_path, "rb") as f:
                return f.read()[-4000:].decode("utf-8", "replace")
        except OSError:
            return ""
