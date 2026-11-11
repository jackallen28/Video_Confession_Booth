"""Finding the webcam and its microphone."""

import glob
import logging
import os
import re
import subprocess

log = logging.getLogger("booth.devices")


def find_camera(setting):
    if setting != "auto":
        return setting
    # Only USB (and other pluggable) cameras appear here, not the Pi's own
    # codec/ISP video nodes.
    for path in sorted(glob.glob("/dev/v4l/by-id/*-video-index0")):
        return path
    for path in sorted(glob.glob("/dev/video*")):
        name = _sysfs_name(path)
        if name and not re.search(r"bcm2835|unicam|rpi|pispbe|hevc", name, re.I):
            return path
    return None


def _sysfs_name(dev):
    try:
        with open(f"/sys/class/video4linux/{os.path.basename(dev)}/name") as f:
            return f.read().strip()
    except OSError:
        return None


def find_microphone(setting):
    """Returns an ALSA device string, "test", or None for no sound."""
    if setting in ("none", "", None):
        return None
    if setting != "auto":
        return setting
    try:
        with open("/proc/asound/cards") as f:
            text = f.read()
    except OSError:
        return None
    # " 1 [C920           ]: USB-Audio - HD Pro Webcam C920"
    for m in re.finditer(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(\S+)\s+-\s+(.*)$", text, re.M):
        num, card_id, driver, name = m.groups()
        if re.search(r"bcm2835|vc4|hdmi", card_id + driver + name, re.I):
            continue
        if glob.glob(f"/proc/asound/card{num}/pcm*c"):
            dev = glob.glob(f"/proc/asound/card{num}/pcm*c")[0]
            devnum = re.search(r"pcm(\d+)c", dev).group(1)
            return f"plughw:CARD={card_id},DEV={devnum}"
    return None


def apply_camera_controls(dev, controls):
    if not controls or not dev or dev == "test":
        return
    for k, v in controls.items():
        try:
            subprocess.run(["v4l2-ctl", "-d", dev, "-c", f"{k}={v}"],
                           capture_output=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("v4l2-ctl %s=%s failed: %s", k, v, e)
