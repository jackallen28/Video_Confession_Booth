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

    def usable(self, min_free):
        return self.present and not self.errors and (self.free or 0) > min_free

    def path(self, name):
        return os.path.join(self.video_dir, name)

    def snapshot(self):
        return {"present": self.present, "level": self.level, "free": self.free,
                "low": self.present and self.low, "errors": dict(self.errors)}


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
        self._hlock = threading.Lock()       # health checks vs. eject
        self._ioerr = {}
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
        """Path of the best copy to read (SSD first), or None."""
        for d in self.drives:
            if d.present and os.path.exists(d.path(name)):
                return d.path(name)
        return None

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
    def targets(self):
        """Where the next recording goes: every healthy drive with room."""
        with self._hlock:
            return {d.name: d for d in self.drives if d.usable(self.min_free)}

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
                return
            try:
                media.fsync_file(part)
            except OSError as e:
                log.error("%s: %s copy unreadable: %s", name, drive_name, e)
                return
            ok, why = media.remux_to_mp4(part, final)
            if not ok:
                log.error("%s: MP4 on %s failed (%s); keeping the raw .ts", name, drive_name, why)
                try:
                    os.replace(part, part[:-len(".part")])
                except OSError:
                    pass
                return
            p = media.probe(final)
            log.info("%s on %s: %s", name, drive_name, p)
            outcome[drive_name] = p["ok"]
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
        """Guest deleted it: remove every copy from every drive."""
        stem = name[:-len(VIDEO_EXT)]
        with self.manifest.lock:
            e = self.manifest.entry(name, create=True)
            e["state"] = "deleted"
            e["deleted"] = time.strftime("%Y-%m-%d %H:%M:%S")
            e["copies"] = {}
            self.manifest.save()
        for d in self.drives:
            if not d.present:
                continue
            for f in (name, stem + PART_EXT, stem + ".ts", name + ".tmp"):
                try:
                    os.remove(d.path(f))
                    log.info("deleted %s from %s", f, d.name)
                except FileNotFoundError:
                    pass
                except OSError as ex:
                    log.error("could not delete %s on %s: %s", f, d.name, ex)
            media.fsync_dir(d.video_dir)
        self.held.discard(name)
        self._write_sums()

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
            if idle and now - self._last["write"] > self.cfg["write_test_interval"]:
                self._last["write"] = now
                for d in self.drives:
                    if d.present:
                        self._write_test(d)
            if idle and self.auto and self.ssd.present and \
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
            log.info("SSD %s (%s) mounted at %s", dev, fstype, self.mountpoint)
        d.present = True
        d.device = dev
        if "ro" in opts:
            d.errors["readonly"] = "drive went read-only (filesystem errors?)"
        n = read_ioerr(dev)
        base = self._ioerr.get(dev)
        if n is not None and base is not None and n > base:
            d.errors["io"] = f"{n - base} I/O errors"
            log.error("SSD I/O errors: %d", n - base)
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

    def _write_test(self, d):
        p = os.path.join(d.video_dir, ".booth-health-check")
        data = os.urandom(512 * 1024)
        try:
            with open(p, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            media.drop_cache(p)
            with open(p, "rb") as f:
                back = f.read()
            os.remove(p)
            if back != data:
                raise OSError("data read back is different")
            d.errors.pop("write", None)
        except OSError as e:
            log.error("%s write test failed: %s", d.name, e)
            d.errors["write"] = f"write test failed: {e}"

    def _smart(self):
        if not shutil.which("smartctl") or not self.ssd.device:
            return
        disk = disk_sysfs(self.ssd.device)
        if not disk:
            return
        dev = "/dev/" + os.path.basename(disk)
        for extra in (["-d", "sat"], []):
            rc, out = run(["smartctl", "-H"] + extra + [dev], timeout=60)
            if rc >= 0 and not rc & 3:
                if rc & 8:
                    self.ssd.errors["smart"] = "SMART says the drive is failing"
                    log.error("SMART: %s", out[-300:])
                else:
                    self.ssd.errors.pop("smart", None)
                return

    # ---------------------------------------------------------------- worker
    def _throttle(self):
        if self.recording.is_set():
            return float(self.cfg["copy_mb_per_s_while_recording"]) * 1e6
        return None

    def _worker(self):
        while not self.stopping.is_set():
            try:
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
                elif f.endswith(VIDEO_EXT) and not f.startswith("."):
                    e = self.manifest.entry(f)
                    if e is None:
                        log.info("found %s on %s, adding it", f, d.name)
                        self.manifest.entry(f, create=True)["found_on_drive"] = True
                    elif e.get("state") == "deleted":
                        log.warning("%s was deleted by a guest but is on %s; leaving it", f, d.name)
        self.manifest.save()
        for name in self.manifest.names():
            if name in self.held:
                continue
            e = self.manifest.entry(name)
            for d in self.drives:
                c = e["copies"].get(d.name, {})
                if d.present and (c.get("status") != "verified" or not os.path.exists(d.path(name))):
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
                if c.get("status") == "verified" or \
                        not d.usable(self.min_free + copies[ref].get("size", 0)):
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

        if e.get("state") == "deleted":
            return
        verified = [k for k, c in copies.items() if c.get("status") == "verified"]
        expected = [d.name for d in self.drives if d.present]
        if not verified:
            e["state"] = "damaged"
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
