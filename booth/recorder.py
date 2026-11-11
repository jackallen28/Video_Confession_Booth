"""One recording = one ffmpeg process.

The camera's JPEG frames are handed over on stdin, the microphone is read
directly from ALSA, and ffmpeg's tee output writes the same file to the SSD
and the microSD card at the same time. If one drive fails mid-recording the
other copy carries on. Files are written as name.mkv.part and renamed when
finished; Matroska stays playable even if the power is cut.

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

log = logging.getLogger("booth.recorder")


class Recorder:
    def __init__(self, cfg, camera, log_dir):
        self.cfg = cfg
        self.camera = camera
        self.log_dir = log_dir
        self.proc = None
        self.name = None
        self.targets = {}           # drive -> final path
        self.started = None         # wall clock when ffmpeg started
        self.action_at = None       # monotonic time of ACTION
        self.frames = 0
        self.dropped = 0
        self.error = None
        self._q = queue.Queue(maxsize=90)
        self._writer = None
        self._errlog_path = None

    # ------------------------------------------------------------------
    def start(self, name, targets, mic):
        """targets: {"ssd": "/path/name.mkv", "sd": "..."} (at least one)."""
        self.name, self.targets = name, dict(targets)
        self.frames = self.dropped = 0
        self.error = None
        self.action_at = None
        self._q = queue.Queue(maxsize=90)
        self._errlog_path = os.path.join(self.log_dir, "last-recording-ffmpeg.log")
        self.started = time.time()
        cmd = self._command(mic)
        log.info("recording %s -> %s", name, ", ".join(self.targets.values()))
        log.debug("cmd: %s", " ".join(cmd))
        errlog = open(self._errlog_path, "wb")
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=errlog, bufsize=0)
        except OSError as e:
            self.error = str(e)
            errlog.close()
            return False
        errlog.close()
        self._writer = threading.Thread(target=self._write_loop, name="rec-writer", daemon=True)
        self._writer.start()
        self.camera.add_sink(self._on_frame)
        return True

    def _command(self, mic):
        cfg = self.cfg
        # -copyts keeps the wall-clock timestamps of both inputs (so they line
        # up); -itsoffset moves them back so the file starts near zero.
        t0 = self.started
        cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-copyts",
               "-itsoffset", f"{float(cfg['av_offset']) - t0:.3f}",
               "-thread_queue_size", "512", "-use_wallclock_as_timestamps", "1",
               # (the raw MJPEG demuxer keeps wall-clock stamps at full precision;
               # -framerate only labels the stream)
               "-framerate", str(cfg["framerate"]), "-probesize", "200000", "-analyzeduration", "0",
               "-f", "mjpeg", "-i", "pipe:0"]
        if mic:
            cmd += ["-itsoffset", f"{-t0:.3f}"]
        maps = ["-map", "0:v"]
        if mic == "test":
            cmd += ["-re", "-use_wallclock_as_timestamps", "1", "-f", "lavfi",
                    "-i", f"sine=frequency=440:sample_rate={cfg['audio_rate']}"]
        elif mic:
            cmd += ["-thread_queue_size", "4096", "-f", "alsa",
                    "-sample_rate", str(cfg["audio_rate"]),
                    "-channels", str(cfg["audio_channels"]), "-i", mic]
        if mic:
            maps += ["-map", "1:a"]
        cmd += maps + ["-c:v", "copy"]
        if mic:
            cmd += ["-c:a", "pcm_s16le"]
        cmd += ["-metadata", f"title={self.name}"]
        slaves = "|".join(f"[f=matroska:onfail=ignore]{p}.part" for p in self.targets.values())
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

    def _finish_process(self, timeout=20):
        self.camera.remove_sink(self._on_frame)
        try:
            self._q.put(None, timeout=2)
        except queue.Full:
            # Writer is stuck; drop what's queued so it can see the end.
            while not self._q.empty():
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            self._q.put(None)
        if self._writer:
            self._writer.join(timeout=5)
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
        return {"name": self.name, "targets": self.targets, "duration": duration,
                "frames": self.frames, "dropped": self.dropped,
                "exit": self.proc.returncode, "error": self.error,
                "ffmpeg_log": self.ffmpeg_log()}

    def abort(self):
        """Stop and delete (used if the camera disappears before ACTION)."""
        self._finish_process(timeout=5)
        for p in self.targets.values():
            try:
                os.remove(p + ".part")
            except OSError:
                pass

    def ffmpeg_log(self):
        try:
            with open(self._errlog_path, "rb") as f:
                return f.read()[-4000:].decode("utf-8", "replace")
        except OSError:
            return ""
