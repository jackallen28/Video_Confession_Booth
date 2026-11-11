"""Storage worker checks with real (tiny) video files; no Pi needed.

    python3 tests/test_storage.py      (or: python3 -m pytest tests)
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from booth import config, media  # noqa: E402
from booth.storage import Storage  # noqa: E402


def make_video(path, seconds=2):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=320x240:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=duration={seconds}",
                    "-c:v", "mjpeg", "-c:a", "pcm_s16le", "-f", "matroska", path], check=True)


def setup(tmp):
    cfg = config.load(overrides=dict(config.dev_overrides(tmp), min_free_mb=1,
                                     health_interval=3600))
    os.makedirs(cfg["ssd_mount"], exist_ok=True)
    st = Storage(cfg)
    os.makedirs(st.sd.video_dir, exist_ok=True)
    st.refresh(first=True)
    return cfg, st


def record(st, name, drives):
    for d in drives:
        make_video(d.path(name) + ".part")
    return st.finalize({"name": name, "targets": {d.name: d.path(name) for d in drives},
                        "duration": 2.0, "frames": 60, "dropped": 0})


def run_queue(st):
    while not st._q.empty():
        item = st._q.get()
        st._pending.discard(item)
        if item == "__reconcile__":
            st._reconcile()
        elif item == "__scrub__":
            st._scrub()
        else:
            st._process(item)


def test_both_copies_verified():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        name = st.new_name()
        assert record(st, name, [st.ssd, st.sd])
        run_queue(st)
        e = st.manifest.entry(name)
        assert e["state"] == "safe", e
        sums = open(os.path.join(st.ssd.video_dir, "SHA256SUMS")).read()
        assert name in sums
        # the SHA256SUMS file works with the standard tool
        r = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=st.sd.video_dir,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_ssd_missing_then_mirrored():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        os.rename(cfg["ssd_mount"], cfg["ssd_mount"] + "-away")    # unplug
        st.refresh()
        assert not st.ssd.present and list(st.targets()) == ["sd"]
        name = st.new_name()
        assert record(st, name, [st.sd])
        run_queue(st)
        assert st.manifest.entry(name)["state"] == "one_copy"
        os.rename(cfg["ssd_mount"] + "-away", cfg["ssd_mount"])    # plug back in
        st.refresh()
        assert st.ssd.present
        run_queue(st)                                              # reconcile -> copy
        e = st.manifest.entry(name)
        assert e["state"] == "safe", e
        assert e["copies"]["ssd"]["copied_from"] == "sd"
        assert media.sha256_file(st.ssd.path(name)) == media.sha256_file(st.sd.path(name))


def test_corruption_found_and_repaired():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        name = st.new_name()
        record(st, name, [st.ssd, st.sd])
        run_queue(st)
        good = media.sha256_file(st.sd.path(name))
        with open(st.ssd.path(name), "r+b") as f:       # flip bytes in the middle
            f.seek(os.path.getsize(st.ssd.path(name)) // 2)
            f.write(b"\x00" * 64)
        st._scrub()
        assert st.manifest.entry(name)["copies"]["ssd"]["status"] == "suspect"
        run_queue(st)
        e = st.manifest.entry(name)
        assert e["state"] == "safe", e
        assert media.sha256_file(st.ssd.path(name)) == good
        assert [f for f in os.listdir(st.ssd.video_dir) if ".bad-" in f], "damaged copy kept"


def test_crash_leftover_recovered():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        name = st.new_name()
        make_video(st.sd.path(name) + ".part")          # power cut mid-recording
        st._reconcile()
        assert os.path.exists(st.sd.path(name))
        run_queue(st)
        assert st.manifest.entry(name)["state"] == "safe"
        assert st.new_name() > name                      # numbers never reused


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        t = time.monotonic()
        fn()
        print(f"ok  {fn.__name__} ({time.monotonic() - t:.1f}s)")
