"""The booth: one screen, one button (with a light in it).

idle --press--> 3-2-1 (+ clapperboard if switched on) --> recording --press--> TV switch-off,
black --> "VIDEO SAVING" + loading bar (hold 3 s, then press again = delete)
--> "VIDEO SAVED" --> idle.
Hold the button for 30 s at any time: finish up, unmount the SSD, power off.
"""

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time

import pygame

from . import devices, display, media, themes, updater, video
from . import ui as ui_mod
from .button import Button, uses_button, uses_touch
from .camera import Camera
from .highlights import Highlights
from .led import Led, level
from .recorder import Recorder
from .storage import Storage
from .ui import UI
from .web import Web

log = logging.getLogger("booth.app")

ERROR_SHOW = 1.6
DONE_SHOW = 1.6
DELETED_SHOW = 1.8
FADE = 0.5
REVIEW_STATES = ("stopping", "review", "confirm")



def set_cpu_governor(cfg):
    """Pi 5: full processor speed while the booth runs (see cpu_governor)."""
    gov = cfg.get("cpu_governor")
    if not gov or not cfg.get("allow_shutdown"):         # not on a laptop
        return
    import glob as _glob
    for p in _glob.glob("/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"):
        try:
            with open(p, "w") as f:
                f.write(gov)
        except OSError as e:
            log.info("CPU speed setting not changed (%s)", e)
            return
    log.info("CPU governor: %s", gov)

