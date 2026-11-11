"""Settings. Every value can be overridden in /etc/confession-booth/config.toml
(see config.example.toml), or in the file given with --config.

Camera settings made over SSH with `sudo booth-camera` are saved separately in
/etc/confession-booth/camera.json and override the same keys here."""

import copy
import logging
import os

log = logging.getLogger("booth.config")

DEFAULTS = {
    # --- Camera -----------------------------------------------------------
    # "auto" = first USB webcam found in /dev/v4l/by-id, or a path such as
    # "/dev/video0". "test" = built-in test picture (for trying it on a laptop).
    "camera": "auto",
    # "auto" = MJPEG if the camera has it (all HD webcams), else raw YUYV.
    "camera_format": "auto",
    # "max" = the camera's highest resolution that this Pi can record smoothly
    # (tested once at first start). Or e.g. "1280x720".
    "video_size": "max",
    "framerate": "max",          # or a number
    # The Pi 3B+'s H.264 hardware encoder tops out at 1080p.
    "max_video_size": "1920x1080",
    # "auto" = Pi hardware encoder (h264_v4l2m2m), else libx264.
    "video_encoder": "auto",
    "video_bitrate": "auto",     # e.g. "8M"
    "audio_bitrate": "128k",

    # Date/time burnt into the corner of the recordings.
    "timestamp": False,
    "timestamp_corner": "bottom-right",   # bottom-left, top-left, top-right
    "timestamp_format": "%d/%m/%Y %H:%M:%S",
    "timestamp_size": "medium",           # small, medium, large
    # Extra camera controls applied with v4l2-ctl at start, e.g.
    # {power_line_frequency = 1} (1 = 50 Hz, 2 = 60 Hz) to stop flicker.
    "v4l2_controls": {},
    # Decode the preview at 1/2 or 1/4 size (much less CPU on a Pi 3B+).
    # Recordings are not affected. "auto", 0, 1 or 2.
    "preview_lowres": "auto",
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
    # LED inside the button (through a transistor, see README). -1 = none.
    "led_pin": 27,
    "led_active_high": True,

    # --- Recording --------------------------------------------------------
    "countdown_from": 3,
    # Clapperboard + "ACTION!" after the countdown. Off = recording starts
    # straight after "1".
    "action_animation": False,
    "max_record_seconds": 300,   # auto stop
    "ignore_stop_for": 1.0,      # ignore a 2nd press right after ACTION
    # After STOP: TV switch-off, black, then the "saving" screen with a bar.
    "black_seconds": 2.0,
    "saving_min_seconds": 5.0,   # the bar never fills faster than this
    "delete_hold_seconds": 3.0,  # hold this long on the saving screen to delete
    "delete_confirm_seconds": 6.0,  # then press again within this time

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
    "camera_settings_file": "/etc/confession-booth/camera.json",
    "min_free_mb": 1024,         # stop / don't record below this
    "low_free_mb": 8192,         # status bar turns amber below this
    "health_interval": 15,
    "write_test_interval": 300,
    "scrub_interval": 600,       # re-check one saved file's checksum when idle
    "smart_interval": 1800,
    "copy_mb_per_s_while_recording": 6,

    # --- Screen -----------------------------------------------------------
    # Free-space bar at the bottom: "when_full" (only when a drive is nearly
    # full, i.e. under low_free_mb), "problems" (also for drive faults, missing
    # SSD/mic, power warnings) or "always".
    "status_bar": "when_full",
    "fullscreen": True,
    "window_size": "800x480",    # only when fullscreen = false
    "idle_text": "PRESS BUTTON TO RECORD",
    "action_text": "ACTION!",
    "saving_text": "VIDEO SAVING",
    "saved_text": "VIDEO SAVED",
    "failed_text": "VIDEO NOT SAVED",
    "delete_hint_text": "HOLD BUTTON 3 SEC TO DELETE",
    "delete_confirm_text": "PRESS AGAIN TO DELETE",
    "deleted_text": "VIDEO DELETED",

    # --- System -----------------------------------------------------------
    "allow_shutdown": True,      # false = just quit (for testing)
    "log_file": True,
}


# Keys that `booth-camera` (camera_setup.py) may save in camera.json.
CAMERA_KEYS = ("video_size", "framerate", "camera_format", "timestamp", "timestamp_corner",
               "timestamp_format", "timestamp_size", "mirror_preview", "v4l2_controls",
               "video_bitrate")


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
    for k, v in load_camera_settings(cfg["camera_settings_file"]).items():
        if k == "v4l2_controls" and isinstance(v, dict):
            cfg[k] = {**cfg[k], **v}
        elif k in CAMERA_KEYS:
            cfg[k] = v
    return cfg


def load_camera_settings(path):
    import json
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.error("ignoring %s: %s", path, e)
        return {}


def dev_overrides(base_dir):
    """Settings for running on a normal computer: window, test camera, folders."""
    return {
        "camera": "test" if not os.path.isdir("/dev/v4l/by-id") else "auto",
        "audio": "test",
        "sd_dir": os.path.join(base_dir, "sd"),
        "ssd_mode": "directory",
        "ssd_mount": os.path.join(base_dir, "ssd"),
        "state_dir": os.path.join(base_dir, "state"),
        "camera_settings_file": os.path.join(base_dir, "camera.json"),
        "fullscreen": False,
        "allow_shutdown": False,
        "min_free_mb": 100,
    }


def size_tuple(s):
    w, h = str(s).lower().split("x")
    return int(w), int(h)
