"""Recording format: picks the camera's best resolution that this Pi can turn
into H.264 in real time, and builds the encode settings (incl. the timestamp).

The first time a camera is used (and whenever the camera or settings change),
each candidate resolution is test-encoded for a few seconds with exactly the
pipeline used for recording. The biggest one that runs comfortably faster than
real time wins. The result is remembered in /var/lib/confession-booth/video-check.json.
"""

import hashlib
import json
import logging
import os
import subprocess
import time

from . import devices, media
from .config import size_tuple

log = logging.getLogger("booth.video")

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MIN_SPEED = 1.3          # must encode 30% faster than real time (headroom for the preview)


def available_encoders():
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, timeout=20)
        text = r.stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    return [e for e in ("h264_v4l2m2m", "libx264") if f" {e} " in text]


def bitrate(cfg, w, h, fps):
    if str(cfg["video_bitrate"]) != "auto":
        return str(cfg["video_bitrate"])
    bits = w * h * fps * 0.15
    return f"{max(2.0, min(12.0, bits / 1e6)):.1f}M"


def encoder_args(cfg, encoder, w, h, fps):
    gop = str(int(round(fps)) * 2)
    if encoder == "h264_v4l2m2m":
        return ["-c:v", "h264_v4l2m2m", "-b:v", bitrate(cfg, w, h, fps), "-g", gop]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "21", "-g", gop]


def _filter_escape(path):
    return "'" + path.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:") + "'"


TS_SIZES = {"small": 40, "medium": 30, "large": 22}      # picture height / n


def portrait(cfg):
    return cfg.get("orientation") == "portrait"


def orient_filters(cfg):
    """Turns the camera picture upright for portrait recordings."""
    if not portrait(cfg):
        return []
    rot = cfg.get("picture_rotation", "right")
    if rot == "right":
        return ["transpose=clock"]
    if rot == "left":
        return ["transpose=cclock"]
    return ["crop=trunc(ih*9/16/2)*2:ih"]          # camera not turned: the middle