class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.log_dir = cfg["state_dir"]
        os.makedirs(self.log_dir, exist_ok=True)

        self.display = display.open_display(cfg)
        pygame.display.set_caption("Confession Booth")
        pygame.mouse.set_visible(False)
        DW, DH = self.display.get_size()
        portrait = video.portrait(cfg)
        # orientation in use until the next restart (for the web page)
        self.layout = {k: cfg[k] for k in ("orientation", "picture_rotation", "screen_rotation")}
        # Portrait with the screen on its side: draw on an upright canvas and
        # turn it onto the display every frame.
        self.screen_turn = cfg["screen_rotation"] if portrait and \
            cfg["screen_rotation"] in ("right", "left") and DW > DH else None
        self.screen = pygame.Surface((DH, DW)).convert() if self.screen_turn else self.display
        W, H = self.screen.get_size()
        # Where the camera picture goes: everywhere, or a portrait strip in the
        # middle of a landscape screen.
        self.preview_rect = pygame.Rect(0, 0, W, H)
        if portrait and W > H:
            pw = int(H * 9 / 16) // 2 * 2
            self.preview_rect = pygame.Rect((W - pw) // 2, 0, pw, H)
        pw, ph = self.preview_rect.size
        if max(pw, ph) > 1024:
            pw, ph = pw // 4 * 2, ph // 4 * 2
        self.preview_size = (pw // 2 * 2, ph // 2 * 2)
        log.info("screen %dx%d, %s%s", DW, DH, cfg["orientation"],
                 f" (screen turned {self.screen_turn})" if self.screen_turn else "")

        self.ui = UI(self.screen, cfg)
        self.waiting = themes.WaitingScreen(self.screen, cfg)
        # snippets of each recording, played behind the saving bar
        self.highlights = Highlights(devices.preview_fps(cfg))
        self._hl_seq = None
        self.button = Button(cfg)
        # the light lives in the physical button, so only drive it when that's in use
        self.led = Led(cfg if uses_button(cfg) else dict(cfg, led_pin=-1))
        self.touch_on = uses_touch(cfg)
        self.press_source = None
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
        self.restart_needed = False   # a setting changed that needs a restart
        self.display_changed = False  # different screen plugged in: restart when idle
        self.term_requested = False   # asked to stop: finish saving, then exit
        self.camera_restart_wanted = False
        self.status_extra = {}
        self.web = Web(self)
        self.save_ok = False
        self.review_t0 = 0.0
        self.bar_elapsed = 0.0       # saving bar time (stands still while held)
        self.bar_last = 0.0
        self.frozen = None
        self.confirm_t0 = 0.0

    # ------------------------------------------------------------------ loop
    def run(self):
        set_cpu_governor(self.cfg)
        self.storage.start()
        self.web.start()
        if self.cfg["fullscreen"]:
            display.Watcher(lambda sig: self.events.put(("display_changed", sig))).start()
        threading.Thread(target=self._prepare_camera, name="prepare", daemon=True).start()
        # systemctl stop/restart, an update or a shutdown: save the recording first
        signal.signal(signal.SIGTERM, lambda *a: setattr(self, "term_requested", True))
        clock = pygame.time.Clock()
        while self.running:
            now = time.monotonic()
            self.step(now)
            try:
                pygame.display.flip()
            except pygame.error as e:          # screen gone; restart when free
                if not self.display_changed:
                    log.warning("screen problem: %s", e)
                self.display_changed = True
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
        self._present()

    def _present(self):
        if self.screen_turn:
            turned = pygame.transform.rotate(self.screen, -90 if self.screen_turn == "right" else 90)
            self.display.blit(turned, (0, 0))

    def set_state(self, s):
        log.debug("state %s -> %s", self.state, s)
        self.state = s
        self.state_t = time.monotonic()
        self.status_extra = {}
        self._write_status()

    def _write_status(self):
        try:
            media.write_atomic(os.path.join(self.log_dir, "status.json"), json.dumps({
                "state": self.state, "pid": os.getpid(), "since": time.strftime("%H:%M:%S"),
                "camera": devices.camera_name(self.device) if self.device else None,
                "mode": self.mode, **self.status_extra}))
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
            elif not self.touch_on or self.state == "shutdown":
                continue
            # Touch screen: a tap anywhere is a press of the button. Touches
            # arrive as finger events, and on some screens as mouse clicks.
            elif ev.type in (pygame.FINGERDOWN, pygame.FINGERUP):
                self.button.touch(("f", ev.touch_id, ev.finger_id), ev.type == pygame.FINGERDOWN)
            elif ev.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
                if getattr(ev, "touch", False) or ev.button not in (1, 2, 3):
                    continue          # copy of a finger event, or a scroll wheel
                self.button.touch("mouse", ev.type == pygame.MOUSEBUTTONDOWN)

        while True:
            ev = self.button.get()
            if ev is None:
                break
            kind, t, source = ev
            if kind == "down":
                self.press_t = t
                self.press_source = source
            elif kind == "up" and self.press_t is not None:
                held = t - self.press_t
                self.press_t = None
                if held < self.cfg["hold_feedback_after"]:
                    self.on_press()

        if self.press_t is not None and self.state != "shutdown":
            held = now - self.press_t
            if self.press_source == "gpio" and not self.button.is_pressed and held > 1.0:
                self.press_t = None          # missed release; forget it
            elif held >= self.cfg["hold_shutdown_seconds"]:
                self.press_t = None
                self.start_shutdown()
            elif self.state == "review" and held >= self.cfg["delete_hold_seconds"]:
                if self.cfg["delete_confirm"]:
                    self.set_state("confirm")
                    self.confirm_t0 = now
                else:
                    self.press_t = None      # this hold is used up; letting go does nothing
                    self.delete_clip()

    def on_press(self):
        if self.state in ("idle", "error"):
            self.start_recording()
        elif self.state == "recording":
            if self.recorder.elapsed() >= self.cfg["ignore_stop_for"]:
                self.stop_recording()
        elif self.state == "confirm":
            self.delete_clip()

    @property
    def black_idle(self):
        """Physical button in use: wait on a waiting screen (no camera picture),
        and switch the picture on like an old TV."""
        return uses_button(self.cfg)

    def _tvon(self):
        return self.ui.TV_ON if self.black_idle else 0.0

    def _intro(self):
        """-> (countdown seconds, clapperboard?, seconds the open clapperboard is
        held first) from the start_animation setting."""
        mode = self.cfg["start_animation"]
        n = int(self.cfg["countdown_from"]) if mode in ("countdown", "both") else 0
        # On its own, the clapperboard gives guests a moment before it snaps.
        return n, mode in ("clapperboard", "both"), 1.2 if mode == "clapperboard" else 0.0

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
        if self.storage.wiping:
            return self._fail("wiping all videos")
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
        self.highlights.finish()
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
        self.ui.preview(frame, self.preview_size, self.preview_rect)
        return self.screen.copy()

    def _finish(self, rec):
        ok = False
        try:
            result = rec.stop()
            if result["error"] or result["frames"] == 0:
                log.error("recorder problem: %s\n%s", result["error"], result["ffmpeg_log"])
            elif result["dropped"] > max(3, result["frames"] // 100) or (
                    result["seconds"] >= 8 and self.mode
                    and result["fps_in"] < 0.85 * float(self.mode["fps"])):
                # The Pi fell behind (frames dropped, or the camera's frames not read in
                # time because the processor is busy encoding): from the next clip on,
                # record with a faster setting. (A camera slowing down in dim light
                # looks the same; a faster setting is harmless then, files just get bigger.)
                new = video.slow_down(self.log_dir, self.mode or {})
                if new:
                    self.mode = new
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

    def begin_update(self):
        """Software update from the web page: only when idle. Stop taking
        recordings, let background checks/copies finish, save everything,
        then start the updater (its own process)."""
        if self.state not in ("idle", "error") or self.press_t is not None:
            return False
        self.set_state("updating")

        def prepare():
            self.storage.paused.set()             # no new background jobs
            end = time.monotonic() + 600
            while self.storage.busy and time.monotonic() < end:
                time.sleep(0.5)                   # let the current copy/check finish
            try:
                self.storage.manifest.save()
            except OSError:
                pass
            os.sync()
            self.status_extra = {"update_ready": True}
            self._write_status()
            try:
                updater.launch(self.cfg)
            except Exception as e:                # noqa: BLE001
                self.events.put(("update_failed", str(e)))
        threading.Thread(target=prepare, name="update", daemon=True).start()
        return True

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
            if kind == "web_press":            # "record/stop" on the web page
                if self.state in ("idle", "error", "recording", "confirm"):
                    self.on_press()
            elif kind == "restart":
                log.warning("restart requested from the web page")
                self.exit_code = 3
                self.running = False
            elif kind == "shutdown":
                if self.state != "shutdown":
                    self.start_shutdown()
            elif kind == "display_changed":
                self.display_changed = True
            elif kind == "camera_restart":
                self.camera_restart_wanted = True      # done when idle (see _update)
            elif kind == "begin_update":
                self.begin_update()
            elif kind == "update_failed":
                log.error("update didn't start: %s", val)
                self.storage.paused.clear()
                if self.state == "updating":
                    self.set_state("idle")
            elif kind == "no_camera":
                self.no_camera = True
            elif kind == "camera_ready":
                self.device, self.mode = val
                self.camera = Camera(self.cfg, self.device, self.mode, self.preview_size,
                                     self.log_dir)
                self.camera.start()
                self.set_state("idle")

    def _update(self, now):
        cfg = self.cfg
        el = max(0.0, now - self.state_t)   # state may have changed after `now`
        if self.state == "countdown":
            n, clapper, lead = self._intro()
            on = self._tvon()
            clap = self.ui.CLAP_AT + lead if clapper else 0.0
            if el >= on + n + clap and self.recorder.action_at is None:
                self.recorder.mark_action()
                self.clapper_t0 = self.state_t + on + n
                self.highlights.start()
                self._hl_seq = None
                self.set_state("recording")
            elif not self.recorder.alive:
                log.error("recorder stopped during countdown:\n%s", self.recorder.ffmpeg_log())
                self.stop_recording()
        elif self.state == "recording":
            seq, frame = self.camera.latest()
            if seq != self._hl_seq:
                self._hl_seq = seq
                self.highlights.add(frame, self.preview_size, self.recorder.elapsed(),
                                    mirrored=bool(cfg["mirror_preview"]))
            if now - self._last_check > 1.0:
                self._last_check = now
                why = None
                if self.recorder.elapsed() >= cfg["max_record_seconds"]:
                    why = "time limit"
                elif not self.recorder.alive:
                    why = "recorder stopped"
                elif not self.camera.ok:
                    why = "camera lost"
                elif not self.storage.has_room(self.recorder.parts):
                    why = "drive full or failed"
                if why:
                    log.warning("stopping recording: %s", why)
                    self.stop_recording()
        elif self.state == "stopping":
            if el >= self.ui.TV_OFF + cfg["black_seconds"]:
                self.review_t0 = now
                self.bar_elapsed, self.bar_last = 0.0, now
                self.set_state("review")
        elif self.state == "review":
            # the saving bar's own clock: it stands still while the button is held
            if self._hold_kind(now) != "delete":
                self.bar_elapsed += max(0.0, now - self.bar_last)
            self.bar_last = now
            if self.save_done.is_set() and self.bar_elapsed >= cfg["saving_min_seconds"] \
                    and self.press_t is None:
                self.keep_clip()
                self.set_state("done")
        elif self.state == "confirm":
            self.bar_last = now                # the bar doesn't move while confirming
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
        if self.term_requested:
            if self.state in ("countdown", "recording"):
                log.warning("asked to stop: saving the recording first")
                self.stop_recording()
            elif self.save_done.is_set() and self.state not in ("stopping",) and self.running:
                log.warning("stopping (recordings saved)")
                self.running = False
        if self.state == "updating" and el > 45 * 60:
            # the updater should have restarted the booth long ago: carry on as before
            log.error("update didn't finish; resuming")
            try:
                media.write_atomic(os.path.join(self.log_dir, "update.json"), json.dumps({
                    "state": "failed", "time": time.time(),
                    "message": "Update failed: it didn't finish. Try again.", "steps": []}))
            except OSError:
                pass
            self.storage.paused.clear()
            self.set_state("idle")
        if self.camera_restart_wanted and self.state == "idle" and self.camera:
            self.camera_restart_wanted = False
            self.camera.restart()
        if self.display_changed and self.state in ("idle", "error", "starting") \
                and self.press_t is None and self.running:
            log.warning("restarting to fit the screen that's connected now")
            self.exit_code = 3
            self.running = False
        if self.state == "idle" and now - self._last_check > 5:
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
        el = max(0.0, now - self.state_t)   # state may have changed after `now`
        if st == "shutdown":
            ui.shutdown(self.shutdown_stage, now)
            return
        if st == "starting":
            ui.no_camera(now) if self.no_camera else ui.starting(now)
        elif st == "updating":
            ui.updating(now)
        elif st == "stopping":
            ui.tv_off(self.frozen, el)
        elif st == "review":
            self._draw_saving(now, fade_in=now - self.review_t0)
        elif st == "confirm":
            left = 1 - (now - self.confirm_t0) / cfg["delete_confirm_seconds"]
            ui.confirm_delete(ui.word("delete_confirm_text"), left, now)
        elif st == "deleted":
            if el < DELETED_SHOW:
                ui.deleted(cfg["deleted_text"])
            else:
                self._draw_waiting(now)
                ui.fade(255 * (1 - (el - DELETED_SHOW) / FADE))
        elif st == "done":
            if el < DONE_SHOW:
                self._draw_saving(now, fade_in=99)
            else:
                self._draw_waiting(now)
                ui.fade(255 * (1 - (el - DONE_SHOW) / FADE))
        elif st == "idle" and self.black_idle and self.camera and self.camera.ok:
            self._draw_waiting(now)
        elif st == "countdown" and el < self._tvon():
            self._draw_camera(now)
            ui.tv_on(self.screen.copy(), el)
        else:
            self._draw_camera(now)
            pressed = self.press_t is not None
            if st == "idle" and self.camera and self.camera.ok:
                ui.idle(now, pressed)
            elif st == "countdown":
                n, clapper, lead = self._intro()
                el -= self._tvon()
                if el < n:
                    ui.countdown(n - int(el), el % 1.0)
                elif clapper:
                    ui.clapper(el - n, lead)
            elif st == "recording":
                _, clapper, lead = self._intro()
                if clapper:
                    ui.clapper(now - self.clapper_t0, lead)
                ui.recording(self.recorder.elapsed(),
                             cfg["max_record_seconds"] - self.recorder.elapsed(), now)
            elif st == "error":
                ui.saved(False, el)

        if now - self._snap_t > 0.5:
            self._snap = self.storage.snapshot()
            self._snap_t = now
        if st not in ("stopping", "review", "confirm", "deleted") and \
                not (st == "done" and el < DONE_SHOW) and self._status_visible(self._snap):
            ui.status_bar(self._snap, bool(self.mic), now)

        if self._hold_kind(now) == "shutdown":
            span = cfg["hold_shutdown_seconds"] - cfg["hold_feedback_after"]
            ui.hold((now - self.press_t - cfg["hold_feedback_after"]) / span,
                    self._drive_rows())

    def _drive_rows(self):
        """For the power-off hold: each drive's status and the space left."""
        if getattr(self, "_rows_for", None) != self.press_t:     # once per hold
            self._rows_for = self.press_t
            try:
                rate = self.storage.placement()["mb_per_minute"]
            except Exception:  # noqa: BLE001 - only a nicety
                rate = None
            snap = self.storage.snapshot()
            rows = []
            for kind, name in (("ssd", "SSD"), ("sd", "microSD")):
                d = snap[kind]
                if not d["present"]:
                    rows.append((kind, name, "not connected", ui_mod.GREY, ""))
                    continue
                if d["errors"]:
                    status, colour = "problem", (255, 72, 72)
                    if "last_save" in d["errors"]:
                        status = "last video didn't save"
                elif d.get("low"):
                    status, colour = "nearly full", ui_mod.AMBER
                else:
                    status, colour = "OK", ui_mod.GREEN
                free = max(0, (d["free"] or 0) - self.storage.min_free)
                detail = media.human_bytes(free) + " free"
                if rate:
                    hours = free / (rate * 1e6) / 60
                    detail += f" · {hours:.0f} h" if hours >= 10 else f" · {hours:.1f} h"
                rows.append((kind, name, status, colour, detail))
            self._rows = rows
        return self._rows

    def _status_visible(self, snap):
        """The free-space bar only comes up when it matters (see status_bar setting)."""
        mode = self.cfg["status_bar"]
        if mode == "always":
            return True
        drives = [snap.get("ssd", {}), snap.get("sd", {})]
        if any(d.get("low") for d in drives):
            return True
        if mode == "problems":
            return bool(snap.get("damaged") or snap.get("undervolt") or not self.mic
                        or any(d.get("level") in ("error", "missing") for d in drives))
        return False

    def _draw_waiting(self, now):
        """What the booth shows while waiting for the next guest."""
        theme = self.cfg["waiting_theme"]
        if self.black_idle:
            # (an old saved choice like the removed "hand" screen: the default one)
            self.waiting.draw(now, theme if theme in themes.THEMES else "confessional")
        else:
            self._draw_camera(now)

    @staticmethod
    def led_level(pattern, now):
        return level(pattern, now)

    def _draw_camera(self, now):
        if self.camera is None or (not self.camera.ok and self.state not in ("countdown",
                                                                             "recording")):
            self.ui.no_camera(now)
        else:
            _, frame = self.camera.latest()
            self.ui.preview(frame, self.preview_size, self.preview_rect)

    def _draw_saving(self, now, fade_in):
        cfg = self.cfg
        p = self.bar_elapsed / max(0.1, cfg["saving_min_seconds"])
        done = self.save_done.is_set()
        progress = min(p, 1.0) if done else min(p, 0.97)
        finished = self.state == "done"
        ok = self.save_ok if finished else None
        title = cfg["saving_text"] if not finished else \
            cfg["saved_text"] if self.save_ok else cfg["failed_text"]
        hold = None
        if self._hold_kind(now) == "delete":
            hold = min(1.0, (now - self.press_t) / cfg["delete_hold_seconds"])
        # while the button is held, the bar and the clip snippets stand still
        clip = self.highlights.frame(self.bar_elapsed)
        self.ui.saving_screen(title, 1.0 if finished else progress,
                              None if finished else self.ui.word("delete_hint_text"), hold, ok,
                              now if hold is not None else self.bar_elapsed,
                              background=clip, rect=self.preview_rect)
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
            n, clapper, _ = self._intro()
            el = now - self.state_t - self._tvon()
            pattern = "action" if el < 0 else "countdown" if el < n or not clapper else "action"
        elif st == "recording":
            pattern = "recording"
        elif st == "review":
            pattern = "saving"           # flash 0.3 s every 1.3 s while the saving bar shows
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
