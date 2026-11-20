"""The SSD tab: check the SSD, scan it, read its SMART report, repair or
reformat it. One job at a time, in the background; the web page polls status().

Videos come first:
- nothing here deletes a video. Reformatting refuses unless every video on the
  SSD also has a checked copy on the microSD (it makes those copies first if
  there is room, and the booth copies them back onto the fresh SSD after);
- the SSD is only taken offline while nobody is recording; recordings made
  meanwhile go to the microSD;
- the read scan and the speed test never write over anything (the speed test
  writes its own temporary file in free space and removes it).
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time

from . import media
from .storage import disk_sysfs, find_usb_partition, mount_entry, run

log = logging.getLogger("booth.ssdcheck")

LABEL = "CONFESSIONS"                 # exFAT volume name after reformatting (max 11)
EXFAT_GUID = "EBD0A0A2-B9E5-4433-87C0-68B6B72699C7"   # "Microsoft basic data" (Mac/Windows)
MKFS = ["mkfs.exfat", "-L", LABEL]
SPEED_TEST_MB = 256
CHUNK = 4 << 20

# kernel messages, newest last: what they mean for this drive
HARMLESS = re.compile(r"ATA_1[26]|pass ?through|Invalid field in cdb|Illegal Request|"
                      r"Invalid command operation code|Unaligned partial completion", re.I)
REAL_ERROR = re.compile(r"I/O error|Medium Error|Unrecovered read|critical medium|"
                        r"Buffer I/O error|exFAT-fs .*error|FAT-fs .*error|EXT4-fs error", re.I)
LINK_PROBLEM = re.compile(r"reset (?:Super|high|full)|disconnect|device descriptor read|"
                          r"uas_eh_abort|cmd cancelled|timing out command|rejecting I/O", re.I)


def _usb_info(disk_dir):
    """/sys/block/sda -> {"speed": Mb/s, "driver", "adapter", "id"} of the USB link."""
    info = {}
    p = os.path.realpath(os.path.join(disk_dir, "device"))
    while p and p != "/":
        if "driver" not in info and os.path.exists(os.path.join(p, "bInterfaceNumber")):
            drv = os.path.join(p, "driver")
            if os.path.islink(drv):
                info["driver"] = os.path.basename(os.path.realpath(drv))
        if os.path.exists(os.path.join(p, "speed")) and os.path.exists(os.path.join(p, "idVendor")):
            def rd(f):
                try:
                    with open(os.path.join(p, f)) as fh:
                        return fh.read().strip()
                except OSError:
                    return ""
            info["speed"] = int(rd("speed") or 0)
            info["adapter"] = " ".join(x for x in (rd("manufacturer"), rd("product")) if x)
            info["id"] = f"{rd('idVendor')}:{rd('idProduct')}"
            info["port"] = os.path.basename(p)
            break
        p = os.path.dirname(p)
    return info


def blkid(dev):
    """Filesystem type and label read from the disk itself (lsblk relies on udev)."""
    rc, out = run(["blkid", "-o", "export", dev], timeout=10)
    vals = dict(l.split("=", 1) for l in out.splitlines() if "=" in l) if rc == 0 else {}
    return {"fstype": vals.get("TYPE"), "label": vals.get("LABEL")}


def kernel_messages(disk_name, port=None, limit=60):
    """Recent kernel lines about this disk / its USB port, each with a verdict."""
    rc, out = run(["dmesg", "-T"], timeout=10)
    if rc != 0:
        rc, out = run(["dmesg"], timeout=10)
    if rc != 0:
        return []
    pat = [rf"\b{re.escape(disk_name)}\d*\b", r"\buas\b", r"usb-storage"]
    if port:
        pat.append(rf"usb {re.escape(port)}\b")
    rx = re.compile("|".join(pat))
    lines = []
    for line in out.splitlines():
        if not rx.search(line):
            continue
        kind = ("harmless" if HARMLESS.search(line) else "error" if REAL_ERROR.search(line)
                else "link" if LINK_PROBLEM.search(line) else "info")
        lines.append({"text": line.strip(), "kind": kind})
    return lines[-limit:]


class SSDCheck:
    def __init__(self, storage, cfg):
        self.storage = storage
        self.cfg = cfg
        self.path = os.path.join(cfg["state_dir"], "ssd-check.json")
        self._lock = threading.Lock()
        self._abort = threading.Event()
        self.thread = None
        self.job = {}
        try:
            with open(self.path) as f:
                self.job = json.load(f)
            if self.job.get("state") == "running":           # cut off by a restart
                self.job["state"] = "stopped"
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ what's there
    def drive(self):
        """The SSD as the system sees it right now (cheap: lsblk + sysfs)."""
        st = self.storage
        dev = st.ssd.device if st.ssd.present else None
        if not dev:
            part = find_usb_partition(self.cfg["ssd_label"])
            dev = part["name"] if part else None
        if not dev and st.maintenance:
            dev = self.job.get("device")
        if not dev or not st.auto:
            return {"found": False, "test_mode": not st.auto}
        disk_dir = disk_sysfs(dev)
        disk = "/dev/" + os.path.basename(disk_dir) if disk_dir else dev
        info = {"found": True, "device": dev, "disk": disk,
                "mounted": bool(mount_entry(st.mountpoint)), "maintenance": st.maintenance}
        rc, out = run(["lsblk", "-J", "-b", "-p", "-o",
                       "NAME,SIZE,MODEL,VENDOR,SERIAL,TRAN,TYPE,FSTYPE,LABEL,PTTYPE,MOUNTPOINT",
                       disk], timeout=10)
        try:
            top = json.loads(out).get("blockdevices", [{}])[0]
        except (ValueError, IndexError):
            top = {}
        info.update({"size": int(top.get("size") or 0),
                     "model": " ".join(x for x in ((top.get("vendor") or "").strip(),
                                                   (top.get("model") or "").strip()) if x),
                     "serial": top.get("serial"), "table": top.get("pttype"),
                     "transport": top.get("tran"),
                     "partitions": [{"name": c.get("name"), "size": int(c.get("size") or 0),
                                     "fstype": c.get("fstype"), "label": c.get("label"),
                                     "mountpoint": c.get("mountpoint")}
                                    for c in top.get("children", [])]})
        for p in info["partitions"]:
            if not p["fstype"]:
                p.update({k: v for k, v in blkid(p["name"]).items() if v})
        info["size_text"] = media.human_bytes(info["size"])
        this = next((p for p in info["partitions"] if p["name"] == dev), {})
        info["fstype"] = this.get("fstype") or top.get("fstype")
        info["label"] = this.get("label")
        info["usb"] = _usb_info(disk_dir) if disk_dir else {}
        info["problems"] = list(st.ssd.errors.values())
        info["free_text"] = media.human_bytes(st.ssd.free) if st.ssd.present else "—"
        info["ideal"] = self._ideal(info)
        info["ssd_only"] = len(st.ssd_only()) if st.ssd.present else None
        info["other_files"] = self._other_files() if st.ssd.present else []
        return info

    def _other_files(self):
        """Things on the booth's partition that aren't the booth's (shown before
        a reformat, which erases them)."""
        skip = {self.cfg["ssd_subdir"], ".Trashes", ".Spotlight-V100", ".fseventsd",
                "System Volume Information", "lost+found", "$RECYCLE.BIN", ".TemporaryItems",
                "._.Trashes", ".DS_Store"}
        try:
            return sorted(f for f in os.listdir(self.storage.mountpoint) if f not in skip)[:20]
        except OSError:
            return []

    @staticmethod
    def _ideal(info):
        """What isn't the way the booth sets an SSD up (empty = all good)."""
        notes = []
        parts = [p for p in info.get("partitions", []) if p.get("fstype")]
        if info.get("fstype") != "exfat":
            notes.append(f"filesystem is {info.get('fstype') or 'unknown'} (exFAT works best: "
                         "Mac, Windows and the Pi)")
        if len(parts) > 1:
            notes.append(f"{len(parts)} partitions (one is simplest; often left over from a Mac "
                         "or Windows set-up)")
        if info.get("table") not in (None, "gpt"):
            notes.append(f"{info.get('table')} partition table (GPT is the modern kind)")
        return notes

    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def status(self):
        with self._lock:
            return dict(self.job)

    # --------------------------------------------------------------- running
    def start(self, kind):
        if kind not in ("check", "scan", "smart", "repair", "format"):
            return False, "unknown job"
        if self.busy():
            return False, "a check is already running"
        if not self.storage.auto:
            return False, "SSD checks only run on the Pi"
        drive = self.drive()
        if not drive.get("found"):
            return False, "no SSD found: is it plugged in?"
        if drive.get("transport") != "usb" or drive["disk"].startswith("/dev/mmcblk"):
            return False, "that isn't a USB drive; not touching it"
        if any(p.get("mountpoint") in ("/", "/boot", "/boot/firmware")
               for p in drive["partitions"]):
            return False, "that drive holds the Pi's system; not touching it"
        self._abort.clear()
        with self._lock:
            self.job = {"kind": kind, "state": "running", "progress": 0.0, "step": "Starting…",
                        "results": [], "started": time.time(), "device": drive["device"],
                        "disk": drive["disk"]}
        self.thread = threading.Thread(target=self._run, args=(kind, drive), name="ssd-" + kind,
                                       daemon=True)
        self.thread.start()
        return True, ""

    def cancel(self):
        self._abort.set()

    def _set(self, progress=None, step=None):
        with self._lock:
            if progress is not None:
                self.job["progress"] = round(max(0.0, min(1.0, progress)), 3)
            if step is not None:
                self.job["step"] = step

    def _result(self, level, title, detail="", **extra):
        """level: ok / warn / bad / info"""
        with self._lock:
            self.job["results"].append({"level": level, "title": title, "detail": detail, **extra})
        (log.warning if level == "bad" else log.info)("SSD %s: %s %s", level, title, detail)

    def _save(self):
        try:
            media.write_atomic(self.path, json.dumps(self.job, indent=1))
        except OSError:
            pass

    def _run(self, kind, drive):
        state, summary = "done", ""
        try:
            summary = getattr(self, "_job_" + kind)(drive) or ""
            if self._abort.is_set():
                state, summary = "stopped", "Stopped. Nothing was changed."
        except _Refused as e:
            state, summary = "refused", str(e)
        except Exception as e:                       # report it, keep the booth going
            log.exception("SSD %s failed", kind)
            state, summary = "failed", str(e)
        finally:
            if self.storage.maintenance:
                self.storage.end_maintenance(fresh=False)
        with self._lock:
            levels = [r["level"] for r in self.job["results"]]
            self.job.update({"state": state, "finished": time.time(), "progress": 1.0,
                             "step": "", "summary": summary,
                             "verdict": "bad" if "bad" in levels else
                             "warn" if "warn" in levels else "ok"})
        self._save()

    # --------------------------------------------------------------- the jobs
    def _job_check(self, drive):
        """Quick check (1-3 min): connection, kernel messages, filesystem, speed."""
        st = self.storage
        disk_name = os.path.basename(drive["disk"])
        self._set(0.05, "Looking at the connection")
        self._result("info", "Drive", f"{drive.get('model') or 'USB drive'}, "
                     f"{drive['size_text']}, {drive.get('fstype') or '?'}"
                     + (f" \"{drive['label']}\"" if drive.get("label") else ""))
        usb = drive.get("usb", {})
        speed = usb.get("speed")
        if speed and speed < 5000:
            self._result("warn", "Connected at USB 2 speed",
                         "Plug the SSD into a blue (USB 3) port on the Pi, and use a USB 3 "
                         "cable. USB 2 works, but is slow and some adapters are unreliable on it.")
        elif speed:
            self._result("ok", "USB 3 connection", f"{speed // 1000} Gb/s via "
                         f"{usb.get('adapter') or 'adapter'} ({usb.get('driver', '?')})")
        if st.undervolt:
            self._result("bad", "The Pi's power supply is too weak",
                         "Under-voltage since start-up. Drives drop out and files get damaged: "
                         "use the official 27 W (5 V 5 A) supply, or a powered USB hub.")
        else:
            self._result("ok", "Power", "no under-voltage since start-up")
        for note in drive["ideal"]:
            self._result("info", "Not the booth's usual set-up", note + ". Reformatting fixes it.")

        self._set(0.15, "Reading the system's messages about the drive")
        msgs = kernel_messages(disk_name, usb.get("port"))
        # Shown for interest only: the drive is judged by whether files copy and check out.
        blips = [m for m in msgs if m["kind"] in ("error", "link")]
        self._result("info", "System messages", (f"{len(blips)} connection blips logged since "
                     "start-up (the system retries them). " if blips else "Nothing unusual. ") +
                     "Not used to judge the drive: what counts is whether files save and check "
                     "out, below.", lines=[m["text"] for m in msgs[-25:]])
        self._recent_recordings()
        if self._abort.is_set():
            return

        self._fsck(drive, repair=False, base=0.25, span=0.35)
        if self._abort.is_set():
            return
        self._speed_test(base=0.65, span=0.35)
        return self._verdict_text()

    def _recent_recordings(self):
        """Did the last few videos save and check out on the SSD?"""
        st = self.storage
        names = st.manifest.names()[-10:]
        copies = [((st.manifest.entry(n) or {}).get("copies") or {}).get("ssd") or {}
                  for n in names]
        bad = [n for n, c in zip(names, copies) if c.get("status") in ("bad", "damaged")]
        good = [n for n, c in zip(names, copies) if c.get("status") == "verified"]
        if "last_save" in st.ssd.errors or bad:
            self._result("bad", "Recordings aren't saving properly on the SSD",
                         "New recordings go to the microSD until a video saves on the SSD "
                         "again." + (f" Failed: {', '.join(bad[-3:])}" if bad else ""))
        elif good:
            self._result("ok", "Recordings are saving on the SSD",
                         f"the last {len(good)} videos on it saved and checked out")
        else:
            self._result("info", "No recent recordings on the SSD to check", "")

    def _fsck(self, drive, repair, base, span):
        st = self.storage
        dev, fs = drive["device"], drive.get("fstype")
        tool = {"exfat": (["fsck.exfat", "-y" if repair else "-n"], "exFAT"),
                "vfat": (["fsck.vfat", "-a" if repair else "-n"], "FAT"),
                "ext4": (["e2fsck", "-fy" if repair else "-fn"], "ext4"),
                "ntfs": (["ntfsfix"] + ([] if repair else ["-n"]), "NTFS")}.get(fs)
        if not tool or not shutil.which(tool[0][0]):
            self._result("info", "Filesystem not checked", f"no checker for {fs or 'it'} here")
            return None
        self._set(base, "Taking the SSD offline (recordings go to the microSD meanwhile)")
        ok, why = st.begin_maintenance("repair" if repair else "filesystem check")
        if not ok:
            self._result("warn", "Filesystem not checked", why)
            return None
        try:
            self._set(base + span * 0.2, ("Repairing" if repair else "Checking") +
                      f" the {tool[1]} filesystem")
            rc, out = run(tool[0] + [dev], timeout=1800)
            tail = "\n".join(out.splitlines()[-12:])
            if rc == 0:
                self._result("ok", f"{tool[1]} filesystem " + ("repaired" if repair else "is clean"),
                             "", lines=tail.splitlines())
            elif repair and rc in (1, 2):           # fixed something
                self._result("ok", f"{tool[1]} filesystem repaired", "problems were fixed",
                             lines=tail.splitlines())
            else:
                self._result("bad", f"{tool[1]} filesystem has problems" if not repair
                             else "Couldn't repair the filesystem",
                             "Tap Repair to fix them (videos stay where they are), or reformat."
                             if not repair else "Reformat the SSD (videos are copied to the "
                             "microSD first).", lines=tail.splitlines())
            return rc
        finally:
            self._set(base + span * 0.9, "Bringing the SSD back online")
            st.end_maintenance(fresh=repair)

    def _speed_test(self, base, span):
        st = self.storage
        end = time.monotonic() + 30
        while not st.ssd.present and time.monotonic() < end:
            time.sleep(0.5)
        if not st.ssd.present:
            self._result("bad", "The SSD didn't come back online", "Unplug it and plug it in again.")
            return
        while st.recording.is_set() and not self._abort.is_set():
            self._set(step="Waiting: someone is recording")
            time.sleep(1)
        free = st.ssd.free or 0
        size_mb = int(min(SPEED_TEST_MB, max(0, free - st.min_free * 2) // (1 << 20)))
        if size_mb < 16:
            self._result("info", "Speed test skipped", "not enough free space")
            return
        path = os.path.join(st.ssd.video_dir, ".booth-speed-test")
        block = os.urandom(CHUNK)
        h = hashlib.sha256()
        try:
            self._set(base, f"Writing {size_mb} MB test file")
            t = time.monotonic()
            with open(path, "wb") as f:
                for i in range(size_mb * (1 << 20) // CHUNK):
                    if self._abort.is_set():
                        return
                    if st.recording.is_set():         # the recording gets the drive
                        self._result("info", "Speed test skipped", "someone started recording")
                        return
                    b = i.to_bytes(8, "big") + block[8:]
                    f.write(b)
                    h.update(b)
                    self._set(base + span * 0.5 * i / (size_mb / 4))
                f.flush()
                os.fsync(f.fileno())
            wtime = time.monotonic() - t
            media.drop_cache(path)
            self._set(base + span * 0.5, "Reading it back")
            t = time.monotonic()
            back = media.sha256_file(path, abort=self._abort)
            rtime = time.monotonic() - t
        except media.Aborted:
            return
        except OSError as e:
            self._result("bad", "Writing to the SSD failed", str(e))
            return
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if back != h.hexdigest():
            self._result("bad", "Data read back was different from what was written",
                         "The drive or adapter is corrupting data. Don't use it for videos.")
            return
        w, r = size_mb / max(wtime, 1e-3), size_mb / max(rtime, 1e-3)
        level = "ok" if w >= 20 else "warn"
        self._result(level, "Write/read test passed",
                     f"{size_mb} MB written and read back identical. Write {w:.0f} MB/s, "
                     f"read {r:.0f} MB/s" + ("" if w >= 20 else
                                              " (slow: check it's on a blue USB 3 port)"))

    def _verdict_text(self):
        levels = [r["level"] for r in self.job["results"]]
        if "bad" in levels:
            return "Problems found: see below."
        if "warn" in levels:
            return "The SSD works, with warnings below."
        return "The SSD is healthy."

    def _job_scan(self, drive):
        """Reads every sector of the whole SSD (read-only). Slow; pauses while
        someone is recording so the recording always gets the drive first."""
        disk, size = drive["disk"], drive["size"]
        if not size:
            raise _Refused("couldn't tell the drive's size")
        bad, pos, t0, done_bytes = [], 0, time.monotonic(), 0
        fd = os.open(disk, os.O_RDONLY)
        try:
            while pos < size and not self._abort.is_set():
                if self.storage.recording.is_set():
                    self._set(step="Paused: someone is recording")
                    time.sleep(1)
                    continue
                try:
                    os.posix_fadvise(fd, pos, CHUNK, os.POSIX_FADV_DONTNEED)
                    n = len(os.pread(fd, min(CHUNK, size - pos), pos))
                    if n <= 0:
                        break
                except OSError as e:
                    bad.append({"at": pos, "error": str(e)})
                    n = CHUNK
                    if len(bad) > 50:
                        break
                pos += n
                done_bytes += n
                rate = done_bytes / max(1e-3, time.monotonic() - t0)
                left = (size - pos) / max(rate, 1)
                self._set(pos / size, f"Reading {media.human_bytes(pos)} of {drive['size_text']} "
                          f"· {rate / 1e6:.0f} MB/s · about {int(left // 60) + 1} min left")
        finally:
            os.close(fd)
        if self._abort.is_set():
            return None
        rate = done_bytes / max(1e-3, time.monotonic() - t0) / 1e6
        if bad:
            self._result("bad", f"{len(bad)} areas of the drive couldn't be read",
                         "The SSD (or adapter) is failing. Copy your videos off and replace it.",
                         lines=[f"at {b['at'] / 1e9:.2f} GB: {b['error']}" for b in bad[:20]])
            return "Unreadable areas found."
        self._result("ok", "Every part of the drive read back fine",
                     f"{drive['size_text']} read at {rate:.0f} MB/s")
        return "Full scan passed: no unreadable areas."

    def _job_smart(self, drive):
        if not shutil.which("smartctl"):
            raise _Refused("smartctl isn't installed (sudo apt install smartmontools)")
        self._set(0.2, "Asking the drive for its health report")
        self.storage.forgive_io_errors(120)
        out, ok = "", False
        for extra in (["-d", "sat"], ["-d", "auto"], []):
            rc, out = run(["smartctl", "-a"] + extra + [drive["disk"]], timeout=90)
            if rc >= 0 and not rc & 3 and "SMART" in out:
                ok = True
                break
        self.storage.forgive_io_errors(120)
        if not ok:
            self._result("info", "This USB adapter doesn't pass SMART health reports through",
                         "Common with USB-SATA adapters and not a fault. Use Quick check and "
                         "Full scan instead.", lines=out.splitlines()[-8:])
            return "No SMART report available through this adapter."
        health = re.search(r"overall-health self-assessment test result:\s*(\w+)|"
                           r"SMART Health Status:\s*(\w+)", out)
        verdict = (health.group(1) or health.group(2)) if health else "?"
        keys = re.compile(r"Reallocated_Sector|Current_Pending|Offline_Uncorrectable|"
                          r"Wear_Leveling|Percent|Media_Wearout|Power_On_Hours|Temperature|"
                          r"Media and Data Integrity|Available Spare|Unsafe Shutdowns|"
                          r"Reported_Uncorrect|CRC_Error", re.I)
        lines = [l.strip() for l in out.splitlines() if keys.search(l)][:20]
        good = verdict.upper() in ("PASSED", "OK")
        self._result("ok" if good else "bad", f"SMART health: {verdict}",
                     "The drive's own self-assessment." if good else
                     "The drive itself reports it is failing: copy your videos off and replace it.",
                     lines=lines)
        return f"SMART health: {verdict}"

    def _job_repair(self, drive):
        rc = self._fsck(drive, repair=True, base=0.1, span=0.85)
        if rc is None:
            return "Not repaired."
        return self._verdict_text()

    def _job_format(self, drive):
        """New GPT partition table, one exFAT partition named CONFESSIONS."""
        st = self.storage
        for tool in ("wipefs", "sfdisk", MKFS[0]):
            if not shutil.which(tool):
                raise _Refused(f"{tool} isn't installed (sudo apt install exfatprogs util-linux)")
        # 1. every video must also be on the microSD
        self._set(0.02, "Making sure every video is also on the microSD")
        ok, msg = st.copy_ssd_only_to_sd(
            progress=lambda p, s: self._set(0.02 + 0.4 * p, s), abort=self._abort)
        if not ok:
            raise _Refused(msg + " The SSD was not touched.")
        if self._abort.is_set():
            return None
        left = st.ssd_only()
        if left:
            raise _Refused(f"{len(left)} videos are still only on the SSD; not formatting.")
        self._result("ok", "Videos safe", msg)
        # 2. offline, then the new layout
        self._set(0.45, "Taking the SSD offline")

        def still_safe():               # checked again with the SSD still mounted
            n = len(st.ssd_only())
            return f"{n} videos are only on the SSD; not formatting" if n else None
        ok, why = st.begin_maintenance("reformat", guard=still_safe)
        if not ok:
            raise _Refused(why + ". The SSD was not touched.")
        disk = drive["disk"]
        for p in drive["partitions"]:
            if p.get("mountpoint"):
                run(["umount", p["mountpoint"]], timeout=30)
        log.warning("REFORMATTING SSD %s (%s)", disk, drive.get("model"))
        self._set(0.5, "Clearing the old layout")
        rc, out = run(["wipefs", "-a", disk], timeout=60)
        if rc != 0:
            raise RuntimeError("wipefs: " + out)
        self._set(0.6, "Making one partition")
        r = subprocess.run(["sfdisk", "--wipe", "always", disk],
                           input=f"label: gpt\nstart=2048, type={EXFAT_GUID}\n".encode(),
                           capture_output=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError("sfdisk: " + r.stderr.decode(errors="replace")[-300:])
        for cmd in (["partprobe", disk], ["partx", "-u", disk], ["udevadm", "settle"]):
            if shutil.which(cmd[0]):          # make the system see the new partition
                run(cmd, timeout=30)
        part = None
        for _ in range(20):
            rc, out = run(["lsblk", "-J", "-p", "-o", "NAME,TYPE", disk], timeout=10)
            try:
                kids = json.loads(out)["blockdevices"][0].get("children", [])
                part = kids[0]["name"] if kids else None
            except (ValueError, KeyError, IndexError):
                part = None
            if part and os.path.exists(part):
                break
            time.sleep(0.5)
        if not part:
            raise RuntimeError("the new partition didn't appear")
        self._set(0.7, "Formatting it as exFAT")
        rc, out = run(MKFS + [part], timeout=600)
        if rc != 0:
            raise RuntimeError(f"{MKFS[0]}: " + out[-300:])
        os.sync()
        self._result("ok", "Reformatted", f"One exFAT partition \"{LABEL}\" filling the "
                     f"{drive['size_text']} drive (works on Mac, Windows and the Pi).")
        self._set(0.85, "Bringing the SSD back online")
        st.end_maintenance(fresh=True)
        end = time.monotonic() + 30
        while not st.ssd.present and time.monotonic() < end:
            time.sleep(0.5)
            st.refresh()
        if not st.ssd.present:
            self._result("bad", "The new SSD didn't mount", "Unplug it and plug it in again.")
            return "Formatted, but it didn't come back online."
        self._speed_test(base=0.88, span=0.12)
        return ("Reformatted. The booth is now copying the videos back onto the SSD "
                "in the background.")


class _Refused(Exception):
    pass
