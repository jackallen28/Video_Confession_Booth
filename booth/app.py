"""The booth: one screen, one button (with a light in it).

idle --press--> 3-2-1 + clapperboard --> recording --press--> TV switch-off,
black --> "VIDEO SAVING" + loading bar (hold 3 s, then press again = delete)
--> "VIDEO SAVED" --> idle.
Hold the button for 30 s at any time: finish up, unmount the SSD, power off.
"""

import json
import logging
import os
import queue
import subprocess
import threading
import time

import pygame

from . import devices, media, video
from .button import Button
from .camera import Camera
from .config import size_tuple
from .led import Led
from .recorder import Recorder
from .storage import Storage
from .ui import UI

log = logging.getLogger("booth.app")

ERROR_SHOW = 1.6
DONE_SHOW = 1.6
DELETED_SHOW = 1.8
FADE = 0.5
REVIEW_STATES = ("stopping", "review", "confirm")


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
        self.led = Led(cfg)
        self.storage = Storage(cfg)
        self.camera = None
        self.device = None
        self.mode = None
        self.mic = devices.find_microphone(cfg["audio"])
        self._mic_checked = time.monotonic()
        log.info("microphone: %s", self.mic or "none")

        self.events = queue.Queue()
        self.state = "starting"
        self.state_t = time.monotonic()
        self.recorder = None
        self.press_t = None
        self.shutdown_stage = None
        self.running = True
        self.exit_code = 0
        self._snap = {}
        self._snap_t = 0.0
        self._last_check = 0.0
        # the recording being reviewed
        self.clip = None             # name
        self.save_done = threading.Event()
        self.save_done.set()         # nothing being saved yet
        self._delete_thread = None
        self.no_camera = False
        self.save_ok = False
        self.review_t0 = 0.0
        self.frozen = None
        self.confirm_t0 = 0.0

    # ------------------------------------------------------------------ loop
    def run(self):
        self.storage.start()
        threading.Thread(target=self._prepare_camera, name="prepare", daemon=True).start()
        clock = pygame.time.Clock()
        while self.running:
            now = time.monotonic()
            self.step(now)
            pygame.display.flip()
            clock.tick(30)
        self.led.off()
        if self.camera:
            self.camera.stop()
        return self.exit_code

    def step(self, now):
        self._handle_input(now)
        self._handle_events()
        self._update(now)
        self._draw(now)
        self._led(now)

    def set_state(self, s):
        log.debug("state %s -> %s", self.state, s)
        self.state = s
        self.state_t = time.monotonic()
        try:
            media.write_atomic(os.path.join(self.log_dir, "status.json"), json.dumps({
                "state": s, "pid": os.getpid(), "since": time.strftime("%H:%M:%S"),
                "camera": devices.camera_name(self.device) if self.device else None,
                "mode": self.mode}))
        except OSError:
            pass

    def _prepare_camera(self):
        """Find the camera and pick the best resolution this Pi can record."""
        while self.running:
            dev = devices.find_camera(self.cfg["camera"])
            if not dev:
                self.events.put(("no_camera", None))
                time.sleep(2)
                continue
            try:
                mode = video.choose(self.cfg, dev, self.log_dir)
            except Exception as e:
                log.error("cannot set up recording: %s", e)
                self.events.put(("no_camera", None))
                time.sleep(20)
                continue
            self.events.put(("camera_ready", (dev, mode)))
            return

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
            held = now - self.press_t
            if self.button._gpio is not None and not self.button.is_pressed and held > 1.0:
                self.press_t = None          # missed release; forget it
            elif held >= self.cfg["hold_shutdown_seconds"]:
                self.press_t = None
                self.start_shutdown()
            elif self.state == "review" and held >= self.cfg["delete_hold_seconds"]:
                self.set_state("confirm")
                self.confirm_t0 = now

    def on_press(self):
        if self.state in ("idle", "error"):
            self.start_recording()
        elif self.state == "recording":
            if self.recorder.elapsed() >= self.cfg["ignore_stop_for"]:
                self.stop_recording()
        elif self.state == "confirm":
            self.delete_clip()

    def _hold_kind(self, now):
        """What a held button is doing right now: None, "delete" or "shutdown"."""
        if self.press_t is None or self.state == "shutdown":
            return None
        held = now - self.press_t
        if self.state == "review":
            return "delete" if held > 0.25 else None
        late = self.cfg["delete_hold_seconds"] + 2 if self.state in REVIEW_STATES else 0
        if held >= max(self.cfg["hold_feedback_after"], late):
            return "shutdown"
        return None

    # ------------------------------------------------------------- recording
    def start_recording(self):
        if not self.camera or not self.camera.ok:
            return self._fail("camera not ready")
        targets = self.storage.targets()
        if not targets:
            return self._fail("no drive with free space")
        if self.cfg["audio"] == "auto" and (not self.mic or time.monotonic() - self._mic_checked > 30):
            self.mic = devices.find_microphone("auto")
            self._mic_checked = time.monotonic()
        name = self.storage.new_name()
        rec = Recorder(self.cfg, self.camera, self.mode, self.log_dir)
        self.storage.active_name = name
        self.storage.recording.set()
        parts = {k: d.path(self.storage.part_name(name)) for k, d in targets.items()}
        if not rec.start(name, parts, self.mic):
            self.storage.recording.clear()
            return self._fail(rec.error)
        self.recorder = rec
        self.set_state("countdown")

    def _fail(self, why):
        log.error("cannot record: %s", why)
        self.set_state("error")

    def stop_recording(self):
        if self.state not in ("recording", "countdown"):
            return
        self.frozen = self._freeze()
        self.clip = self.recorder.name
        self.save_done.clear()
        self.save_ok = False
        self.set_state("stopping")
        threading.Thread(target=self._finish, args=(self.recorder,), name="finish",
                         daemon=True).start()

    def _freeze(self):
        _, frame = self.camera.latest()
        if frame is None:
            return None
        img = pygame.image.frombuffer(frame, self.preview_size, "RGB")
        return pygame.transform.scale(img, self.screen.get_size()).convert()

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
        self.save_ok = ok
        self.save_done.set()

    def delete_clip(self):
        name = self.clip
        log.warning("guest deleted %s", name)
        self.set_state("deleted")

        def work():
            self.save_done.wait(120)
            self.storage.delete(name)
        self._delete_thread = threading.Thread(target=work, name="delete", daemon=True)
        self._delete_thread.start()
        self.clip = None

    def keep_clip(self):
        if self.clip:
            self.storage.accept(self.clip)
            self.clip = None

    # ---------------------------------------------------------------- update
    def _handle_events(self):
        while True:
            try:
                kind, val = self.events.get_nowait()
            except queue.Empty:
                return
            if kind == "no_camera":
                self.no_camera = True
            elif kind == "camera_ready":
                self.device, self.mode = val
                self.camera = Camera(self.cfg, self.device, self.mode, self.preview_size,
                                     self.log_dir)
                self.camera.start()
                self.set_state("idle")

    def _update(self, now):
        cfg = self.cfg
        el = now - self.state_t
        if self.state == "countdown":
            n = cfg["countdown_from"]
            if el >= n + self.ui.CLAP_AT and self.recorder.action_at is None:
                self.recorder.mark_action()
                self.clapper_t0 = self.state_t + n
                self.set_state("recording")
            elif not self.recorder.alive:
                log.error("recorder stopped during countdown:\n%s", self.recorder.ffmpeg_log())
                self.stop_recording()
        elif self.state == "recording":
            if now - self._last_check > 1.0:
                self._last_check = now
                why = None
                if self.recorder.elapsed() >= cfg["max_record_seconds"]:
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
        elif self.state == "stopping":
            if el >= self.ui.TV_OFF + cfg["black_seconds"]:
                self.review_t0 = now
                self.set_state("review")
        elif self.state == "review":
            if self.save_done.is_set() and now - self.review_t0 >= cfg["saving_min_seconds"] \
                    and self.press_t is None:
                self.keep_clip()
                self.set_state("done")
        elif self.state == "confirm":
            if self.press_t is not None:
                self.confirm_t0 = now          # still holding: the timer starts on release
            elif now - self.confirm_t0 > cfg["delete_confirm_seconds"]:
                self.set_state("review")       # not confirmed: back to saving
        elif self.state == "done" and el > DONE_SHOW + FADE:
            self.set_state("idle")
        elif self.state == "deleted" and el > DELETED_SHOW + FADE:
            self.set_state("idle")
        elif self.state == "error" and el > ERROR_SHOW:
            self.set_state("idle")
        elif self.state == "idle" and now - self._last_check > 5:
            self._last_check = now
            dev = devices.find_camera(cfg["camera"])
            if dev and self.device != "test" and dev != self.device:
                log.warning("a different camera was plugged in (%s); restarting", dev)
                self.exit_code = 3
                self.running = False

    # ------------------------------------------------------------------ draw
    def _draw(self, now):
        ui, cfg = self.ui, self.cfg
        st = self.state
        el = now - self.state_t
        if st == "shutdown":
            ui.shutdown(self.shutdown_stage, now)
            return
        if st == "starting":
            ui.no_camera(now) if self.no_camera else ui.starting(now)
        elif st == "stopping":
            ui.tv_off(self.frozen, el)
        elif st == "review":
            self._draw_saving(now, fade_in=now - self.review_t0)
        elif st == "confirm":
            left = 1 - (now - self.confirm_t0) / cfg["delete_confirm_seconds"]
            ui.confirm_delete(cfg["delete_confirm_text"], left, now)
        elif st == "deleted":
            if el < DELETED_SHOW:
                ui.deleted(cfg["deleted_text"])
            else:
                self._draw_camera(now)
                ui.fade(255 * (1 - (el - DELETED_SHOW) / FADE))
        elif st == "done":
            if el < DONE_SHOW:
                self._draw_saving(now, fade_in=99)
            else:
                self._draw_camera(now)
                ui.fade(255 * (1 - (el - DONE_SHOW) / FADE))
        else:
            self._draw_camera(now)
            pressed = self.press_t is not None
            if st == "idle" and self.camera and self.camera.ok:
                ui.idle(now, pressed)
            elif st == "countdown":
                n = cfg["countdown_from"]
                if el < n:
                    ui.countdown(n - int(el), el % 1.0)
                else:
                    ui.clapper(el - n)
            elif st == "recording":
                ui.clapper(now - self.clapper_t0)
                ui.recording(self.recorder.elapsed(),
                             cfg["max_record_seconds"] - self.recorder.elapsed(), now)
            elif st == "error":
                ui.saved(False, el)

        if st not in ("stopping", "review", "confirm", "deleted") and \
                not (st == "done" and el < DONE_SHOW):
            if now - self._snap_t > 0.5:
                self._snap = self.storage.snapshot()
                self._snap_t = now
            ui.status_bar(self._snap, bool(self.mic), now)

        if self._hold_kind(now) == "shutdown":
            span = cfg["hold_shutdown_seconds"] - cfg["hold_feedback_after"]
            ui.hold((now - self.press_t - cfg["hold_feedback_after"]) / span)

    def _draw_camera(self, now):
        if self.camera is None or (not self.camera.ok and self.state not in ("countdown",
                                                                             "recording")):
            self.ui.no_camera(now)
        else:
            _, frame = self.camera.latest()
            self.ui.preview(frame, self.preview_size)

    def _draw_saving(self, now, fade_in):
        cfg = self.cfg
        p = (now - self.review_t0) / cfg["saving_min_seconds"]
        done = self.save_done.is_set()
        progress = min(p, 1.0) if done else min(p, 0.97)
        finished = self.state == "done"
        ok = self.save_ok if finished else None
        title = cfg["saving_text"] if not finished else \
            cfg["saved_text"] if self.save_ok else cfg["failed_text"]
        hold = None
        if self._hold_kind(now) == "delete":
            hold = min(1.0, (now - self.press_t) / cfg["delete_hold_seconds"])
        self.ui.saving_screen(title, 1.0 if finished else progress,
                              None if finished else cfg["delete_hint_text"], hold, ok, now)
        if fade_in < FADE:
            self.ui.fade(255 * (1 - fade_in / FADE))

    # ------------------------------------------------------------------- LED
    def _led(self, now):
        st = self.state
        hold = self._hold_kind(now)
        if st == "shutdown":
            pattern = "off" if self.shutdown_stage == "off" else "confirm"
        elif hold == "shutdown":
            pattern = "shutdown_hold"
        elif hold == "delete":
            pattern = "delete_hold"
        elif st == "idle":
            pattern = "idle" if self.camera and self.camera.ok else "problem"
        elif st == "countdown":
            pattern = "countdown" if now - self.state_t < self.cfg["countdown_from"] else "action"
        elif st == "recording":
            pattern = "recording"
        elif st == "confirm":
            pattern = "confirm"
        elif st == "done":
            pattern = "done" if self.save_ok else "problem"
        elif st in ("error", "starting"):
            pattern = "problem" if st == "error" else "off"
        else:
            pattern = "off"
        self.led.show(pattern, now)

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
                self.clip = self.recorder.name
                self._finish(self.recorder)
            self.save_done.wait(60)          # a recording still being saved
            if self._delete_thread:
                self._delete_thread.join(30)
            self.keep_clip()                 # not deleted -> keep it
            self.storage.wait_idle(10)
            if self.camera:
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
        self.led.off()
        os.sync()
        if self.cfg["allow_shutdown"]:
            subprocess.run(["systemctl", "poweroff"])
        else:
            self.running = False
