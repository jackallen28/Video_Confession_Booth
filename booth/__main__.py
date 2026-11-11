"""python3 -m booth [--dev] [--config FILE]"""

import argparse
import logging
import logging.handlers
import os
import sys


def main():
    ap = argparse.ArgumentParser(prog="booth", description="Confession booth recorder")
    ap.add_argument("--config", help="settings file (default /etc/confession-booth/config.toml)")
    ap.add_argument("--dev", action="store_true",
                    help="run in a window on a normal computer (test camera, ./booth-data folders)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    from . import config
    overrides = config.dev_overrides(os.path.abspath("booth-data")) if args.dev else None
    cfg = config.load(args.config, overrides)
    if args.dev:
        os.makedirs(cfg["ssd_mount"], exist_ok=True)   # a folder stands in for the SSD

    handlers = [logging.StreamHandler(sys.stderr)]
    if cfg["log_file"]:
        os.makedirs(cfg["state_dir"], exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            os.path.join(cfg["state_dir"], "booth.log"), maxBytes=2_000_000, backupCount=3))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY")
                                                 or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame
    pygame.display.init()
    pygame.font.init()

    from .app import App
    try:
        code = App(cfg).run()
    finally:
        pygame.quit()
    sys.exit(code)


if __name__ == "__main__":
    main()
