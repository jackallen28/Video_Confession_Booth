"""The screen: fits whatever display is plugged in.

* At start the Pi's kernel (KMS) has already picked the screen's own
  resolution from what the screen reports (EDID); the booth uses that.
* Big screens (e.g. a 1080p TV) are drawn at up to `max_render_size` and
  scaled up by the Pi's graphics chip: drawing every pixel of 1080p in Python
  is too slow on a Pi 3B+. Recordings are not affected.
* If no screen is connected at start, it waits for one.
* If the screen is unplugged, swapped or changes resolution, the booth
  restarts itself (when nobody is recording) to fit the new one.
"""

import glob
import logging
import os
import threading
import time

import pygame

from .config import size_tuple

log = logging.getLogger("booth.display")


def connectors():
    """[(name, status, preferred mode)] from the kernel, e.g.
    ("card1-HDMI-A-1", "connected", "1920x1080")."""
    out = []
    for status_file in sorted(glob.glob("/sys/class/drm/card*-*/status")):
        d = os.path.dirname(status_file)
        try:
            with open(status_file) as f:
                status = f.read().strip()
            with open(os.path.join(d, "modes")) as f:
                modes = f.read().split()
        except OSError:
            continue
        out.append((os.path.basename(d), status, modes[0] if modes else ""))
    return out


def signature():
    """What's plugged in, to notice changes."""
    return tuple(c for c in connectors() if c[1] == "connected")


def has_screen():
    cons = connectors()
    # Not a Pi / no KMS info (laptop, desktop session): assume yes.
    return not cons or any(c[1] == "connected" for c in cons)


def wait_for_screen():
    if has_screen():
        return
    log.warning("no screen connected; waiting for one")
    while not has_screen():
        time.sleep(2)
    log.info("screen connected: %s", signature())
    time.sleep(2)            # let the kernel finish setting it up


def render_size(native, cap):
    """Size to draw at: the screen's own, or scaled down (same shape) so the
    longer side is at most cap's longer side."""
    w, h = native
    limit = max(size_tuple(cap))
    if max(w, h) <= limit:
        return w, h
    k = limit / max(w, h)
    return int(w * k) // 2 * 2, int(h * k) // 2 * 2


def open_display(cfg):
    """Returns the display surface to draw on (logical size)."""
    if not cfg["fullscreen"]:
        return pygame.display.set_mode(size_tuple(cfg["window_size"]))
    sizes = pygame.display.get_desktop_sizes() if hasattr(pygame.display, "get_desktop_sizes") \
        else []
    native = sizes[0] if sizes else (0, 0)
    want = render_size(native, cfg["max_render_size"]) if native[0] else native
    log.info("display %dx%d%s", native[0], native[1],
             f", drawing at {want[0]}x{want[1]} (scaled up by the GPU)" if want != native else "")
    if want != native:
        try:
            return pygame.display.set_mode(want, pygame.FULLSCREEN | pygame.SCALED)
        except pygame.error as e:
            log.warning("GPU scaling not available (%s); drawing at full size", e)
    return pygame.display.set_mode((0, 0), pygame.FULLSCREEN)


class Watcher:
    """Calls on_change() when a screen is plugged in/out or changes mode."""

    def __init__(self, on_change, interval=3.0):
        self.on_change = on_change
        self.interval = interval
        self.start_sig = signature()

    def start(self):
        if not connectors():
            return                 # nothing to watch (not a Pi with KMS)
        threading.Thread(target=self._loop, name="display-watch", daemon=True).start()

    def _loop(self):
        last = self.start_sig
        while True:
            time.sleep(self.interval)
            sig = signature()
            if sig != last:
                # wait for it to settle (plugging in can flicker)
                time.sleep(2)
                sig2 = signature()
                if sig2 != sig:
                    last = sig2
                    continue
                log.warning("screen changed: %s -> %s", last or "none", sig or "none")
                last = sig
                if sig != self.start_sig:
                    self.on_change(sig)
