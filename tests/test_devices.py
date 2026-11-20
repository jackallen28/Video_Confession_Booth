"""Parsing v4l2-ctl output (as printed for a Logitech C920), and mode choice.

    python3 tests/test_devices.py
"""

import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from booth import config, devices, video  # noqa: E402

FORMATS = """ioctl: VIDIOC_ENUM_FMT
	Type: Video Capture

	[0]: 'YUYV' (YUYV 4:2:2)
		Size: Discrete 640x480
			Interval: Discrete 0.033s (30.000 fps)
			Interval: Discrete 0.067s (15.000 fps)
		Size: Discrete 1920x1080
			Interval: Discrete 0.200s (5.000 fps)
	[1]: 'H264' (H.264, compressed)
		Size: Discrete 1920x1080
			Interval: Discrete 0.033s (30.000 fps)
	[2]: 'MJPG' (Motion-JPEG, compressed)
		Size: Discrete 640x480
			Interval: Discrete 0.033s (30.000 fps)
		Size: Discrete 1280x720
			Interval: Discrete 0.033s (30.000 fps)
			Interval: Discrete 0.042s (24.000 fps)
		Size: Discrete 1920x1080
			Interval: Discrete 0.033s (30.000 fps)
		Size: Discrete 3840x2160
			Interval: Discrete 0.033s (30.000 fps)
"""

CONTROLS = """
User Controls

                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=140
                       contrast 0x00980901 (int)    : min=0 max=255 step=1 default=128 value=128
        white_balance_automatic 0x0098090c (bool)   : default=1 value=1
           power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=2 value=2 (60 Hz)
				0: Disabled
				1: 50 Hz
				2: 60 Hz
      white_balance_temperature 0x0098091a (int)    : min=2000 max=6500 step=1 default=4000 value=4000 flags=inactive

Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3 (Aperture Priority Mode)
				1: Manual Mode
				3: Aperture Priority Mode
         exposure_time_absolute 0x009a0902 (int)    : min=3 max=2047 step=1 default=250 value=250 flags=inactive
"""


def fake_v4l2(args, timeout=10):
    if "--list-formats-ext" in args:
        return 0, FORMATS, ""
    if "--list-ctrls-menus" in args:
        return 0, CONTROLS, ""
    return 0, "", ""


def test_modes():
    with mock.patch.object(devices, "_v4l2", fake_v4l2):
        modes = devices.list_modes("/dev/video0")
    assert modes[0] == {"format": "mjpeg", "w": 3840, "h": 2160, "fps": 30.0}, modes[0]
    assert {"format": "mjpeg", "w": 1280, "h": 720, "fps": 30.0} in modes
    assert all(m["format"] in ("mjpeg", "yuyv422") for m in modes)    # H264 ignored


def test_candidates_capped_at_1080p():
    cfg = config.load(overrides={"settings_file": "/nonexistent"})
    with mock.patch.object(devices, "_v4l2", fake_v4l2):
        cands, forced = video._candidates(cfg, "/dev/video0")
    assert (cands[0]["w"], cands[0]["h"]) == (1920, 1080) and not forced
    cfg["video_size"] = "1280x720"
    with mock.patch.object(devices, "_v4l2", fake_v4l2):
        cands, forced = video._candidates(cfg, "/dev/video0")
    assert forced and (cands[0]["w"], cands[0]["h"]) == (1280, 720)


def test_controls():
    with mock.patch.object(devices, "_v4l2", fake_v4l2):
        cs = {c["name"]: c for c in devices.list_controls("/dev/video0")}
    assert cs["brightness"]["value"] == 140 and cs["brightness"]["max"] == 255
    assert cs["power_line_frequency"]["menu"] == {0: "Disabled", 1: "50 Hz", 2: "60 Hz"}
    assert "inactive" in cs["exposure_time_absolute"]["flags"]
    assert cs["auto_exposure"]["group"] == "Camera Controls"
    order = [k for k, _ in devices.order_controls({"exposure_time_absolute": 100,
                                                   "auto_exposure": 1})]
    assert order == ["auto_exposure", "exposure_time_absolute"]


