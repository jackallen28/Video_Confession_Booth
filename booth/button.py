"""The big red button on a GPIO pin (plus the space bar, for testing).

Press-and-release = record / stop. Holding is measured by the app (for the
30 second shutdown), so this only reports "down" and "up" events."""

import logging
import queue
import time

log = logging.getLogger("booth.button")


class Button:
    def __init__(self, cfg):
        self.events = queue.Queue()
        self._gpio = None
        try:
            from gpiozero import Button as GpioButton
            self._gpio = GpioButton(cfg["button_pin"], pull_up=cfg["button_pull_up"],
                                    bounce_time=cfg["button_bounce"])
            self._gpio.when_pressed = lambda: self.events.put(("down", time.monotonic()))
            self._gpio.when_released = lambda: self.events.put(("up", time.monotonic()))
            log.info("button on GPIO%s", cfg["button_pin"])
        except Exception as e:   # not a Pi, or gpiozero missing
            log.warning("no GPIO button (%s); use the space bar", e)

    def key(self, down):
        self.events.put(("down" if down else "up", time.monotonic()))

    @property
    def is_pressed(self):
        return bool(self._gpio and self._gpio.is_pressed)

    def get(self):
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None
