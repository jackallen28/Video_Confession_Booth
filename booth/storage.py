"""The two drives (USB SSD and microSD), their health, and the background
worker that verifies every recording and keeps both drives holding a good copy.

After each recording, in the background:
  * every copy is read back from its drive (cache dropped first), its SHA-256
    taken and all of its audio/video packets run through ffmpeg;
  * the SSD and microSD copies must hold identical audio/video;
  * a copy that is missing (SSD unplugged at the time) or damaged is replaced
    by a byte-for-byte copy of the good one, checked by SHA-256;
  * a SHA256SUMS file is kept next to the videos on both drives
    (check on a computer with: sha256sum -c SHA256SUMS).

Every few seconds the drives are checked: SSD still there (remounted if it was
unplugged and comes back), not switched to read-only, no new I/O errors, free
space; every few minutes a test file is written and read back; when idle, one
saved video is re-read and its checksum compared, and SMART health is read if
the SSD's USB adapter supports it.
"""

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from . import media

log = logging.getLogger("booth.storage")

VIDEO_EXT = ".mp4"
DELETED_DIR = "DELETED"     # guests' "deleted" clips are moved here, never erased
PART_EXT = ".ts.part"       # while recording (MPEG-TS, survives power cuts)


# --------------------------------------------------------------------- manifest
class Manifest:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"version": 2, "recordings": {}}
        try:
            with open(path) as f:
                self.data = json.load(f)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.error("manifest unreadable (%s), starting a new one", e)
            try:
                os.replace(path, path + f".broken-{int(time.time())}")
            except OSError:
                pass
        self.data.setdefault("recordings", {})

    def save(self):
        with self.lock:
            for attempt in range(5):
                try:
                    blob = json.dumps(self.data, indent=1, sort_keys=True)
                    break
                except RuntimeError:     # changed by the worker mid-dump; try again
                    time.sleep(0.01)
            media.write_atomic(self.path, blob)


    def entry(self, name, create=False):
        with self.lock:
            e = self.data["recordings"].get(name)
            if e is None and create:
                e = self.data["recordings"][name] = {"copies": {}}
            return e

    def names(self):
        """Recordings that still exist (not deleted by a guest)."""
        with self.lock:
            return sorted(n for n, e in self.data["recordings"].items()
                          if e.get("state") != "deleted")


# ------------------------------------------------------------------------ drives
class Drive:
    def __init__(self, name, video_dir):
        self.name = name
        self.video_dir = video_dir
        self.present = False
        self.free = None
        self.total = None
        self.device = None
        self.errors = {}         # kind -> message; any entry = drive not trusted
        self.notes = {}          # shown on the web page only (don't stop recording here)
        self.low = False

    @property
    def level(self):
        if not self.present:
            return "missing"
        if self.errors:
            return "error"
        if self.low:
            return "warn"
        return "ok"

    def usable(self, min_free, copying=False):
        """copying: a background copy is also how a drive whose last recording
        failed gets tested again (a good copy clears that)."""
        errs = [k for k in self.errors if not (copying and k == "last_save")]
        return self.present and not errs and (self.free or 0) > min_free

    def path(self, name):
        return os.path.join(self.video_dir, name)

    def snapshot(self):
        return {"present": self.present, "level": self.level, "free": self.free,
                "low": self.present and self.low, "errors": dict(self.errors),
                "notes": dict(self.notes)}


def _mounts():
    out = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    unesc = lambda s: re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), s)
                    out.append((unesc(p[0]), unesc(p[1]), p[2], p[3].split(",")))
    except OSError:
        pass
    return out


def mount_entry(mountpoint):
    mp = os.path.realpath(mountpoint)
    for dev, where, fstype, opts in _mounts():
        if where == mp:
            return dev, fstype, opts
    return None


def mount_of(path):
    """The mount that holds path (longest matching mountpoint)."""
    path = os.path.realpath(path)
    best = None
    for dev, where, fstype, opts in _mounts():
        if path == where or path.startswith(where.rstrip("/") + "/"):
            if best is None or len(where) > len(best[1]):
                best = (dev, where, fstype, opts)
    return best


def find_usb_partition(label=""):
    try:
        r = subprocess.run(["lsblk", "-J", "-b", "-p", "-o",
                            "NAME,LABEL,FSTYPE,TRAN,TYPE,SIZE,MOUNTPOINT"],
                           capture_output=True, timeout=10)
        tree = json.loads(r.stdout or b"{}").get("blockdevices", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    found = []
    for disk in tree:
        if disk.get("tran") != "usb" or disk.get("type") != "disk":
            continue
        for part in [disk] + disk.get("children", []):
            fs = part.get("fstype")
            if not fs or fs in ("swap", "LVM2_member", "crypto_LUKS", "linux_raid_member"):
                continue
            mp = part.get("mountpoint")
            if mp in ("/", "/boot", "/boot/firmware"):
                continue
            found.append(part)
    if not found:
        return None
    if label:
        for p in found:
            if (p.get("label") or "") == label:
                return p
    return max(found, key=lambda p: int(p.get("size") or 0))


def disk_sysfs(device):
    """/dev/sda1 -> /sys/.../block/sda (the whole disk)."""
    try:
        p = os.path.realpath(f"/sys/class/block/{os.path.basename(os.path.realpath(device))}")
    except OSError:
        return None
    if os.path.exists(os.path.join(p, "partition")):
        p = os.path.dirname(p)
    return p if os.path.isdir(p) else None


def read_ioerr(device):
    d = disk_sysfs(device)
    if not d:
        return None
    try:
        with open(os.path.join(d, "device", "ioerr_cnt")) as f:
            return int(f.read().strip(), 16)
    except (OSError, ValueError):
        return None


def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).decode(errors="replace").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


