"""Settings. Every value can be overridden in /etc/confession-booth/config.toml
(see config.example.toml), or in the file given with --config."""

import copy
import logging
import os

log = logging.getLogger("booth.config")

DEFAULTS = {
    # --- Camera -----------------------------------------------------------
    # "auto" = first USB webcam found in /dev/v4l/by-id, or a path such as
    # "/dev/video0". "test" = built-in test picture (for trying it on a laptop).
    "camera": "auto",
    # "mjpeg" is what almost every USB webcam sends at 720p/1080p and costs the
    # Pi nothing to save. "yuyv422" for cameras without MJPEG (then 640x480).
    "camera_format": "mjpeg",
    "video_size": "1280x720",
    "framerate": 30,
    # Extra camera controls applied with v4l2-ctl at start, e.g.
    # {power_line_frequency = 1} (1 = 50 Hz, 2 = 60 Hz) to stop flicker.
    "v4l2_controls": {},
    # Decode the preview at 1/2 size (much less CPU on a Pi 3B+). Recordings
    # are not affected. 0 = off.
    "preview_lowres": 1,
    "preview_fps": 15,
    "mirror_preview": True,

    # --- Audio ------------------------------------------------------------
    # "auto" = first USB microphone (the webcam's own mic), "none", "test",
    # or an ALSA device such as "plughw:CARD=C920,DEV=0".
    "audio": "auto",
    "audio_rate": 48000,
    "audio_channels": 1,
    # Seconds to shift the video against the sound if lips look out of sync
    # (positive = video later).
    "av_offset": 0.0,

    # --- Button -----------------------------------------------------------
    "button_pin": 17,            # BCM numbering. Button between GPIO17 (pin 11) and GND (pin 9).
    "button_pull_up": True,
    "button_bounce": 0.05,
    "hold_feedback_after": 2.0,  # seconds before the shutdown ring appears
    "hold_shutdown_seconds": 30,

    # --- Recording --------------------------------------------------------
    "countdown_from": 3,
    "max_record_seconds": 300,   # auto stop
    "ignore_stop_for": 1.0,      # ignore a 2nd press right after ACTION

    # --- Storage ----------------------------------------------------------
    "sd_dir": "/srv/confessions",
    # "auto": the program finds and mounts the USB SSD itself (and remounts it
    # if it is unplugged and plugged back in). "directory": ssd_mount is just a
    # folder that is already there (fstab mounts, or testing).
    "ssd_mode": "auto",
    "ssd_mount": "/mnt/booth-ssd",
    "ssd_subdir": "Confessions",
    "ssd_label": "",             # prefer the partition with this label
    "state_dir": "/var/lib/confession-booth",
    "min_free_mb": 1024,         # stop / don't record below this
    "low_free_mb": 8192,         # status bar turns amber below this
    "health_interval": 15,
    "write_test_interval": 300,
    "scrub_interval": 600,       # re-check one saved file's checksum when idle
    "smart_interval": 1800,
    "copy_mb_per_s_while_recording": 6,

    # --- Screen -----------------------------------------------------------
    "fullscreen": True,
    "window_size": "800x480",    # only when fullscreen = false
    "idle_text": "PRESS BUTTON TO RECORD",
    "action_text": "ACTION!",

    # --- System -----------------------------------------------------------
    "allow_shutdown": True,      # false = just quit (for testing)
    "log_file": True,
}


def load(path=None, overrides=None):
    cfg = copy.deepcopy(DEFAULTS)
    candidates = [path] if path else [os.environ.get("BOOTH_CONFIG"),
                                      "/etc/confession-booth/config.toml"]
    for p in candidates:
        if not p or not os.path.exists(p):
            if path:
                raise FileNotFoundError(path)
            continue
        try:
            import tomllib
        except ImportError:  # Python < 3.11
            log.warning("tomllib missing, ignoring %s", p)
            break
        with open(p, "rb") as f:
            data = tomllib.load(f)
        for k, v in data.items():
            if k not in cfg:
                log.warning("unknown setting %r in %s (ignored)", k, p)
                continue
            cfg[k] = v
        log.info("settings from %s", p)
        break
    if overrides:
        cfg.update(overrides)
    return cfg


def dev_overrides(base_dir):
    """Settings for running on a normal computer: window, test camera, folders."""
    return {
        "camera": "test" if not os.path.isdir("/dev/v4l/by-id") else "auto",
        "audio": "test",
        "sd_dir": os.path.join(base_dir, "sd"),
        "ssd_mode": "directory",
        "ssd_mount": os.path.join(base_dir, "ssd"),
        "state_dir": os.path.join(base_dir, "state"),
        "fullscreen": False,
        "allow_shutdown": False,
        "min_free_mb": 100,
    }


def size_tuple(s):
    w, h = str(s).lower().split("x")
    return int(w), int(h)
