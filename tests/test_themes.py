"""Every waiting screen draws, in landscape and portrait, through its whole loop.

    python3 tests/test_themes.py
"""

import os
import sys
import time

os.environ["SDL_VIDEODRIVER"] = "dummy"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pygame  # noqa: E402

from booth import settings, themes  # noqa: E402


def test_all_themes_draw():
    pygame.init()
    opts = next(f for f in settings.FIELDS if f["key"] == "waiting_theme")["options"]
    assert set(opts) == set(themes.THEMES), "web page and themes disagree"
    for size in ((1024, 600), (600, 1024), (800, 480)):
        screen = pygame.display.set_mode(size)
        ws = themes.WaitingScreen(screen, {})
        for theme in themes.THEMES:
            for t in (0.0, 0.3, 1.0, 1.7, 2.5, 7.0, 12.3, 12.8, 13.5, 30.0):
                ws.draw(100 + t, theme)


def test_fessup_sequence():
    """Star first, then FESS UP!, then the small line; it holds, fades, starts over."""
    pygame.init()
    screen = pygame.display.set_mode((1024, 600))
    ws = themes.WaitingScreen(screen, {})
    red = themes.WaitingScreen.FESS_RED

    def at(t):
        ws.draw(50 + t, "fessup")
        return screen.get_at((512, 300))[:3], screen.get_at((314, 269))[:3], \
            screen.get_at((462, 356))[:3]

    frames = [at(i / 25) for i in range(0, 25 * 13)]     # continuous, as the booth draws
    centre = [f[0] for f in frames]
    title = [f[1] for f in frames]
    sub = [f[2] for f in frames]
    assert centre[0] == red, "starts on plain red"
    first_white = next(i for i, c in enumerate(centre) if c == (255, 255, 255))
    first_title = next(i for i, c in enumerate(title) if max(c) < 80)
    first_sub = next(i for i, c in enumerate(sub) if max(c) < 80)
    assert first_white < first_title < first_sub, (first_white, first_title, first_sub)
    assert first_sub / 25 < 2.2
    assert max(title[25 * 11]) < 80, "still showing at 11 s"
    later = at(12.3)                           # 2 s build + 10 s hold, then the fade
    assert later[0] != (255, 255, 255), "fading out before the next round"


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        t = time.monotonic()
        fn()
        print(f"ok  {fn.__name__} ({time.monotonic() - t:.1f}s)")
    print("theme checks OK")
