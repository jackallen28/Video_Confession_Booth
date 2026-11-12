"""How guests start and stop: the touch screen, the big red button on a GPIO
pin, or both (setting "input"). The space bar works too, for testing.

Press-and-release = record / stop. Holding is measured by the app (3 s delete,
30 s shutdown), so this only reports "down" and "up" events, each with where
it came from ("gpio", "touch" or "key")."""

import logging
import queue
import time

log = logging.getLogger("booth.button")


def uses_button(cfg):
    return cfg.get("input", "touch") in ("button", "both")


def uses_touch(cfg):
    return cfg.get("input", "touch") in ("touch", "both")


class Button:
    def __init__(self, cfg):
        self.events = queue.Queue()
        self._gpio = None
        self._fingers = set()
        if not uses_button(cfg):
            log.info("using the touch screen (physical button off)")
            return
        try:
            from gpiozero import Button as GpioButton
            self._gpio = GpioButton(cfg["button_pin"], pull_up=cfg["button_pull_up"],
                                    bounce_time=cfg["button_bounce"])
            self._gpio.when_pressed = lambda: self.events.put(("down", time.monotonic(), "gpio"))
            self._gpio.when_released = lambda: self.events.put(("up", time.monotonic(), "gpio"))
            log.info("button on GPIO%s", cfg["button_pin"])
        except Exception as e:   # not a Pi, or gpiozero missing
            log.warning("no GPIO button (%s); use the space bar", e)

    def key(self, down, source="key"):
        self.events.put(("down" if down else "up", time.monotonic(), source))

    def touch(self, finger, down):
        """A finger (or mouse) on the screen. Several fingers count as one press:
        down with the first, up when the last one lifts."""
        was = bool(self._fingers)
        if down:
            self._fingers.add(finger)
        else:
            self._fingers.discard(finger)
        if bool(self._fingers) != was:
            self.key(bool(self._fingers), "touch")

    @property
    def is_pressed(self):
        return bool(self._gpio and self._gpio.is_pressed)

    def get(self):
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None
