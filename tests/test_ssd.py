"""SSD tab: check, refuse, reformat, on a real (loop-device) disk.

    sudo python3 tests/test_ssd.py

Needs root, losetup, sfdisk and exfatprogs; skipped otherwise. The fake disk is
set up like a drive that came from a Mac/Windows PC: two partitions, the booth
using the big exFAT one.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from booth import config, media, ssdcheck, storage  # noqa: E402
from booth.storage import Storage  # noqa: E402


# This container's kernel can't mount exFAT (the Pi's can), so the whole-flow test
# uses ext4; test_exfat_commands runs the real exFAT commands on the fake disk.
FS, MKFS = ("ext4", ["mkfs.ext4", "-q", "-F", "-L"])


def sh(*cmd, inp=None):
    return subprocess.run(cmd, input=inp, capture_output=True, check=True, text=True).stdout


def can_run():
    return os.geteuid() == 0 and all(shutil.which(t) for t in
                                     ("losetup", "sfdisk", "mkfs.exfat", "fsck.exfat", "wipefs"))


def partitions(loop):
    out = json.loads(sh("lsblk", "-J", "-b", "-p", "-o", "NAME,FSTYPE,LABEL,SIZE,MOUNTPOINT", loop))
    parts = out["blockdevices"][0].get("children", [])
    for p in parts:                     # no udev here: read the type off the disk
        p.update({k: v for k, v in ssdcheck.blkid(p["name"]).items() if v})
    return parts


def make_video(path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=160x120:rate=10:duration=1", "-c:v", "libx264", "-preset",
                    "ultrafast", "-f", "mp4", path], check=True)


class Rig:
    def __init__(self, tmp, sd_free=None):
        self.img = os.path.join(tmp, "disk.img")
        with open(self.img, "wb") as f:
            f.truncate(300 << 20)
        self.loop = sh("losetup", "-f", "--show", "-P", self.img).strip()
        # like a drive from a PC: a small FAT partition and a big exFAT one
        sh("sfdisk", self.loop, inp="label: dos\n,40M,c\n,,7\n")
        subprocess.run(["partx", "-u", self.loop], capture_output=True)   # no udev in containers
        time.sleep(0.5)
        p1, p2 = [p["name"] for p in partitions(self.loop)]
        sh(*MKFS, "OTHER", p1)
        sh(*MKFS, "OLDSSD", p2)
        overrides = dict(config.dev_overrides(tmp), min_free_mb=1, ssd_mode="auto",
                         ssd_mount=os.path.join(tmp, "mnt"), health_interval=3600)
        self.cfg = config.load(overrides=overrides)
        os.makedirs(self.cfg["ssd_mount"], exist_ok=True)
        loop = self.loop

        def find(label=""):            # this loop disk stands in for the USB SSD
            parts = [p for p in partitions(loop) if p.get("fstype")]
            return max(parts, key=lambda p: int(p["size"]), default=None)
        self.patches = [mock.patch.object(storage, "find_usb_partition", find),
                        mock.patch.object(ssdcheck, "find_usb_partition", find)]
        for p in self.patches:
            p.start()
        self.st = Storage(self.cfg)
        os.makedirs(self.st.sd.video_dir, exist_ok=True)
        self.st.refresh(first=True)
        assert self.st.ssd.present, "loop SSD didn't mount"
        self.chk = ssdcheck.SSDCheck(self.st, self.cfg)
        real_drive = self.chk.drive
        self.chk.drive = lambda: dict(real_drive(), transport="usb")   # loop disks say None

    def run(self, kind):
        assert self.chk.start(kind) == (True, ""), kind
        self.chk.thread.join(300)
        return self.chk.status()

    def close(self):
        subprocess.run(["umount", self.cfg["ssd_mount"]], capture_output=True)
        for p in self.patches:
            p.stop()
        subprocess.run(["losetup", "-d", self.loop], capture_output=True)


def test_check_then_refused_then_reformat():
    with tempfile.TemporaryDirectory() as tmp:
        rig = Rig(tmp)
        st = rig.st
        try:
            os.makedirs(os.path.join(st.ssd.video_dir, "DELETED"), exist_ok=True)
            both, only, gone = ("2026-11-20_10-00-00.mp4", "2026-11-20_11-00-00.mp4",
                                "2026-11-20_12-00-00.mp4")
            for n in (both, only):
                make_video(st.ssd.path(n))
            make_video(os.path.join(st.ssd.video_dir, "DELETED", gone))
            shutil.copy(st.ssd.path(both), st.sd.path(both))
            with open(os.path.join(st.mountpoint, "holiday.jpg"), "w") as f:
                f.write("not the booth's")
            digest = {n: media.sha256_file(st.ssd.path(n)) for n in (both, only)}

            d = rig.chk.drive()
            assert d["fstype"] == FS and len(d["partitions"]) == 2, d
            assert any("partitions" in n for n in d["ideal"]), d["ideal"]
            assert d["ssd_only"] == 2 and d["other_files"] == ["holiday.jpg"], d

            # quick check: clean filesystem, data reads back identical, SSD back online
            j = rig.run("check")
            titles = " | ".join(r["title"] for r in j["results"])
            assert j["state"] == "done", j
            assert "System messages" in titles, titles             # shown, never "bad"
            assert all(r["level"] != "bad" for r in j["results"]), j["results"]
            assert "ext4 filesystem is clean" in titles and "Write/read test passed" in titles, titles
            assert st.ssd.present and not st.maintenance
            assert not os.path.exists(os.path.join(st.ssd.video_dir, ".booth-speed-test"))

            # no room on the microSD for the SSD-only videos: refused, SSD untouched
            with mock.patch.object(st, "_check_drive", lambda drv: setattr(drv, "free", 1000)):
                j = rig.run("format")
            assert j["state"] == "refused" and "hasn't room" in j["summary"], j
            assert [p["label"] for p in partitions(rig.loop)] == ["OTHER", "OLDSSD"]
            assert os.path.exists(st.ssd.path(only)) and st.ssd.present

            # reformat: videos copied to the microSD and checked first, then one exFAT partition
            with mock.patch.object(ssdcheck, "MKFS", MKFS + [ssdcheck.LABEL]):
                j = rig.run("format")
            assert j["state"] == "done", j
            parts = partitions(rig.loop)
            assert [(p["fstype"], p["label"]) for p in parts] == [(FS, "CONFESSIONS")], parts
            table = sh("blkid", "-p", "-o", "value", "-s", "PTTYPE", rig.loop).strip()
            assert table == "gpt", table
            for n in (both, only):
                assert media.sha256_file(st.sd.path(n)) == digest[n], n
            assert os.path.exists(os.path.join(st.sd.video_dir, "DELETED", gone)), \
                "guest-deleted clips are kept too"
            assert st.ssd.present and st.ssd.device == parts[0]["name"] and not st.ssd.errors
            assert only in st._pending, "booth not asked to check the copy"
            assert "__reconcile__" in st._pending, "videos not queued to go back onto the SSD"
            assert len([p for p in rig.chk.drive()["partitions"]]) == 1
        finally:
            rig.close()


def test_exfat_commands():
    """The exact partition + exFAT commands the reformat uses, on a fake disk."""
    if not can_run():
        print("skip test_exfat_commands")
        return
    with tempfile.TemporaryDirectory() as tmp:
        img = os.path.join(tmp, "d.img")
        with open(img, "wb") as f:
            f.truncate(200 << 20)
        loop = sh("losetup", "-f", "--show", "-P", img).strip()
        try:
            sh("wipefs", "-a", loop)
            sh("sfdisk", "--wipe", "always", loop,
               inp=f"label: gpt\nstart=2048, type={ssdcheck.EXFAT_GUID}\n")
            subprocess.run(["partx", "-u", loop], capture_output=True)
            part = partitions(loop)[0]["name"]
            sh(*ssdcheck.MKFS, part)
            subprocess.run(["fsck.exfat", "-n", part], check=True, capture_output=True)
            p = partitions(loop)
            assert [(x["fstype"], x["label"]) for x in p] == [("exfat", "CONFESSIONS")], p
            start = int(sh("cat", f"/sys/block/{os.path.basename(loop)}/"
                                  f"{os.path.basename(part)}/start"))
            assert start == 2048, "partition not on a 1 MB boundary"
        finally:
            subprocess.run(["losetup", "-d", loop], capture_output=True)


def test_kernel_messages_sorted_out():
    out = "\n".join([
        "[Fri Nov 20 22:17:29 2026] sd 0:0:0:0: [sda] tag#7 uas_eh_abort_handler 0 uas-tag 1",
        "[Fri Nov 20 22:17:29 2026] sd 0:0:0:0: [sda] tag#3 Sense Key : Illegal Request",
        "[Fri Nov 20 22:17:29 2026] sd 0:0:0:0: [sda] tag#3 CDB: ATA command pass through(16)",
        "[Fri Nov 20 22:17:30 2026] I/O error, dev sda, sector 12345 op 0x0:(READ)",
        "[Fri Nov 20 22:17:31 2026] usb 2-1: reset SuperSpeed USB device number 2 using xhci",
        "[Fri Nov 20 22:17:31 2026] mmcblk0: something else"])
    with mock.patch.object(ssdcheck, "run", lambda cmd, timeout=30: (0, out)):
        kinds = [m["kind"] for m in ssdcheck.kernel_messages("sda", "2-1")]
    assert kinds == ["link", "harmless", "harmless", "error", "link"], kinds


def test_test_mode_refuses():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = config.load(overrides=dict(config.dev_overrides(tmp)))
        st = Storage(cfg)
        ok, why = ssdcheck.SSDCheck(st, cfg).start("format")
        assert not ok and "only run on the Pi" in why


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        if fn is test_check_then_refused_then_reformat and not can_run():
            print("skip", fn.__name__, "(needs root, losetup, sfdisk, exfatprogs)")
            continue
        t = time.monotonic()
        fn()
        print(f"ok  {fn.__name__} ({time.monotonic() - t:.1f}s)")
    print("ssd checks OK")
