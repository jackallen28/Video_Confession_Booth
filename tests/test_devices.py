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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