def output_size(cfg, w, h):
    """Size of the recorded video for a camera mode of w x h."""
    if not portrait(cfg):
        return w, h
    if cfg.get("picture_rotation", "right") in ("right", "left"):
        return h, w
    return (h * 9 // 16) // 2 * 2, h


def video_filter(cfg, w, h, workdir, fps=None):
    """Filters for the recording: 4:2:0, upright for portrait, optional date/time."""
    parts = ["format=yuv420p"] + orient_filters(cfg)
    w, h = output_size(cfg, w, h)
    if cfg["timestamp"]:
        # The text goes in a file so no shell/filter escaping can break it.
        fmt = str(cfg["timestamp_format"]).replace("\\", "").replace(":", "\\:")
        textfile = os.path.join(workdir, "timestamp-text.txt")
        os.makedirs(workdir, exist_ok=True)
        with open(textfile, "w") as f:
            f.write("%{localtime:" + fmt + "}")
        short = min(w, h)
        size = max(12, int(short / TS_SIZES.get(cfg["timestamp_size"], 30)))
        m = max(6, int(short / 40))
        corner = cfg["timestamp_corner"]
        x = f"{m}" if "left" in corner else f"w-tw-{m}"
        y = f"{m}" if "top" in corner else f"h-th-{m}"
        opts = [f"textfile={_filter_escape(textfile)}", "fontcolor=white", f"fontsize={size}",
                "box=1", "boxcolor=black@0.45", f"boxborderw={max(3, size // 4)}",
                f"x={x}", f"y={y}"]
        if os.path.exists(FONT):
            opts.insert(1, f"fontfile={_filter_escape(FONT)}")
        parts.append("drawtext=" + ":".join(opts))
    if fps:
        # Constant frame rate into the encoder: webcams slow down in dim light,
        # and Apple players (iPhone, Safari, QuickTime) are fussy about uneven
        # frame timing. Missing frames are repeated instead.
        parts.append(f"fps={float(fps):g}")
    return ",".join(parts)


# ---------------------------------------------------------------- choosing
def _candidates(cfg, dev):
    modes = devices.list_modes(dev)
    fmt = cfg["camera_format"]
    if fmt == "auto":
        fmt = "mjpeg" if any(m["format"] == "mjpeg" for m in modes) else "yuyv422"
    capw, caph = size_tuple(cfg["max_video_size"])
    want_fps = None if str(cfg["framerate"]) == "max" else float(cfg["framerate"])
    out = []
    for m in modes:
        if m["format"] != fmt or m["w"] > capw or m["h"] > caph:
            continue
        fps = min(m["fps"], want_fps or 30.0)
        out.append({"format": fmt, "w": m["w"], "h": m["h"], "fps": fps})
    if str(cfg["video_size"]) != "max":
        w, h = size_tuple(cfg["video_size"])
        exact = [m for m in out if (m["w"], m["h"]) == (w, h)]
        if exact:
            return exact[:1], True
        log.warning("camera has no %dx%d %s mode, using its best", w, h, fmt)
    # Smooth ones (24 fps+) first, biggest first.
    out.sort(key=lambda m: (m["fps"] >= 24, m["w"] * m["h"], m["fps"]), reverse=True)
    seen, uniq = set(), []
    for m in out:
        if (m["w"], m["h"]) not in seen:
            seen.add((m["w"], m["h"]))
            uniq.append(m)
    if not uniq:
        log.warning("no camera modes found (%s), assuming 1280x720", dev)
        uniq = [{"format": fmt, "w": 1280, "h": 720, "fps": 30.0}]
    return uniq, False


def choose(cfg, dev, workdir, progress=None):
    """-> {"format", "w", "h", "fps", "encoder", "speed"}"""
    os.makedirs(workdir, exist_ok=True)
    cands, forced = _candidates(cfg, dev)
    encoders = available_encoders()
    if cfg["video_encoder"] != "auto":
        encoders = [cfg["video_encoder"]]
    if not encoders:
        raise RuntimeError("ffmpeg has no H.264 encoder (h264_v4l2m2m or libx264)")

    key = hashlib.sha1(json.dumps([devices.camera_name(dev), cands, encoders, cfg["timestamp"],
                                   cfg["timestamp_size"], str(cfg["video_bitrate"]),
                                   cfg["audio_bitrate"], cfg["orientation"],
                                   cfg["picture_rotation"]], sort_keys=True).encode()).hexdigest()
    cache_path = os.path.join(workdir, "video-check.json")
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        if cache.get("key") == key:
            log.info("recording format (tested before): %s", cache["choice"])
            return cache["choice"]
    except (OSError, ValueError, KeyError):
        pass

    choice, fallback, results = None, None, []
    for m in cands[:5]:
        for enc in encoders:
            if progress:
                progress(f"{m['w']}x{m['h']} {enc}")
            ok, speed, why = benchmark(cfg, m, enc, workdir)
            results.append({**m, "encoder": enc, "ok": ok, "speed": speed, "why": why})
            log.info("test %dx%d@%g %s: %s", m["w"], m["h"], m["fps"], enc,
                     f"{speed:.2f}x real time" if ok else f"failed ({why})")
            if ok:
                this = {**m, "encoder": enc, "speed": round(speed, 2)}
                if fallback is None or speed > fallback["speed"]:
                    fallback = this
                if speed >= MIN_SPEED or forced:
                    choice = this
                    break
        if choice:
            break
    if choice is None:
        if fallback is None:
            raise RuntimeError("no working H.264 encoder: " + "; ".join(r["why"] for r in results))
        choice = fallback
        log.warning("nothing reached %.1fx real time; using the fastest: %s", MIN_SPEED, choice)
    log.info("recording format: %dx%d @ %g fps, %s", choice["w"], choice["h"], choice["fps"],
             choice["encoder"])
    try:
        media.write_atomic(cache_path, json.dumps({"key": key, "choice": choice,
                                                   "tested": results, "when": time.ctime()},
                                                  indent=1))
    except OSError:
        pass
    return choice


def benchmark(cfg, mode, encoder, workdir, seconds=4.0):
    """Encodes `seconds` of camera-like frames with the real recording pipeline.
    Returns (ok, speed, why)."""
    w, h, fps = mode["w"], mode["h"], mode["fps"]
    src = os.path.join(workdir, f"bench-{w}x{h}.mjpeg")
    part = os.path.join(workdir, "bench.ts")
    mp4 = os.path.join(workdir, "bench.mp4")
    try:
        if not os.path.exists(src):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            f"testsrc2=size={w}x{h}:rate={fps}", "-t", str(seconds),
                            "-c:v", "mjpeg", "-q:v", "3", "-f", "mjpeg", src],
                           check=True, capture_output=True, timeout=120)
        cmd = (["ffmpeg", "-v", "error", "-nostdin", "-y", "-framerate", str(fps), "-f", "mjpeg",
                "-i", src, "-f", "lavfi", "-i",
                f"anullsrc=r={cfg['audio_rate']}:cl=mono", "-t", str(seconds),
                "-map", "0:v", "-map", "1:a", "-vf", video_filter(cfg, w, h, workdir, fps)]
               + encoder_args(cfg, encoder, w, h, fps)
               + ["-c:a", "aac", "-b:a", cfg["audio_bitrate"],
                  "-f", "tee", f"[f=mpegts:onfail=ignore]{part}"])
        t = time.monotonic()
        r = subprocess.run(cmd, capture_output=True, timeout=seconds * 15)
        took = time.monotonic() - t
        if r.returncode != 0:
            return False, 0.0, r.stderr.decode(errors="replace").strip()[-200:]
        ok, why = media.remux_to_mp4(part, mp4)
        if not ok:
            return False, 0.0, "MP4: " + why
        p = media.probe(mp4)
        if not p["ok"] or abs(p["duration"] - seconds) > 1.0:
            return False, 0.0, f"bad test file {p}"
        return True, seconds / took, ""
    except (OSError, subprocess.SubprocessError) as e:
        return False, 0.0, str(e)
    finally:
        for f in (part, mp4):
            try:
                os.remove(f)
            except OSError:
                pass
