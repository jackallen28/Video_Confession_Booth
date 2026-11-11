"""The booth: one screen, one button.

idle --press--> countdown 3-2-1 + clapperboard --> recording --press--> saving --> saved --> idle
Hold the button for 30 s at any time: finish the recording, unmount the SSD, power off.
"""

import logging
import os
import queue
import subprocess
import threading
import time

import pygame

from . import devices
from .button import Button
from .camera import Camera
from .config import size_tuple
from .recorder import Recorder
from .storage import Storage
from .ui import UI

log = logging.getLogger("booth.app")

SAVED_SHOW = 1.6


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log_dir = cfg["state_dir"]
        os.makedirs(self.log_dir, exist_ok=True)

        if cfg["fullscreen"]:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(size_tuple(cfg["window_size"]))
        pygame.display.set_caption("Confession Booth")
        pygame.mouse.set_visible(False)
        W, H = self.screen.get_size()
        log.info("screen %dx%d", W, H)
        self.preview_size = (W, H) if W <= 1024 else (W // 2 // 2 * 2, H // 2 // 2 * 2)

        self.ui = UI(self.screen, cfg)
        self.button = Button(cfg)
        self.storage = Storage(cfg)
        self.camera = Camera(cfg, self.preview_size, self.log_dir)
        self.mic = devices.find_microphone(cfg["audio"])
        self._mic_checked = time.monotonic()
        log.info("microphone: %s", self.mic or "none")

        self.events = queue.Queue()
        self.state = "idle"
        self.state_t = time.monotonic()
        self.recorder = None
        self.saved_ok = True
        self.press_t = None
        self.shutdown_stage = None
        self.running = True
        self._snap = {}
        self._snap_t = 0.0
        self._last_rec_check = 0.0

    # ------------------------------------------------------------------ loop
    def run(self):
        self.storage.start()
        self.camera.start()
        clock = pygame.time.Clock()
        while self.running:
            now = time.monotonic()
            self._handle_input(now)
            self._handle_events()
            self._update(now)
            self._draw(now)
            pygame.display.flip()
            clock.tick(30)
        self.camera.stop()

    def set_state(self, s):
        log.debug("state %s -> %s", self.state, s)
        self.state = s
        self.state_t = time.monotonic()

    # ----------------------------------------------------------------- input
    def _handle_input(self, now):
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                self.running = False
            elif ev.type in (pygame.KEYDOWN, pygame.KEYUP):
                if ev.key in (pygame.K_SPACE, pygame.K_RETURN):
                    if ev.type == pygame.KEYDOWN and not getattr(ev, "repeat", False):
                        self.button.key(True)
                    elif ev.type == pygame.KEYUP:
                        self.button.key(False)
                elif ev.key == pygame.K_ESCAPE and ev.type == pygame.KEYDOWN \
                        and not self.cfg["fullscreen"]:
                    self.running = False

        while True:
            ev = self.button.get()
            if ev is None:
                break
            kind, t = ev
            if kind == "down":
                self.press_t = t
            elif kind == "up" and self.press_t is not None:
                held = t - self.press_t
                self.press_t = None
                if held < self.cfg["hold_feedback_after"]:
                    self.on_press()

        if self.press_t is not None and self.state != "shutdown":
            if self.button._gpio is not None and not self.button.is_pressed \
                    and now - self.press_t > 1.0:
                self.press_t = None          # missed release; forget it
            elif now - self.press_t >= self.cfg["hold_shutdown_seconds"]:
                self.press_t = None
                self.start_shutdown()

    def on_press(self):
        if self.state in ("idle", "saved"):
            self.start_recording()
        elif self.state == "recording":
            if self.recorder.elapsed() >= self.cfg["ignore_stop_for"]:
                self.stop_recording()

    # ------------------------------------------------------------- recording
    def start_recording(self):
        if not self.camera.ok:
            return self._fail("camera not ready")
        targets = self.storage.targets()
        if not targets:
            return self._fail("no drive with free space")
        if self.cfg["audio"] == "auto" and (not self.mic or time.monotonic() - self._mic_checked > 30):
            self.mic = devices.find_microphone("auto")
            self._mic_checked = time.monotonic()
        name = self.storage.new_name()
        rec = Recorder(self.cfg, self.camera, self.log_dir)
        self.storage.active_name = name
        self.storage.recording.set()
        if not rec.start(name, {k: d.path(name) for k, d in targets.items()}, self.mic):
            self.storage.recording.clear()
            return self._fail(rec.error)
        self.recorder = rec
        self.set_state("countdown")

    def _fail(self, why):
        log.error("cannot record: %s", why)
        self.saved_ok = False
        self.set_state("saved")

    def stop_recording(self):
        if self.state not in ("recording", "countdown"):
            return
        self.set_state("saving")
        rec = self.recorder
        threading.Thread(target=self._finish, args=(rec,), name="finish", daemon=True).start()

    def _finish(self, rec):
        ok = False
        try:
            result = rec.stop()
            if result["error"] or result["frames"] == 0:
                log.error("recorder problem: %s\n%s", result["error"], result["ffmpeg_log"])
            ok = self.storage.finalize(result)
        except Exception:
            log.exception("finishing recording failed")
        finally:
            self.storage.recording.clear()
            self.storage.active_name = None
        self.events.put(("saved", ok))

    def _handle_events(self):
        while True:
            try:
                kind, val = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "saved" and self.state == "saving":
                self.saved_ok = val
                self.set_state("saved")

    def _update(self, now):
        el = now - self.state_t
        if self.state == "countdown":
            n = self.cfg["countdown_from"]
            if el >= n + self.ui.CLAP_AT and self.recorder.action_at is None:
                self.recorder.mark_action()
                self.clapper_t0 = self.state_t + n
                self.set_state("recording")
            elif not self.recorder.alive:
                log.error("recorder stopped during countdown:\n%s", self.recorder.ffmpeg_log())
                self.stop_recording()
        elif self.state == "recording":
            if now - self._last_rec_check > 1.0:
                self._last_rec_check = now
                why = None
                if self.recorder.elapsed() >= self.cfg["max_record_seconds"]:
                    why = "time limit"
                elif not self.recorder.alive:
                    why = "recorder stopped"
                elif not self.camera.ok:
                    why = "camera lost"
                elif not self.storage.targets():
                    why = "drives full or failed"
                if why:
                    log.warning("stopping recording: %s", why)
                    self.stop_recording()
        elif self.state == "saved" and el > SAVED_SHOW:
            self.set_state("idle")

    # ------------------------------------------------------------------ draw
    def _draw(self, now):
        ui = self.ui
        if self.state == "shutdown":
            ui.shutdown(self.shutdown_stage, now)
            return
        recording = self.state in ("countdown", "recording")
        if not self.camera.ok and not recording and self.state != "saving":
            ui.no_camera(now)
        else:
            _, frame = self.camera.latest()
            ui.preview(frame, self.preview_size)

        el = now - self.state_t
        pressed = self.press_t is not None
        if self.state == "idle" and self.camera.ok:
            ui.idle(now, pressed)
        elif self.state == "countdown":
            n = self.cfg["countdown_from"]
            if el < n:
                ui.countdown(n - int(el), el % 1.0)
            else:
                ui.clapper(el - n)
        elif self.state == "recording":
            ui.clapper(now - self.clapper_t0)
            ui.recording(self.recorder.elapsed(),
                         self.cfg["max_record_seconds"] - self.recorder.elapsed(), now)
        elif self.state == "saving":
            ui.saving(el)
        elif self.state == "saved":
            ui.saved(self.saved_ok, el)

        if now - self._snap_t > 0.5:
            self._snap = self.storage.snapshot()
            self._snap_t = now
        ui.status_bar(self._snap, bool(self.mic), now)

        if pressed and now - self.press_t >= self.cfg["hold_feedback_after"]:
            span = self.cfg["hold_shutdown_seconds"] - self.cfg["hold_feedback_after"]
            ui.hold((now - self.press_t - self.cfg["hold_feedback_after"]) / span)

    # -------------------------------------------------------------- shutdown
    def start_shutdown(self):
        log.warning("button held: shutting down")
        prev = self.state
        self.set_state("shutdown")
        self.shutdown_stage = "working"
        threading.Thread(target=self._shutdown, args=(prev,), name="shutdown", daemon=True).start()

    def _shutdown(self, prev):
        try:
            if prev in ("countdown", "recording") and self.recorder:
                self._finish(self.recorder)
            end = time.monotonic() + 30
            while self.storage.recording.is_set() and time.monotonic() < end:
                time.sleep(0.2)          # a recording was still being saved
            self.storage.wait_idle(10)
            self.camera.stop()
            ok = self.storage.eject_ssd()
            self.shutdown_stage = "ejected" if ok else "failed"
            log.warning("SSD %s", "safe to remove" if ok else "could not be unmounted")
        except Exception:
            log.exception("shutdown problem")
            self.shutdown_stage = "failed"
        time.sleep(3)
        self.shutdown_stage = "off"
        time.sleep(0.5)
        os.sync()
        if self.cfg["allow_shutdown"]:
            subprocess.run(["systemctl", "poweroff"])
        else:
            self.running = False
