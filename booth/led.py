"""The LED inside the red button, on its own GPIO pin (PWM, so it can fade).

The app calls show(pattern, t) every frame; patterns are worked out from the
time, so the LED stays in step with the screen (e.g. it blinks with the red
recording dot)."""

import logging
import math

log = logging.getLogger("booth.led")


def level(pattern, t, since=0.0):
    """Brightness 0..1 for a pattern at time t (seconds); since = when it started."""
    e = t - since
    if pattern == "idle":            # slow breathing: "press me"
        return 0.12 + 0.88 * (0.5 - 0.5 * math.cos(2 * math.pi * e / 2.2))
    if pattern == "countdown":       # flash on each number, fading
        return max(0.0, 1.0 - (e % 1.0) * 1.6)
    if pattern == "action":
        return 1.0
    if pattern == "recording":       # in step with the red dot on screen
        return 1.0 if (t % 1.0) < 0.6 else 0.0
    if pattern == "saving":
        return 0.0
    if pattern == "delete_hold":     # gets faster as the 3 s hold fills up
        return 1.0 if math.sin(e * (8 + 18 * min(1.0, e / 3))) > 0 else 0.0
    if pattern == "confirm":         # quick blink: press me again to delete
        return 1.0 if (e % 0.3) < 0.15 else 0.0
    if pattern == "done":
        return 1.0
    if pattern == "shutdown_hold":
        return 1.0 if (e % 0.5) < 0.25 else 0.15
    if pattern == "problem":         # double blink
        p = e % 1.6
        return 1.0 if p < 0.12 or 0.25 < p < 0.37 else 0.0
    return 0.0                       # "off"


class Led:
    def __init__(self, cfg):
        self._led = None
        self._last = None
        self._pattern = None
        self._since = 0.0
        pin = int(cfg["led_pin"])
        if pin < 0:
            return
        try:
            from gpiozero import PWMLED
            self._led = PWMLED(pin, active_high=cfg["led_active_high"], frequency=200)
            log.info("button LED on GPIO%s", pin)
        except Exception as e:   # not a Pi, or gpiozero missing
            log.warning("no button LED (%s)", e)

    def show(self, pattern, t):
        if pattern != self._pattern:
            self._pattern, self._since = pattern, t
        self.set(level(pattern, t, self._since))

    def set(self, value):
        value = round(max(0.0, min(1.0, value)), 2)
        if self._led is None or value == self._last:
            return
        self._last = value
        try:
            self._led.value = value
        except Exception:
            pass

    def off(self):
        self.set(0.0)
