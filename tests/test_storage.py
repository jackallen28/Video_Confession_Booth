"""Storage worker checks with real (tiny) video files; no Pi needed.

    python3 tests/test_storage.py      (or: python3 -m pytest tests)
"""

import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from booth import config, media  # noqa: E402
from booth.storage import Storage  # noqa: E402


def make_video(path, seconds=2):
    """A recording as the recorder leaves it: H.264 + AAC in MPEG-TS."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=320x240:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-f", "mpegts", path], check=True)


def setup(tmp, mode="both"):
    cfg = config.load(overrides=dict(config.dev_overrides(tmp), min_free_mb=1,
                                     health_interval=3600, storage_mode=mode))
    os.makedirs(cfg["ssd_mount"], exist_ok=True)
    st = Storage(cfg)
    os.makedirs(st.sd.video_dir, exist_ok=True)
    st.refresh(first=True)
    return cfg, st


def record(st, name, drives, keep=True):
    parts = {d.name: d.path(st.part_name(name)) for d in drives}
    for p in parts.values():
        make_video(p)
    ok = st.finalize({"name": name, "parts": parts, "duration": 2.0, "frames": 60, "dropped": 0})
    if keep:
        st.accept(name)
    return ok


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
        make_video(st.sd.path(st.part_name(name)))      # power cut mid-recording
        st._reconcile()
        assert os.path.exists(st.sd.path(name))
        assert not os.path.exists(st.sd.path(st.part_name(name)))
        run_queue(st)
        assert st.manifest.entry(name)["state"] == "safe"
        assert st.new_name() != name                     # names never reused


def test_unfinished_copies_cleaned_only_if_safe():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        name = st.new_name()
        record(st, name, [st.sd])
        run_queue(st)
        junk = st.ssd.path(name + ".part")           # copy to the SSD cut short
        open(junk, "wb").write(b"half")
        lonely = st.ssd.path("2020-01-01_00-00-00.mp4.part")   # no complete copy anywhere
        open(lonely, "wb").write(b"only data")
        st._reconcile()
        assert not os.path.exists(junk), "half copy of a saved video removed"
        assert os.path.exists(lonely), "never delete the only data"


def test_ssd_first_then_sd():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp, mode="ssd_first")
        assert list(st.targets()) == ["ssd"], "SSD only while it has room"
        name = st.new_name()
        record(st, name, [st.ssd])
        run_queue(st)
        assert st.manifest.entry(name)["state"] == "safe"
        assert not os.path.exists(st.sd.path(name)), "no second copy on the microSD"
        st._reconcile()
        assert st._q.empty(), "nothing to mirror"

        st.ssd.free = 1                      # SSD full -> microSD
        assert list(st.targets()) == ["sd"]
        assert not st.has_room(["ssd"]), "a recording on the full SSD is stopped and saved"
        assert st.has_room(["sd"])
        name2 = st.new_name() if st.new_name() != name else name[:-4] + "_2.mp4"
        record(st, name2, [st.sd])
        run_queue(st)                        # SSD still full: stays on the microSD only
        assert st.manifest.entry(name2)["state"] == "safe"
        assert not os.path.exists(st.ssd.path(name2))

        st.refresh()                         # room on the SSD again (e.g. emptied)
        st._reconcile()
        run_queue(st)                        # microSD clip copied onto the SSD
        assert media.sha256_file(st.ssd.path(name2)) == media.sha256_file(st.sd.path(name2))
        assert os.path.exists(st.sd.path(name2)), "never removed from the microSD"


def test_names_are_timestamps_and_mp4():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        name = st.new_name()
        assert re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.mp4", name), name
        record(st, name, [st.ssd, st.sd])
        run_queue(st)
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_name:format=format_name", "-of", "csv=p=0",
                            st.sd.path(name)], capture_output=True, text=True)
        assert "h264" in r.stdout and "aac" in r.stdout and "mp4" in r.stdout, r.stdout
        from unittest import mock
        with mock.patch("booth.storage.time.strftime", return_value=name[:-4]):
            assert st.new_name() == name[:-4] + "_2.mp4"  # same second: _2 added


def test_guest_delete():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        name = st.new_name()
        assert record(st, name, [st.ssd, st.sd], keep=False)
        st._reconcile()                                  # must not touch a clip under review
        assert st._q.empty()
        st.delete(name)
        for d in (st.ssd, st.sd):
            assert not [f for f in os.listdir(d.video_dir) if f.startswith(name[:-4])]
            kept = os.path.join(d.video_dir, "DELETED", name)
            assert media.probe(kept)["ok"], "guest-deleted clips are moved, never erased"
        assert st.manifest.entry(name)["state"] == "deleted"
        assert name not in st.manifest.names() and name in st.deleted_names()
        assert st.video_path(name).endswith(os.path.join("DELETED", name))
        assert name not in open(os.path.join(st.sd.video_dir, "SHA256SUMS")).read()
        # restore from the web page
        assert st.restore(name)
        run_queue(st)
        assert st.manifest.entry(name)["state"] == "safe"
        assert os.path.exists(st.sd.path(name)) and os.path.exists(st.ssd.path(name))


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        t = time.monotonic()
        fn()
        print(f"ok  {fn.__name__} ({time.monotonic() - t:.1f}s)")
