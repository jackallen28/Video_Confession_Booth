"""Finding the webcam, what it can do, its controls, and its microphone."""

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
        with open(f"/sys/class/video4linux/{os.path.basename(os.path.realpath(dev))}/name") as f:
            return f.read().strip()
    except OSError:
        return None


def camera_name(dev):
    if dev == "test":
        return "test picture"
    return _sysfs_name(dev) or os.path.basename(dev or "?")


def _v4l2(args, timeout=10):
    try:
        r = subprocess.run(["v4l2-ctl"] + args, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "", str(e)


FOURCC = {"MJPG": "mjpeg", "YUYV": "yuyv422"}


def list_modes(dev):
    """-> [{"format": "mjpeg", "w": 1920, "h": 1080, "fps": 30.0}, ...] best first."""
    if dev == "test":
        return [{"format": "mjpeg", "w": w, "h": h, "fps": 30.0}
                for w, h in ((1920, 1080), (1280, 720), (640, 480))]
    rc, out, _ = _v4l2(["-d", dev, "--list-formats-ext"])
    modes, fmt, size = {}, None, None
    for line in out.splitlines():
        m = re.search(r"\[\d+\]:\s*'(\w+)'", line)
        if m:
            fmt = FOURCC.get(m.group(1))
            continue
        m = re.search(r"Size:\s*\w+\s+(\d+)x(\d+)", line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            continue
        m = re.search(r"\(([\d.]+)\s*fps\)", line)
        if m and fmt and size:
            key = (fmt, size)
            modes[key] = max(modes.get(key, 0.0), float(m.group(1)))
    result = [{"format": f, "w": w, "h": h, "fps": fps} for (f, (w, h)), fps in modes.items()]
    # MJPEG first, then biggest picture, then fastest
    result.sort(key=lambda m: (m["format"] == "mjpeg", m["w"] * m["h"], m["fps"]), reverse=True)
    return result


def pi_model():
    """'Raspberry Pi 5 Model B Rev 1.0', or '' when not on a Pi."""
    try:
        with open("/proc/device-tree/model") as f:
            return f.read().strip("\x00\n ")
    except OSError:
        return ""


def preview_fps(cfg):
    v = cfg.get("preview_fps", "auto")
    if str(v) != "auto":
        return float(v)
    return 15.0 if "Pi 3" in pi_model() else 24.0


def list_controls(dev):
    """Camera controls as reported by v4l2-ctl --list-ctrls-menus.
    -> [{"name", "type", "min", "max", "step", "default", "value", "flags", "menu": {n: label}, "group"}]"""
    if dev == "test":
        return []
    rc, out, _ = _v4l2(["-d", dev, "--list-ctrls-menus"])
    controls, cur, group = [], None, ""
    for line in out.splitlines():
        if not line.strip():
            continue
        m = re.match(r"^\s*(\w+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)$", line)
        if m:
            name, typ, rest = m.groups()
            cur = {"name": name, "type": typ, "menu": {}, "group": group, "flags": ""}
            for k, v in re.findall(r"(\w+)=(\S+)", rest):
                if k == "flags":
                    cur["flags"] = v
                else:
                    try:
                        cur[k] = int(v)
                    except ValueError:
                        cur[k] = v
            controls.append(cur)
            continue
        m = re.match(r"^\s+(-?\d+):\s*(.+)$", line)
        if m and cur is not None and cur["type"] in ("menu", "intmenu"):
            cur["menu"][int(m.group(1))] = m.group(2).strip()
            continue
        if not line.startswith(" ") or line.strip().endswith("Controls"):
            group = line.strip()
    return controls


def order_controls(controls):
    """Automatic modes first: e.g. auto exposure must be off before exposure time sticks."""
    return sorted(controls.items(), key=lambda kv: (0 if "auto" in kv[0] else 1))


def set_control(dev, name, value):
    rc, out, err = _v4l2(["-d", dev, "-c", f"{name}={value}"])
    return rc == 0, (err or out).strip()


def apply_camera_controls(dev, controls):
    if not controls or not dev or dev == "test":
        return
    for k, v in order_controls(controls):
        ok, msg = set_control(dev, k, v)
        if not ok:
            log.warning("camera setting %s=%s not applied: %s", k, v, msg)


def mic_card(mic):
    """'plughw:CARD=C920,DEV=0' -> 'C920'."""
    m = re.search(r"CARD=([^,]+)", mic or "")
    return m.group(1) if m else None


def _amixer(args):
    try:
        r = subprocess.run(["amixer"] + args, capture_output=True, timeout=10)
        return r.returncode, r.stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def mic_levels(card):
    """Capture volume controls of a sound card -> [{"name", "percent"}]."""
    if not card:
        return []
    rc, out = _amixer(["-c", card, "scontents"])
    result, name, caps = [], None, ""
    for line in out.splitlines():
        m = re.match(r"Simple mixer control '(.+)',(\d+)", line)
        if m:
            name, caps = m.group(1), ""
            continue
        if "Capabilities:" in line:
            caps = line
            continue
        m = re.search(r"Capture.*\[(\d+)%\]", line)
        if m and name and "cvolume" in caps and not any(r["name"] == name for r in result):
            result.append({"name": name, "percent": int(m.group(1))})
    return result


def set_mic_level(card, name, percent):
    rc, out = _amixer(["-c", card, "sset", name, f"{int(percent)}%"])
    if rc == 0:
        subprocess.run(["alsactl", "store"], capture_output=True)   # keep after reboot
    return rc == 0, out.strip()[-200:]


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
        caps = glob.glob(f"/proc/asound/card{num}/pcm*c")
        if caps:
            devnum = re.search(r"pcm(\d+)c", caps[0]).group(1)
            return f"plughw:CARD={card_id},DEV={devnum}"
    return None
