"""Recorder timing: smooth, right-length video whatever rate the camera delivers.

    python3 tests/test_recorder.py

Webcams slow down in dim light, and a busy Pi can read the camera late. The
video must still last as long as the recording, with each picture shown for an
even number of frames (no fast-forward, no jumps), and the sound must line up.
"""

import collections
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from booth import config, media, mkv, video  # noqa: E402
from booth.recorder import Recorder  # noqa: E402


class Cam:
    def __init__(self):
        self.sinks = []

    def add_sink(self, f):
        self.sinks.append(f)

    def remove_sink(self, f):
        self.sinks.remove(f)


def jpegs(tmp, n=30, size="640x360"):
    src = os.path.join(tmp, "src.mjpeg")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size={size}:rate=30", "-frames:v", str(n), "-c:v", "mjpeg",
                    "-q:v", "4", "-f", "mjpeg", src], check=True)
    data, out, i = open(src, "rb").read(), [], 0
    while (s := data.find(b"\xff\xd8", i)) >= 0:
        e = data.find(b"\xff\xd9", s) + 2
        out.append(data[s:e])
        i = e
    return out


def record(tmp, frames, times, mic="test"):
    cfg = config.load(overrides=dict(config.dev_overrides(tmp)))
    cam = Cam()
    rec = Recorder(cfg, cam, {"w": 640, "h": 360, "fps": 30.0, "encoder": "libx264:ultrafast"},
                   tmp)
    part, mp4 = os.path.join(tmp, "t.ts.part"), os.path.join(tmp, "t.mp4")
    assert rec.start("t.mp4", {"sd": part}, mic)
    t0 = time.monotonic()
    for n, t in enumerate(times):
        while time.monotonic() < t0 + t:
            time.sleep(0.0005)
        for s in list(cam.sinks):
            s(frames[n % len(frames)], time.monotonic())
    res = rec.stop()
    ok, why = media.remux_to_mp4(part, mp4)
    assert ok, why
    return res, mp4


def picture_changes(mp4):
    """How many output frames each new picture is held for."""
    r = subprocess.run(["ffmpeg", "-i", mp4, "-map", "0:v", "-vf",
                        "mpdecimate=hi=64*12:lo=64*5:frac=0.33:max=0,showinfo",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    pts = [float(x) for x in re.findall(r"pts_time:([\d.]+)", r)]
    return collections.Counter(round((b - a) * 30) for a, b in zip(pts, pts[1:]))


def streams(mp4):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,start_time,duration", "-of", "json", mp4],
                       capture_output=True)
    return {s["codec_type"]: (float(s["start_time"]), float(s["duration"]))
            for s in json.loads(r.stdout)["streams"]}


def test_mkv_writer_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        frames = jpegs(tmp, 5)
        w = mkv.Writer(640, 360)
        path = os.path.join(tmp, "x.mkv")
        with open(path, "wb") as f:
            for n, ms in enumerate([0, 40, 100, 31_000, 31_050]):   # crosses a cluster
                f.write(w.frame(frames[n], ms))
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                            "packet=pts_time", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        assert [round(float(x), 3) for x in r.stdout.split()] == \
            [0.0, 0.04, 0.1, 31.0, 31.05], r.stdout


def test_steady_camera():
    with tempfile.TemporaryDirectory() as tmp:
        res, mp4 = record(tmp, jpegs(tmp), [n / 30 for n in range(120)])
        s = streams(mp4)
        assert abs(s["video"][1] - 4.0) < 0.3, s
        assert abs(s["audio"][0] - s["video"][0]) < 0.25, f"sound out of step: {s}"
        assert not media.iphone_issues(mp4)
        gaps = picture_changes(mp4)
        assert gaps.get(1, 0) >= 0.95 * sum(gaps.values()), gaps


def test_dim_light_15fps_is_not_fast_forward():
    with tempfile.TemporaryDirectory() as tmp:
        res, mp4 = record(tmp, jpegs(tmp), [n / 15 for n in range(60)])
        s = streams(mp4)
        assert abs(s["video"][1] - 4.0) < 0.3, f"video length wrong (fast-forward?): {s}"
        assert abs(s["audio"][1] - s["video"][1]) < 0.4, s
        gaps = picture_changes(mp4)
        assert gaps.get(2, 0) >= 0.9 * sum(gaps.values()), f"uneven: {gaps}"
        assert res["fps_in"] < 20


def test_bursty_delivery_stays_smooth():
    with tempfile.TemporaryDirectory() as tmp:
        times = [(n // 2) * 2 / 30 + (n % 2) * 0.002 for n in range(120)]
        res, mp4 = record(tmp, jpegs(tmp), times, mic=None)
        gaps = picture_changes(mp4)
        assert gaps.get(1, 0) >= 0.9 * sum(gaps.values()), gaps


def test_slow_down_steps_to_faster_preset():
    with tempfile.TemporaryDirectory() as tmp:
        mode = {"w": 1920, "h": 1080, "fps": 30.0, "encoder": "libx264:veryfast"}
        with open(os.path.join(tmp, "video-check.json"), "w") as f:
            json.dump({"key": "k", "choice": mode}, f)
        new = video.slow_down(tmp, mode)
        assert new["encoder"] == "libx264:superfast"
        assert json.load(open(os.path.join(tmp, "video-check.json")))["choice"] == new
        assert video.slow_down(tmp, dict(mode, encoder="libx264:ultrafast")) is None
        assert video.slow_down(tmp, dict(mode, encoder="h264_v4l2m2m")) is None
        # older versions saved plain "libx264": that was the fastest setting
        args = video.encoder_args({}, "libx264", 1920, 1080, 30)
        assert "ultrafast" in args
        # ultrafast's blockiness filter and better compression switched back on
        assert "deblock=1,0:cabac=1" in args


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        t = time.monotonic()
        fn()
        print(f"ok  {fn.__name__} ({time.monotonic() - t:.1f}s)")
    print("recorder checks OK")