# ----------------------------------------------------------------------- storage
class Storage:
    def __init__(self, cfg):
        self.cfg = cfg
        self.auto = cfg["ssd_mode"] == "auto"
        self.mountpoint = cfg["ssd_mount"]
        self.ssd = Drive("ssd", os.path.join(cfg["ssd_mount"], cfg["ssd_subdir"]))
        self.sd = Drive("sd", cfg["sd_dir"])
        self.drives = [self.ssd, self.sd]
        os.makedirs(cfg["state_dir"], exist_ok=True)
        self.manifest = Manifest(os.path.join(cfg["state_dir"], "manifest.json"))
        self.recording = threading.Event()   # set by the app while recording
        self.active_name = None
        self.held = set()            # recordings waiting for the guest's keep/delete
        self._q = queue.Queue()
        self._pending = set()
        self._plock = threading.Lock()
        self.busy = False
        self.stopping = threading.Event()
        self.paused = threading.Event()      # software update: no new background jobs
        self.wiping = False                  # "wipe all videos" in progress
        self._hlock = threading.Lock()       # health checks vs. eject
        self._ioerr = {}
        self._no_smart = False
        self.maintenance = None      # SSD tab: SSD taken offline (check/repair/format)
        self._forgive_until = 0.0    # I/O errors right after a SMART query aren't faults
        self._last = {"write": 0.0, "scrub": time.monotonic(), "smart": 0.0}
        self._scrub_pos = 0
        self._ssd_device = None
        self.undervolt = False       # Pi power supply too weak (risk of corruption)

    @property
    def min_free(self):
        return int(self.cfg["min_free_mb"]) * 1_000_000

    @property
    def low_free(self):       # can change from the web page
        return int(self.cfg["low_free_mb"]) * 1_000_000

    def video_path(self, name):
        """Path of the best copy to read (SSD first), or None. Guest-deleted
        clips are found in the DELETED folders."""
        e = self.manifest.entry(name) or {}
        for d in self.drives:
            if not d.present:
                continue
            p = self.deleted_path(d, name) if e.get("state") == "deleted" else d.path(name)
            if os.path.exists(p):
                return p
        return None

    def deleted_names(self):
        with self.manifest.lock:
            return sorted(n for n, e in self.manifest.data["recordings"].items()
                          if e.get("state") == "deleted")

    # ------------------------------------------------------------ lifecycle
    def start(self):
        os.makedirs(self.sd.video_dir, exist_ok=True)
        self.refresh(first=True)
        self.queue("__reconcile__")
        threading.Thread(target=self._worker, name="storage", daemon=True).start()
        threading.Thread(target=self._health_loop, name="health", daemon=True).start()

    def queue(self, item):
        with self._plock:
            if item in self._pending:
                return
            self._pending.add(item)
        self._q.put(item)

    def snapshot(self):
        with self._plock:
            pending = len(self._pending)
        unsafe = 0
        with self.manifest.lock:
            for e in self.manifest.data["recordings"].values():
                if e.get("state") == "damaged":
                    unsafe += 1
        return {"ssd": self.ssd.snapshot(), "sd": self.sd.snapshot(),
                "pending": pending + (1 if self.busy else 0), "damaged": unsafe,
                "undervolt": self.undervolt}

    # --------------------------------------------------------- recording io
    MODES = {"both": ("ssd", "sd"), "ssd_first": ("ssd", "sd"), "sd_first": ("sd", "ssd"),
             "ssd_only": ("ssd",), "sd_only": ("sd",)}

    @property
    def mode(self):
        m = self.cfg.get("storage_mode", "both")
        return m if m in self.MODES else "both"

    @property
    def primary(self):
        """Drives every video should end up on in this mode."""
        order = self.MODES[self.mode]
        return set(order) if self.mode == "both" else {order[0]}

    def targets(self):
        """Where the next recording goes: both drives at once, or the first
        drive in the mode's order that is connected, has room and saved the
        last recording properly."""
        with self._hlock:
            usable = {d.name: d for d in self.drives if d.usable(self.min_free)}
        order = self.MODES[self.mode]
        if self.mode == "both":
            return {n: usable[n] for n in order if n in usable}
        for n in order:
            if n in usable:
                return {n: usable[n]}
        return {}

    def has_room(self, names):
        """Is a recording going to these drives still OK? Stops (and saves)
        a recording before its drive is completely full."""
        with self._hlock:
            return any(d.present and not d.errors and (d.free or 0) > self.min_free / 2
                       for d in self.drives if d.name in names)

    def _copy_wanted(self, d, name, copies):
        """Should drive d hold a copy of this video? Its mode's main drive(s)
        always; any other drive keeps a copy it already has."""
        return d.name in self.primary or d.name in copies or os.path.exists(d.path(name))

    def new_name(self):
        """Just the date and time, e.g. 2026-11-11_21-04-33.mp4 (a _2 is added
        in the rare case that name is taken, e.g. after the clock was reset)."""
        base = time.strftime("%Y-%m-%d_%H-%M-%S")
        n = 1
        while True:
            name = base + (f"_{n}" if n > 1 else "") + VIDEO_EXT
            stem = name[:-len(VIDEO_EXT)]
            taken = self.manifest.entry(name) is not None or any(
                os.path.exists(d.path(name)) or os.path.exists(d.path(stem + PART_EXT))
                for d in self.drives if d.present)
            if not taken:
                return name
            n += 1

    @staticmethod
    def part_name(name):
        return name[:-len(VIDEO_EXT)] + PART_EXT

    def finalize(self, result):
        """Called when ffmpeg has finished: turn each recorded copy into a normal
        .mp4 and check it plays. Returns True if at least one copy plays.
        The full background check starts with accept() (after the guest had
        the chance to delete it)."""
        name = result["name"]
        outcome = {}

        def one(drive_name, part):
            final = part[:-len(PART_EXT)] + VIDEO_EXT
            if not os.path.exists(part) or os.path.getsize(part) == 0:
                log.error("%s: no file on %s", name, drive_name)
                self._last_save(drive_name, False)
                return
            try:
                media.fsync_file(part)
            except OSError as e:
                log.error("%s: %s copy unreadable: %s", name, drive_name, e)
                self._last_save(drive_name, False)
                return
            ok, why = media.remux_to_mp4(part, final, when=result.get("started"))
            if not ok:
                log.error("%s: MP4 on %s failed (%s); keeping the raw .ts", name, drive_name, why)
                self._last_save(drive_name, False)
                try:
                    os.replace(part, part[:-len(".part")])
                except OSError:
                    pass
                return
            p = media.probe(final)
            log.info("%s on %s: %s", name, drive_name, p)
            outcome[drive_name] = p["ok"]
            if not p["ok"]:
                self._last_save(drive_name, False)
            if p["ok"]:
                finals.append(final)
                try:
                    os.remove(part)
                except OSError:
                    pass

        self.held.add(name)
        finals = []
        threads = [threading.Thread(target=one, args=(k, v)) for k, v in result["parts"].items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        iphone = media.iphone_issues(finals[0]) if finals else ["not saved"]
        with self.manifest.lock:
            e = self.manifest.entry(name, create=True)
            e.update({"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "seconds": round(result["duration"], 1),
                      "frames": result["frames"], "dropped_frames": result["dropped"],
                      "recorded_to": sorted(result["parts"]), "state": "review",
                      "iphone": iphone})
            if iphone:
                log.warning("%s may not play on iPhone/Safari: %s", name, "; ".join(iphone))
            self.manifest.save()
        return any(outcome.values())

    def accept(self, name):
        """Guest kept it: start the full check / mirroring."""
        self.held.discard(name)
        self.queue(name)

    def delete(self, name):
        """Guest deleted it: move every copy into the DELETED folder on its drive.
        Nothing is erased; it can be restored from the web page."""
        stem = name[:-len(VIDEO_EXT)]
        with self.manifest.lock:
            e = self.manifest.entry(name, create=True)
            e["state"] = "deleted"
            e["deleted"] = time.strftime("%Y-%m-%d %H:%M:%S")
            e["copies"] = {}
            self.manifest.save()
        for d in self.drives:
            if d.present:
                for f in (name, stem + PART_EXT, stem + ".ts"):
                    self._move(d, f, to_deleted=True)
        self.held.discard(name)
        self._write_sums()

    @staticmethod
    def _is_booth_file(f):
        """Files the booth itself made in a video folder (nothing else is touched)."""
        return (f.endswith((VIDEO_EXT, ".ts", ".part", ".tmp")) or ".mp4.bad-" in f
                or f in ("SHA256SUMS", "booth-manifest.json", ".booth-health-check")
                or f.startswith(".tmp-"))

    def wipe_all(self, thumbs_dir=None):
        """PERMANENTLY delete every video (and the DELETED folders) on every
        connected drive. Only when nothing is being recorded. Returns a report."""
        self.wiping = True
        self.paused.set()
        try:
            end = time.monotonic() + 120
            while self.busy and time.monotonic() < end:
                time.sleep(0.2)                    # let the current check/copy finish
            while True:                            # forget queued background jobs
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
            with self._plock:
                self._pending.clear()
            report = {"files": 0, "bytes": 0, "drives": [], "skipped": []}
            with self._hlock:
                for d in self.drives:
                    if not d.present:
                        report["skipped"].append(d.name)
                        continue
                    for folder in (os.path.join(d.video_dir, DELETED_DIR), d.video_dir):
                        try:
                            names = os.listdir(folder)
                        except OSError:
                            continue
                        for f in names:
                            p = os.path.join(folder, f)
                            if not os.path.isfile(p) or not self._is_booth_file(f):
                                continue
                            if self.active_name and f.startswith(self.active_name[:-4]):
                                continue           # never touch a recording in progress
                            try:
                                size = os.path.getsize(p)
                                os.remove(p)
                                report["files"] += 1
                                report["bytes"] += size
                            except OSError as ex:
                                log.error("wipe: could not delete %s: %s", p, ex)
                        media.fsync_dir(folder)
                    try:
                        os.rmdir(os.path.join(d.video_dir, DELETED_DIR))   # only if empty
                    except OSError:
                        pass
                    report["drives"].append(d.name)
                with self.manifest.lock:
                    self.manifest.data["recordings"] = {}
                    self.manifest.save()
            if thumbs_dir and os.path.isdir(thumbs_dir):
                shutil.rmtree(thumbs_dir, ignore_errors=True)
            os.sync()
            # tell the drives the space is free (SSDs and SD cards), where supported
            if shutil.which("fstrim") and self.auto:
                for d in self.drives:
                    if d.present:
                        run(["fstrim", os.path.dirname(d.video_dir) if d is self.ssd
                             else "/"], timeout=120)
            log.warning("WIPED all videos: %d files, %s (%s)", report["files"],
                        media.human_bytes(report["bytes"]), ", ".join(report["drives"]))
            return report
        finally:
            self.paused.clear()
            self.wiping = False

    def restore(self, name):
        """Bring a guest-deleted clip back (web page)."""
        e = self.manifest.entry(name)
        if not e or e.get("state") != "deleted":
            return False
        found = False
        for d in self.drives:
            if d.present:
                found |= self._move(d, name, to_deleted=False)
        if not found:
            return False
        with self.manifest.lock:
            e["state"] = "checking"
            e["restored"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.manifest.save()
        log.warning("restored %s", name)
        self.queue(name)
        return True

    def deleted_path(self, d, name):
        return os.path.join(d.video_dir, DELETED_DIR, name)

    def _move(self, d, f, to_deleted):
        """Rename between the video folder and its DELETED folder (same drive,
        so it's instant and can't lose data). Never overwrites anything."""
        src = self.deleted_path(d, f) if not to_deleted else d.path(f)
        dst = d.path(f) if not to_deleted else self.deleted_path(d, f)
        if not os.path.exists(src):
            return False
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                base, ext = os.path.splitext(dst)
                dst = f"{base}-{int(time.time())}{ext}"
            os.replace(src, dst)
            media.fsync_dir(os.path.dirname(dst))
            media.fsync_dir(os.path.dirname(src))
            log.info("%s %s on %s", "moved to DELETED:" if to_deleted else "restored:", f, d.name)
            return True
        except OSError as ex:
            log.error("could not move %s on %s: %s", f, d.name, ex)
            return False

    # ---------------------------------------------------------------- health
    def _health_loop(self):
        while not self.stopping.wait(self.cfg["health_interval"]):
            try:
                self.refresh()
            except Exception:
                log.exception("health check failed")

    def refresh(self, first=False):
        with self._hlock:
            if self.stopping.is_set():
                return
            was = self.ssd.present
            self._check_ssd()
            self._check_drive(self.sd)
            if self.ssd.present:
                self._check_drive(self.ssd)
            self._check_power()
            idle = not self.recording.is_set()
            now = time.monotonic()
            if idle and self.auto and self.ssd.present and self.cfg["smart_interval"] and \
                    now - self._last["smart"] > self.cfg["smart_interval"]:
                self._last["smart"] = now
                self._smart()
        if not first and self.ssd.present and not was:
            log.info("SSD connected")
            self.queue("__reconcile__")
        if idle and time.monotonic() - self._last["scrub"] > self.cfg["scrub_interval"]:
            self._last["scrub"] = time.monotonic()
            self.queue("__scrub__")

    def _check_ssd(self):
        d = self.ssd
        if self.maintenance:              # being checked/formatted from the SSD tab
            d.present = False
            d.free = d.total = None
            return
        if not self.auto:
            d.present = os.path.isdir(self.mountpoint)
            if d.present:
                os.makedirs(d.video_dir, exist_ok=True)
            return
        entry = mount_entry(self.mountpoint)
        if entry and not os.path.exists(entry[0]):
            log.error("SSD %s disappeared, cleaning up its mount", entry[0])
            run(["umount", "-l", self.mountpoint])
            entry = None
        if entry is None:
            part = find_usb_partition(self.cfg["ssd_label"])
            if part:
                entry = self._mount(part)
        if entry is None:
            if d.present:
                log.error("SSD missing")
            d.present = False
            d.free = d.total = None
            return
        dev, fstype, opts = entry
        if dev != self._ssd_device:     # new or re-plugged drive: forget old errors
            self._ssd_device = dev
            d.errors.clear()
            self._ioerr[dev] = read_ioerr(dev)
            self._no_smart = False
            log.info("SSD %s (%s) mounted at %s", dev, fstype, self.mountpoint)
        d.present = True
        d.device = dev
        if "ro" in opts:
            d.errors["readonly"] = "drive went read-only (filesystem errors?)"
        n = read_ioerr(dev)
        base = self._ioerr.get(dev)
        if n is not None and base is not None and n < base:    # drive re-plugged: count reset
            self._ioerr[dev] = base = n
            d.notes.pop("io", None)
        if n is not None and base is not None and n > base and \
                time.monotonic() < self._forgive_until:
            self._ioerr[dev] = base = n       # caused by the SMART query just made
        if n is not None and base is not None and n > base:
            # Only noted: whether the SSD is used depends on whether recordings
            # actually save on it (see _last_save). USB adapters log harmless ones.
            msg = f"{n - base} I/O errors counted by the system"
            if d.notes.get("io") != msg:            # log when it changes, not every 15 s
                log.warning("SSD: %s (not a problem unless recordings fail to save)", msg)
            d.notes["io"] = msg
        try:
            os.makedirs(d.video_dir, exist_ok=True)
        except OSError as e:
            d.errors["folder"] = str(e)

    def _check_power(self):
        """Pi under-voltage (since boot) - a weak supply corrupts SD cards and SSDs."""
        if not shutil.which("vcgencmd"):
            return
        rc, out = run(["vcgencmd", "get_throttled"], timeout=5)
        m = re.search(r"0x([0-9a-fA-F]+)", out)
        if rc == 0 and m:
            bits = int(m.group(1), 16)
            uv = bool(bits & 0x10001)
            if uv and not self.undervolt:
                log.error("UNDER-VOLTAGE: use a stronger power supply / powered USB hub (%s)", out)
            self.undervolt = uv

    def _mount(self, part):
        dev, fs = part["name"], part.get("fstype")
        os.makedirs(self.mountpoint, exist_ok=True)
        if part.get("mountpoint"):          # mounted somewhere else (desktop)
            log.info("SSD already mounted at %s, moving it", part["mountpoint"])
            run(["umount", part["mountpoint"]])
        attempts = [["-t", "ntfs3"], []] if fs == "ntfs" else [[]]
        for extra in attempts:
            rc, out = run(["mount", "-o", "noatime"] + extra + [dev, self.mountpoint])
            if rc == 0:
                return mount_entry(self.mountpoint)
            log.error("mount %s failed: %s", dev, out)
        return None

    def _check_drive(self, d):
        if not d.present and d is self.sd:
            d.present = os.path.isdir(d.video_dir)
        if not d.present:
            return
        try:
            st = os.statvfs(d.video_dir)
            d.free = st.f_bavail * st.f_frsize
            d.total = st.f_blocks * st.f_frsize
            d.low = d.free < self.low_free
        except OSError as e:
            d.errors["stat"] = str(e)
            return
        d.errors.pop("stat", None)
        if d is self.sd:
            m = mount_of(d.video_dir)
            if m and "ro" in m[3]:
                d.errors["readonly"] = "microSD went read-only"
            else:
                d.errors.pop("readonly", None)

    def _smart(self):
        if not shutil.which("smartctl") or not self.ssd.device or self._no_smart:
            return
        disk = disk_sysfs(self.ssd.device)
        if not disk:
            return
        dev = "/dev/" + os.path.basename(disk)
        # Many USB-SATA adapters don't pass SMART through: the kernel then counts the
        # refused requests (and sometimes the next few) as I/O errors. Not drive faults.
        self.forgive_io_errors()
        for extra in (["-d", "sat"], []):
            rc, out = run(["smartctl", "-H"] + extra + [dev], timeout=60)
            if rc >= 0 and not rc & 3:
                if rc & 8:
                    self.ssd.errors["smart"] = "SMART says the drive is failing"
                    log.error("SMART: %s", out[-300:])
                else:
                    self.ssd.errors.pop("smart", None)
                self.forgive_io_errors()
                return
        log.info("this SSD's USB adapter doesn't support SMART health checks; skipping them")
        self._no_smart = True
        self.forgive_io_errors()

    def forgive_io_errors(self, seconds=90):
        """I/O errors counted in the next `seconds` come from our own SMART query."""
        self._forgive_until = time.monotonic() + seconds

    def clear_ssd_errors(self):
        """SSD tab, after a check found the drive healthy: forget the warning."""
        with self._hlock:
            dev = self._ssd_device
            if dev:
                self._ioerr[dev] = read_ioerr(dev)
            for k in ("io", "write", "smart", "last_save"):
                self.ssd.errors.pop(k, None)
            self.ssd.notes.pop("io", None)
        log.warning("SSD warning cleared from the SSD tab")

    def placement(self):
        """Which drive holds each saved video (guest-deleted ones not counted),
        and roughly how much more recording time fits on each drive."""
        rows, total_bytes, total_secs = [], 0, 0.0
        for name in self.manifest.names():
            e = self.manifest.entry(name) or {}
            row = {"name": name, "on": {}}
            for d in self.drives:
                if not d.present:
                    row["on"][d.name] = None             # can't tell: drive not connected
                    continue
                try:
                    size = os.path.getsize(d.path(name))
                except OSError:
                    row["on"][d.name] = False
                    continue
                c = e.get("copies", {}).get(d.name, {})
                row["on"][d.name] = "verified" if c.get("status") == "verified" else "checking"
                row["size"] = size
                secs = c.get("duration") or 0
                if secs > 1:
                    total_bytes += size
                    total_secs += secs
            rows.append(row)
        rate = total_bytes / total_secs if total_secs > 30 else None      # bytes per second
        room = {}
        for d in self.drives:
            if d.present and d.free is not None:
                usable = max(0, d.free - self.min_free)
                room[d.name] = {"free": usable, "free_text": media.human_bytes(usable),
                                "hours": round(usable / rate / 3600, 1) if rate else None}
        return {"videos": rows, "mb_per_minute": round(rate * 60 / 1e6, 1) if rate else None,
                "room": room}

    # ------------------------------------------------------------ maintenance
    def ssd_only(self):
        """Every video file in the SSD's video folder (kept, guest-deleted or
        unfinished) with no copy of the same size on the microSD. Looks at the
        files themselves, so videos the booth doesn't know about count too."""
        out = []
        for sub in ("", DELETED_DIR):
            folder = os.path.join(self.ssd.video_dir, sub)
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for f in names:
                if f.startswith(".") or not f.endswith((VIDEO_EXT, ".ts", PART_EXT)):
                    continue
                rel = os.path.join(sub, f) if sub else f
                try:
                    size = os.path.getsize(os.path.join(folder, f))
                    if os.path.getsize(os.path.join(self.sd.video_dir, rel)) == size:
                        continue
                except OSError:
                    pass
                out.append(rel)
        return out

    def copy_ssd_only_to_sd(self, progress=None, abort=None):
        """Copies (and verifies) every video that is only on the SSD to the
        microSD. -> (ok, message)"""
        todo = self.ssd_only()
        need = sum(os.path.getsize(os.path.join(self.ssd.video_dir, r)) for r in todo)
        self._check_drive(self.sd)
        if need and (self.sd.free or 0) - need < self.min_free:
            return False, (f"{len(todo)} videos ({media.human_bytes(need)}) are only on the SSD "
                           f"and the microSD hasn't room for them "
                           f"({media.human_bytes(self.sd.free)} free). Download them "
                           "(Videos → Download all) and wipe them first.")
        for i, rel in enumerate(todo):
            if progress:
                progress(i / max(1, len(todo)), f"Copying {os.path.basename(rel)} to the microSD")
            src = os.path.join(self.ssd.video_dir, rel)
            dst = os.path.join(self.sd.video_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            want = media.sha256_file(src, abort=abort)
            got = media.copy_file(src, dst, abort=abort)
            if got != want:
                os.remove(dst)
                return False, f"copy of {rel} didn't verify; nothing was changed"
            if rel.endswith(VIDEO_EXT) and os.sep not in rel and \
                    (self.manifest.entry(rel) or {}).get("state") != "deleted":
                self.queue(rel)                   # recorded as a checked microSD copy
        os.sync()
        return True, f"{len(todo)} videos copied to the microSD and checked"

    def begin_maintenance(self, why, guard=None):
        """Takes the SSD offline: background jobs pause, the SSD is unmounted and
        new recordings go to the microSD. guard(): checked at the last moment,
        with the SSD still mounted; a message from it stops. -> (ok, message)"""
        if self.recording.is_set() or self.active_name:
            return False, "a recording is in progress"
        self.paused.set()
        end = time.monotonic() + 120
        while self.busy and time.monotonic() < end:
            time.sleep(0.2)
        if self.busy:
            self.paused.clear()
            return False, "the booth is busy checking videos; try again in a minute"
        with self._hlock:
            problem = guard() if guard else None
            if problem or self.recording.is_set() or self.active_name:
                self.paused.clear()
                return False, problem or "a recording is in progress"
            self.maintenance = why
            try:
                self.manifest.save()
            except OSError:
                pass
            os.sync()
            self.ssd.present = False
            if self.auto and mount_entry(self.mountpoint):
                for attempt in range(5):
                    rc, out = run(["umount", self.mountpoint])
                    if rc == 0:
                        break
                    time.sleep(1)
                else:
                    self.maintenance = None
                    self.paused.clear()
                    return False, f"couldn't unmount the SSD: {out}"
        log.warning("SSD offline for %s (recordings go to the microSD meanwhile)", why)
        return True, ""

    def end_maintenance(self, fresh=False):
        """SSD back online. fresh: new/repaired filesystem, so old error counts go."""
        with self._hlock:
            self.maintenance = None
            if fresh:
                self._ssd_device = None
                self.ssd.errors.clear()
        self.paused.clear()
        self.refresh()
        self.queue("__reconcile__")             # copy anything missing back to the SSD
        log.info("SSD back online")

    # ---------------------------------------------------------------- worker
    def _throttle(self):
        if self.recording.is_set():
            return float(self.cfg["copy_mb_per_s_while_recording"]) * 1e6
        return None

    def _worker(self):
        while not self.stopping.is_set():
            try:
                if self.paused.is_set():
                    time.sleep(0.5)
                    continue
                item = self._q.get(timeout=1)
            except queue.Empty:
                continue
            self.busy = True
            with self._plock:
                self._pending.discard(item)
            try:
                if item == "__reconcile__":
                    self._reconcile()
                elif item == "__scrub__":
                    self._scrub()
                else:
                    self._process(item)
            except media.Aborted:
                log.info("background check of %s stopped for shutdown", item)
            except Exception:
                log.exception("background job %s failed", item)
            finally:
                self.busy = False

    def _listdir(self, d):
        try:
            return os.listdir(d.video_dir)
        except OSError:
            return []

    def _reconcile(self):
        """Pick up leftovers after a crash and anything missing on a drive."""
        for d in self.drives:
            if not d.present:
                continue
            for f in self._listdir(d):
                if f.endswith(PART_EXT):
                    name = f[:-len(PART_EXT)] + VIDEO_EXT
                    e = self.manifest.entry(name)
                    if name == self.active_name or self.recording.is_set() or \
                            (e and e.get("state") == "deleted"):
                        continue
                    final = d.path(name)
                    if not os.path.exists(final):
                        ok, why = media.remux_to_mp4(d.path(f), final)
                        if ok and media.probe(final)["ok"]:
                            log.warning("recovered unfinished recording %s on %s", name, d.name)
                            os.remove(d.path(f))
                            self.manifest.entry(name, create=True).setdefault("recovered", True)
                        else:
                            log.error("could not recover %s on %s: %s", f, d.name, why)
                elif f.endswith(VIDEO_EXT + ".part") or f.endswith(VIDEO_EXT + ".tmp"):
                    # a copy/conversion cut short (stop, power cut). Only removed
                    # when a complete copy of that video exists somewhere.
                    base = f.rsplit(".", 1)[0]
                    if any(os.path.exists(x.path(base)) for x in self.drives if x.present):
                        try:
                            os.remove(d.path(f))
                            log.info("removed unfinished copy %s on %s", f, d.name)
                        except OSError:
                            pass
                elif f.endswith(VIDEO_EXT) and not f.startswith("."):
                    e = self.manifest.entry(f)
                    if e is None:
                        log.info("found %s on %s, adding it", f, d.name)
                        self.manifest.entry(f, create=True)["found_on_drive"] = True
                    elif e.get("state") == "deleted":
                        # e.g. the SSD was unplugged when the guest deleted it
                        self._move(d, f, to_deleted=True)
        self.manifest.save()
        for name in self.manifest.names():
            if name in self.held:
                continue
            e = self.manifest.entry(name)
            for d in self.drives:
                c = e["copies"].get(d.name, {})
                have = {k: v for k, v in e["copies"].items() if v.get("status") != "missing"}
                if d.present and self._copy_wanted(d, name, have) and \
                        (c.get("status") != "verified" or not os.path.exists(d.path(name))):
                    self.queue(name)
                    break

    def _scrub(self):
        """Re-read one saved video per run and compare its checksum."""
        if self.recording.is_set():
            return
        names = self.manifest.names()
        if not names:
            return
        name = names[self._scrub_pos % len(names)]
        self._scrub_pos += 1
        e = self.manifest.entry(name)
        for d in self.drives:
            c = e["copies"].get(d.name)
            if not d.present or not c or c.get("status") != "verified":
                continue
            path = d.path(name)
            try:
                digest = media.sha256_file(path, self._throttle, self.stopping)
            except FileNotFoundError:
                digest = None
            except OSError as ex:
                digest = f"error: {ex}"
            if digest != c.get("sha256"):
                log.error("SCRUB: %s on %s changed or unreadable", name, d.name)
                c["status"] = "suspect"
                self.manifest.save()
                self.queue(name)
            else:
                c["scrubbed"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.manifest.save()

    def _process(self, name):
        """Verify every copy of one recording and repair/mirror as needed."""
        with self.manifest.lock:
            e = self.manifest.entry(name, create=True)
        if e.get("state") == "deleted":
            return
        copies = e.setdefault("copies", {})
        present = [d for d in self.drives if d.present and not d.errors.get("readonly")]
        # checksum each copy had when it was last verified (to spot later changes)
        before = {k: c.get("sha256") for k, c in copies.items()
                  if c.get("status") in ("verified", "suspect")}

        # 1. check every copy that is not already known to be good
        for d in present:
            path = d.path(name)
            c = copies.setdefault(d.name, {})
            if not os.path.exists(path):
                c.clear()
                c["status"] = "missing"
                continue
            st = os.stat(path)
            if c.get("status") == "verified" and c.get("size") == st.st_size:
                continue
            log.info("checking %s on %s", name, d.name)
            r = media.check_copy(path, self._throttle, self.stopping)
            p = media.probe(path)
            c.update({"size": st.st_size, "sha256": r["sha256"], "content": r["content"],
                      "duration": p["duration"], "audio": p["audio"],
                      "status": "checked" if (r["demux_ok"] and p["ok"]) else "bad",
                      "checked": time.strftime("%Y-%m-%d %H:%M:%S")})
            if r["error_sample"]:
                c["errors"] = r["error_sample"]
        self.manifest.save()

        # 2. decide which copy is the reference
        good = [k for k, c in copies.items() if c.get("status") in ("checked", "verified")]
        ref = None
        if len(good) == 1 and copies[good[0]]["status"] == "verified":
            ref = good[0]
        elif len(good) >= 2 and len({copies[k]["content"] for k in good}) == 1:
            ref = good[0]           # both drives hold identical audio/video
            for k in good:
                copies[k]["status"] = "verified"
        elif good:
            # one copy, or copies that disagree: decode them fully to choose
            scored = []
            for k in good:
                ok, nerr = media.decode_check(self._drive(k).path(name))
                copies[k]["decode_errors"] = nerr
                unchanged = before.get(k) == copies[k]["sha256"]
                scored.append((ok, unchanged, copies[k].get("duration", 0), -nerr, k))
            scored.sort(reverse=True)
            if scored[0][0]:
                ref = scored[0][-1]
                copies[ref]["status"] = "verified"
                for *_, k in scored[1:]:
                    if copies[k]["content"] != copies[ref]["content"]:
                        copies[k]["status"] = "bad"
            else:
                ref = scored[0][-1]  # nothing decodes cleanly; keep the best we have
                copies[ref]["status"] = "damaged"
        self.manifest.save()

        # 3. make sure every present drive has a verified copy
        if ref and copies[ref]["status"] == "verified":
            src = self._drive(ref).path(name)
            for d in present:
                c = copies.get(d.name, {})
                if not self._copy_wanted(d, name, {k: v for k, v in copies.items()
                                                   if v.get("status") != "missing"}):
                    continue
                if c.get("status") == "verified" or \
                        not d.usable(self.min_free + copies[ref].get("size", 0), copying=True):
                    continue
                path = d.path(name)
                if os.path.exists(path):
                    keep = path + f".bad-{int(time.time())}"
                    log.warning("%s on %s is damaged, keeping it as %s", name, d.name, keep)
                    os.replace(path, keep)
                log.info("copying %s from %s to %s", name, ref, d.name)
                try:
                    digest = media.copy_file(src, path, self._throttle, self.stopping)
                except OSError as ex:
                    log.error("copy to %s failed: %s", d.name, ex)
                    continue
                if digest == copies[ref]["sha256"]:
                    copies[d.name] = dict(copies[ref], status="verified", copied_from=ref,
                                          checked=time.strftime("%Y-%m-%d %H:%M:%S"))
                else:
                    log.error("copy of %s on %s does not match!", name, d.name)
                    copies[d.name] = {"status": "bad", "sha256": digest}

        # the drive check: did this video save properly on each drive?
        for k, c in copies.items():
            if c.get("status") == "verified":
                self._last_save(k, True)
            elif c.get("status") in ("bad", "damaged"):
                self._last_save(k, False)
        if e.get("state") == "deleted":
            return
        verified = [k for k, c in copies.items() if c.get("status") == "verified"]
        expected = [d.name for d in self.drives if d.present]
        if not verified:
            e["state"] = "damaged"
        elif self.mode != "both":
            e["state"] = "safe"          # saved and checked (one copy by design)
        elif all(k in verified for k in expected) and len(verified) == len(self.drives):
            e["state"] = "safe"          # verified on both drives
        else:
            e["state"] = "one_copy"
        log.info("%s: %s (%s)", name, e["state"],
                 ", ".join(f"{k}={c.get('status')}" for k, c in copies.items()))
        self.manifest.save()
        self._write_sums()

    def _drive(self, name):
        return self.ssd if name == "ssd" else self.sd

    def _last_save(self, drive_name, ok):
        """The drive check: a drive whose last recording didn't save and check out
        isn't used for new recordings (the other drive is) until a video saves on
        it properly again (the booth retries by copying one over), it's
        re-plugged, or the warning is cleared on the SSD tab."""
        d = self._drive(drive_name)
        if ok:
            if d.errors.pop("last_save", None):
                log.info("%s saved a video properly again: using it", d.name)
        elif "last_save" not in d.errors:
            label = "SSD" if d is self.ssd else "microSD"
            d.errors["last_save"] = f"the last recording didn't save properly on the {label}"
            log.error("%s: last recording didn't save properly; recording to the other drive",
                      d.name)

    def _write_sums(self):
        for d in self.drives:
            if not d.present or d.errors:
                continue
            lines = []
            with self.manifest.lock:
                for name, e in sorted(self.manifest.data["recordings"].items()):
                    c = e.get("copies", {}).get(d.name, {})
                    if c.get("status") == "verified" and c.get("sha256"):
                        lines.append(f"{c['sha256']}  {name}\n")
                blob = json.dumps(self.manifest.data, indent=1, sort_keys=True)
            try:
                media.write_atomic(os.path.join(d.video_dir, "SHA256SUMS"), "".join(lines))
                if d is self.ssd:
                    media.write_atomic(os.path.join(d.video_dir, "booth-manifest.json"), blob)
            except OSError as ex:
                log.error("writing SHA256SUMS on %s failed: %s", d.name, ex)

    # --------------------------------------------------------------- shutdown
    def wait_idle(self, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._plock:
                if not self.busy and not self._pending:
                    return True
            time.sleep(0.2)
        return False

    def eject_ssd(self):
        """Stops background work, unmounts the SSD and powers it off.
        Returns True when it is safe to unplug."""
        self.stopping.set()                 # copies in progress stop and clean up
        end = time.monotonic() + 15
        while self.busy and time.monotonic() < end:
            time.sleep(0.2)
        try:
            self.manifest.save()
        except OSError:
            pass
        os.sync()
        with self._hlock:
            if not self.auto:
                return True
            entry = mount_entry(self.mountpoint)
            if entry is None:
                return True
            dev = entry[0]
            for attempt in range(5):
                rc, out = run(["umount", self.mountpoint])
                if rc == 0:
                    break
                log.warning("umount failed (%s), retrying", out)
                time.sleep(1)
            else:
                return False
            disk = disk_sysfs(dev)
            os.sync()
            if shutil.which("udisksctl"):
                rc, _ = run(["udisksctl", "power-off", "-b", "/dev/" + os.path.basename(disk or dev),
                             "--no-user-interaction"])
                if rc == 0:
                    log.info("SSD powered off")
                    return True
            if disk:
                try:
                    with open(os.path.join(disk, "device", "delete"), "w") as f:
                        f.write("1")
                    log.info("SSD removed from the system")
                except OSError as ex:
                    log.warning("could not power off SSD: %s (it is unmounted, still safe)", ex)
            return True