def test_saved_settings_override():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "settings.json")
        with open(p, "w") as f:
            f.write('{"timestamp": true, "v4l2_controls": {"brightness": 150}, "idle_text": "x",'
                    ' "led_pin": 5}')
        cfg = config.load(overrides={"settings_file": p,
                                     "v4l2_controls": {"sharpness": 2}})
    assert cfg["timestamp"] is True
    assert cfg["v4l2_controls"] == {"sharpness": 2, "brightness": 150}
    assert cfg["idle_text"] == "x"
    assert cfg["led_pin"] != 5                # only web-editable keys come from settings.json


def test_portrait_sizes_and_filters():
    cfg = config.load(overrides={"settings_file": "/nonexistent"})
    assert video.output_size(cfg, 1920, 1080) == (1920, 1080) and video.orient_filters(cfg) == []
    cfg["orientation"] = "portrait"
    assert video.output_size(cfg, 1920, 1080) == (1080, 1920)
    assert video.orient_filters(cfg) == ["transpose=clock"]
    cfg["picture_rotation"] = "left"
    assert video.orient_filters(cfg) == ["transpose=cclock"]
    cfg["picture_rotation"] = "crop"
    assert video.output_size(cfg, 1920, 1080) == (606, 1080)      # even width, 9:16
    with tempfile.TemporaryDirectory() as tmp:
        f = video.video_filter(dict(cfg, timestamp=True), 1920, 1080, tmp)
    # turned/cropped first, so the date/time is upright and inside the picture
    assert f.index("crop=") < f.index("drawtext") and f.startswith("scale=out_color_matrix=bt709:out_range=tv,format=yuv420p")


def test_apple_friendly_encoding():
    cfg = config.load(overrides={"settings_file": "/nonexistent"})
    args = video.encoder_args(cfg, "libx264:veryfast", 1920, 1080, 30)
    s = " ".join(args)
    assert "-profile:v high" in s and "-level:v 4.1" in s and "-preset veryfast" in s
    assert "-colorspace bt709" in s and "-color_range tv" in s
    from booth import media
    assert media.creation_time("/x/2026-11-20_21-04-33.mp4").endswith("Z")
    assert media.creation_time("holiday.mp4") is None
    assert video.describe_encoder("libx264:superfast") == "software superfast"


def test_choice_keeps_up_and_tests_little():
    """Pi 5 style (software only): best quality that keeps up; skip hopeless sizes."""
    speeds = {(1920, "ultrafast"): 1.2,                       # 1080p: too slow even at fastest
              (1280, "ultrafast"): 3.0, (1280, "veryfast"): 1.8, (1280, "superfast"): 2.3}
    tried = []

    def fake_bench(cfg, m, enc, workdir, seconds=4.0):
        tried.append((m["w"], enc))
        return True, speeds.get((m["w"], enc.split(":")[1]), 0.5), ""
    cfg = config.load(overrides={"settings_file": "/nonexistent"})
    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(devices, "_v4l2", fake_v4l2), \
            mock.patch.object(video, "benchmark", fake_bench), \
            mock.patch.object(video, "available_encoders",
                              lambda: ["libx264:veryfast", "libx264:superfast", "libx264:ultrafast"]):
        c = video.choose(cfg, "/dev/video0", tmp)
        assert (c["w"], c["encoder"]) == (1280, "libx264:superfast"), c
        assert (1920, "libx264:veryfast") not in tried, "1080p better settings not tried"
        # a fixed size that can't keep up: the fastest setting, not the best-quality one
        cfg["video_size"] = "1920x1080"
        tried.clear()
        c = video.choose(cfg, "/dev/video0", tmp)
        assert (c["w"], c["encoder"]) == (1920, "libx264:ultrafast"), c


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
