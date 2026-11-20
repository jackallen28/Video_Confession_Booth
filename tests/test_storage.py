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


def test_wipe_all_videos():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        a = st.new_name()
        record(st, a, [st.ssd, st.sd])
        run_queue(st)
        from unittest import mock
        with mock.patch("booth.storage.time.strftime", return_value=a[:-4] + "b"):
            b = st.new_name()
        record(st, b, [st.ssd, st.sd], keep=False)
        st.delete(b)                                   # in the DELETED folders
        mine = os.path.join(st.ssd.video_dir, "my notes.txt")
        open(mine, "w").write("not a video")
        outside = os.path.join(cfg["ssd_mount"], "Holiday.mp4")   # outside the booth folder
        open(outside, "w").write("x")
        rep = st.wipe_all()
        assert rep["files"] >= 4 and sorted(rep["drives"]) == ["sd", "ssd"], rep
        for d in (st.ssd, st.sd):
            left = [f for f in os.listdir(d.video_dir) if f != "my notes.txt"]
            assert not left, left
            assert not os.path.exists(os.path.join(d.video_dir, "DELETED"))
        assert os.path.exists(mine) and os.path.exists(outside), "only the booth's videos"
        assert st.manifest.names() == [] and st.deleted_names() == []
        assert not st.wiping and not st.paused.is_set()
        # recording works normally afterwards
        c = st.new_name()
        assert record(st, c, [st.ssd, st.sd])


def test_wipe_with_ssd_unplugged_reports_it():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        a = st.new_name()
        record(st, a, [st.ssd, st.sd])
        os.rename(cfg["ssd_mount"], cfg["ssd_mount"] + "-away")
        st.refresh()
        rep = st.wipe_all()
        assert rep["skipped"] == ["ssd"] and rep["drives"] == ["sd"], rep


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


def test_smart_check_is_not_an_ssd_error():
    """USB adapters that refuse SMART make the kernel count I/O errors; those must
    not mark the SSD as failing. Real errors still do, logged once, and a re-plugged
    drive starts clean."""
    from unittest import mock
    from booth import storage
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        st.auto = True
        cnt = {"n": 5}
        real_exists = os.path.exists

        def smartctl(cmd, timeout=30):
            if cmd[0] == "smartctl":
                cnt["n"] += 1                      # the adapter refuses: kernel counts it
                return 2, "unsupported"
            return 0, ""
        with mock.patch.object(storage, "mount_entry", return_value=("/dev/sda4", "exfat", "rw")), \
                mock.patch.object(storage.os.path, "exists",
                                  lambda p: p == "/dev/sda4" or real_exists(p)), \
                mock.patch.object(storage, "read_ioerr", lambda dev: cnt["n"]), \
                mock.patch.object(storage, "disk_sysfs", return_value="/sys/block/sda"), \
                mock.patch.object(storage.shutil, "which", return_value="/usr/sbin/smartctl"), \
                mock.patch.object(storage, "run", smartctl):
            assert not st.cfg["smart_interval"], "automatic SMART queries should be off"
            st._check_ssd()
            assert st.ssd.device == "/dev/sda4" and not st.ssd.errors
            st._smart()
            st._check_ssd()
            assert cnt["n"] == 7 and not st.ssd.errors, st.ssd.errors
            assert st._no_smart                    # not tried again on this drive
            st._smart()
            assert cnt["n"] == 7
            st._forgive_until = 0                  # 90 s later: errors count again
            cnt["n"] += 1                          # an error the system counted
            st._check_ssd()
            # noted on the web page, but the SSD stays in use: only a recording that
            # doesn't save stops the booth using a drive
            assert st.ssd.notes.get("io", "").startswith("1 I/O errors"), st.ssd.notes
            assert not st.ssd.errors and st.ssd.usable(0)
            cnt["n"] = 0                           # re-plugged: the kernel's count restarts
            st._check_ssd()
            assert "io" not in st.ssd.notes


def test_placement_and_room_left():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp, mode="ssd_first")
        a, b = st.new_name(), "2026-01-01_00-00-01.mp4"
        record(st, a, [st.ssd])
        record(st, b, [st.sd])
        run_queue(st)
        pl = st.placement()
        on = {v["name"]: v["on"] for v in pl["videos"]}
        assert on[a]["ssd"] == "verified" and on[a]["sd"] in (False, "verified"), on
        assert on[b]["sd"] == "verified", on
        assert pl["mb_per_minute"] is None, "too little video to estimate yet"
        assert set(pl["room"]) == {"ssd", "sd"} and pl["room"]["sd"]["free"] > 0

def test_storage_modes():
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp)
        assert config.DEFAULTS["storage_mode"] == "both", "default: both drives at once"
        want = {"both": {"ssd", "sd"}, "ssd_first": {"ssd"}, "sd_first": {"sd"},
                "ssd_only": {"ssd"}, "sd_only": {"sd"}}
        for mode, drives in want.items():
            cfg["storage_mode"] = mode
            assert set(st.targets()) == drives, (mode, st.targets())
        # the first choice full: "first" modes move on, "only" modes don't
        st.ssd.free = 0
        for mode, drives in {"both": {"sd"}, "ssd_first": {"sd"}, "ssd_only": set(),
                             "sd_first": {"sd"}}.items():
            cfg["storage_mode"] = mode
            assert set(st.targets()) == drives, (mode, st.targets())


def test_last_recording_is_the_drive_check():
    """A drive whose last recording didn't save isn't used for the next one; a
    video that then copies onto it properly puts it back in use."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg, st = setup(tmp, mode="both")
        a = st.new_name()
        parts = {d.name: d.path(st.part_name(a)) for d in (st.ssd, st.sd)}
        make_video(parts["sd"])
        open(parts["ssd"], "wb").close()               # nothing reached the SSD
        st.finalize({"name": a, "parts": parts, "duration": 2.0, "frames": 60, "dropped": 0})
        assert "last_save" in st.ssd.errors and set(st.targets()) == {"sd"}, st.ssd.errors
        st.accept(a)
        run_queue(st)                                  # the booth copies it over: works
        assert "last_save" not in st.ssd.errors, st.ssd.errors
        assert set(st.targets()) == {"ssd", "sd"}
        assert media.sha256_file(st.ssd.path(a)) == media.sha256_file(st.sd.path(a))


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        t = time.monotonic()
        fn()
        print(f"ok  {fn.__name__} ({time.monotonic() - t:.1f}s)")
