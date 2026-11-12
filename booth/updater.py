"""Software update, started from the web page (System -> Software update) or
with `sudo booth-update`.

    1. The booth is already paused: nobody recording, background checks done,
       records saved (app.py does that before starting this).
    2. Internet: if the Pi has none (normal on the booth Wi-Fi), it switches the
       booth Wi-Fi off and joins the phone hotspot saved on the web page.
    3. Downloads the new version (git, fast-forward only) and installs it.
       install.sh only replaces the program in /opt/confession-booth, swapping
       it in at once; videos are never touched.
    4. Whatever happened, leaves the hotspot and turns the booth Wi-Fi back on.
    5. Restarts the booth. If the new version doesn't keep running, it goes
       back to the previous version by itself.

Progress is written to /var/lib/confession-booth/update.json (shown on the
web page) and /var/log/booth-update.log. Runs as its own systemd unit, so the
booth restarting doesn't stop it.
"""

import json
import logging
import os
import pwd
import shutil
import subprocess
import sys
import time
import unicodedata

from . import config, media

log = logging.getLogger("booth.updater")

SOURCE_FILE = "/etc/confession-booth/source"         # where the git clone is
HOTSPOT_FILE = "/etc/confession-booth/hotspot.json"  # {"name", "password"}
APP_DIR = "/opt/confession-booth"
SERVICE = "confession-booth"
AP = "booth-ap"
HOTSPOT_CON = "booth-update-hotspot"
IFACE = "wlan0"
BUSY = ("countdown", "recording", "stopping", "review", "confirm", "deleted", "shutdown")


# ------------------------------------------------------------------ helpers
def run(cmd, timeout=120, user=None, cwd=None):
    if user and user != "root":
        cmd = ["runuser", "-u", user, "--"] + cmd
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)
        out = (r.stdout + r.stderr).decode(errors="replace").strip()
        log.info("$ %s -> %s%s", " ".join(cmd), r.returncode, f"\n{out}" if out else "")
        return r.returncode, out
    except (OSError, subprocess.SubprocessError) as e:
        log.info("$ %s -> %s", " ".join(cmd), e)
        return -1, str(e)


def norm_ssid(s):
    """Phones use curly apostrophes ("Jack’s iPhone"); people type straight ones."""
    s = unicodedata.normalize("NFKC", s or "")
    for ch in "’‘`´":
        s = s.replace(ch, "'")
    return " ".join(s.split()).casefold()


def pick_ssid(wanted, visible):
    for s in visible:
        if s == wanted:
            return s
    for s in visible:
        if norm_ssid(s) == norm_ssid(wanted):
            return s
    return None


def load_hotspot():
    try:
        with open(HOTSPOT_FILE) as f:
            d = json.load(f)
        return d if d.get("name") else None
    except (OSError, ValueError):
        return None


def save_hotspot(name, password):
    name = (name or "").strip()
    if not name or len(name.encode()) > 32:
        raise ValueError("hotspot name must be 1-32 characters")
    if password and not 8 <= len(password) <= 63:
        raise ValueError("hotspot password must be 8-63 characters")
    os.makedirs(os.path.dirname(HOTSPOT_FILE), exist_ok=True)
    media.write_atomic(HOTSPOT_FILE, json.dumps({"name": name, "password": password or ""}))
    os.chmod(HOTSPOT_FILE, 0o600)


def source_dir():
    try:
        with open(SOURCE_FILE) as f:
            d = f.read().strip()
        return d if os.path.isdir(os.path.join(d, ".git")) else None
    except OSError:
        return None


def installed_version(app_dir=APP_DIR):
    try:
        with open(os.path.join(app_dir, "VERSION")) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


class Updater:
    def __init__(self, cfg):
        self.cfg = cfg
        self.status_path = os.path.join(cfg["state_dir"], "update.json")
        self.steps = []
        self.app_dir = APP_DIR
        self.settle = 45          # seconds the new version must keep running

    def status(self, state, message, **extra):
        log.info("[%s] %s", state, message)
        self.steps.append(f"{time.strftime('%H:%M:%S')} {message}")
        data = {"state": state, "message": message, "time": time.time(),
                "steps": self.steps[-30:], **extra}
        try:
            media.write_atomic(self.status_path, json.dumps(data))
        except OSError:
            pass

    # -------------------------------------------------------------- network
    def online(self, src, owner):
        rc, _ = run(["git", "-C", src, "ls-remote", "--heads", "origin"], timeout=25, user=owner)
        return rc == 0

    def ap_active(self):
        rc, out = run(["nmcli", "-g", "GENERAL.STATE", "connection", "show", AP], timeout=15)
        return rc == 0 and "activated" in out

    def ap_wanted(self):
        rc, out = run(["nmcli", "-g", "connection.autoconnect", "connection", "show", AP],
                      timeout=15)
        return rc == 0 and "yes" in out

    def join_hotspot(self, hs):
        self.status("working", f"Looking for the hotspot \"{hs['name']}\"…")
        ssid = None
        end = time.monotonic() + 60
        while time.monotonic() < end and not ssid:
            rc, out = run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan",
                           "yes"], timeout=30)
            visible = [l.replace("\\:", ":") for l in out.splitlines() if l.strip()]
            ssid = pick_ssid(hs["name"], visible)
            if not ssid:
                time.sleep(4)
        if not ssid:
            raise RuntimeError(f"couldn't see the hotspot \"{hs['name']}\". Is Personal Hotspot "
                               "on, with its settings screen open?")
        run(["nmcli", "connection", "delete", HOTSPOT_CON], timeout=20)
        cmd = ["nmcli", "connection", "add", "type", "wifi", "ifname", IFACE, "con-name",
               HOTSPOT_CON, "ssid", ssid, "connection.autoconnect", "no"]
        if hs.get("password"):
            cmd += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", hs["password"]]
        rc, out = run(cmd, timeout=20)
        if rc != 0:
            raise RuntimeError(f"couldn't set up the hotspot connection: {out[-200:]}")
        self.status("working", f"Joining \"{ssid}\"…")
        for attempt in range(3):
            rc, out = run(["nmcli", "--wait", "45", "connection", "up", HOTSPOT_CON], timeout=60)
            if rc == 0:
                return
            time.sleep(5)
        raise RuntimeError("couldn't join the hotspot (wrong password?)")

    def restore_wifi(self, ap_was_on):
        run(["nmcli", "connection", "down", HOTSPOT_CON], timeout=30)
        run(["nmcli", "connection", "delete", HOTSPOT_CON], timeout=20)
        if ap_was_on or self.ap_wanted():
            for attempt in range(5):
                rc, _ = run(["nmcli", "--wait", "30", "connection", "up", AP], timeout=45)
                if rc == 0:
                    log.info("booth Wi-Fi back on")
                    return True
                time.sleep(5)
            log.error("booth Wi-Fi didn't come back; it will at the next power-on")
            return False
        run(["nmcli", "device", "connect", IFACE], timeout=45)
        return True

    # --------------------------------------------------------------- update
    def wait_for_booth_paused(self, timeout=900):
        """The booth must be idle and paused before anything is changed."""
        rc, out = run(["systemctl", "is-active", SERVICE], timeout=10)
        if out.strip() != "active":
            return True                      # not running: nothing to protect live
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                with open(os.path.join(self.cfg["state_dir"], "status.json")) as f:
                    st = json.load(f)
            except (OSError, ValueError):
                st = {}
            if st.get("state") == "updating" and st.get("update_ready"):
                return True
            if st.get("state") in BUSY:
                self.status("working", "Waiting for the booth to finish saving…")
            time.sleep(2)
        return False

    def git_update(self, src, owner):
        branch = run(["git", "-C", src, "rev-parse", "--abbrev-ref", "HEAD"], user=owner)[1]
        rc, out = run(["git", "-C", src, "status", "--porcelain", "--untracked-files=no"],
                      user=owner)
        if out.strip():
            raise RuntimeError(f"the copy in {src} has local changes; not touching it")
        old = run(["git", "-C", src, "rev-parse", "--short", "HEAD"], user=owner)[1]
        self.status("working", "Downloading the new version…")
        rc, out = run(["git", "-C", src, "fetch", "--prune", "origin"], timeout=300, user=owner)
        if rc != 0:
            raise RuntimeError(f"download failed: {out[-200:]}")
        rc, out = run(["git", "-C", src, "merge", "--ff-only", f"origin/{branch}"],
                      timeout=120, user=owner)
        if rc != 0:
            raise RuntimeError(f"couldn't apply the new version: {out[-200:]}")
        new = run(["git", "-C", src, "rev-parse", "--short", "HEAD"], user=owner)[1]
        return old.strip(), new.strip()

    def install(self, src):
        self.status("working", "Installing…")
        env = dict(os.environ, BOOTH_FROM_UPDATER="1")
        try:
            r = subprocess.run(["bash", os.path.join(src, "install.sh")], cwd=src, env=env,
                               capture_output=True, timeout=1800)
        except (OSError, subprocess.SubprocessError) as e:
            raise RuntimeError(f"install failed: {e}")
        log.info("install.sh:\n%s", (r.stdout + r.stderr).decode(errors="replace")[-4000:])
        if r.returncode != 0:
            raise RuntimeError("install failed (see the log)")

    def restart_and_check(self, src, rollback_possible):
        """Restart the booth; if it doesn't keep running, go back."""
        self.status("working", "Restarting the booth…")
        run(["systemctl", "restart", SERVICE], timeout=60)
        before = run(["systemctl", "show", "-p", "NRestarts", "--value", SERVICE])[1]
        time.sleep(self.settle)
        active = run(["systemctl", "is-active", SERVICE])[1].strip() == "active"
        after = run(["systemctl", "show", "-p", "NRestarts", "--value", SERVICE])[1]
        if active and before == after:
            return "ok"
        app = self.app_dir
        prev = os.path.join(app, "booth.prev")
        if not rollback_possible or not os.path.isdir(prev):
            return "not running"
        log.error("new version isn't running properly; going back to the previous one")
        bad = os.path.join(app, "booth.failed")
        shutil.rmtree(bad, ignore_errors=True)
        os.replace(os.path.join(app, "booth"), bad)
        os.replace(prev, os.path.join(app, "booth"))
        try:
            os.replace(os.path.join(app, "VERSION.prev"), os.path.join(app, "VERSION"))
        except OSError:
            pass
        os.sync()
        run(["systemctl", "restart", SERVICE], timeout=60)
        return "rolled back"

    def go(self):
        self.status("working", "Starting the update")
        src = source_dir()
        if not src:
            self.status("failed", "Don't know where the booth was installed from. Run "
                        "sudo ./install.sh once in the Video_Confession_Booth folder.")
            return 1
        owner = pwd.getpwuid(os.stat(src).st_uid).pw_name
        if not self.wait_for_booth_paused():
            self.status("failed", "The booth stayed busy; nothing was changed.")
            run(["systemctl", "restart", SERVICE], timeout=60)
            return 1
        ap_was_on = self.ap_active()
        switched = False
        result, ok, installed = None, False, False
        try:
            if not self.online(src, owner):
                hs = load_hotspot()
                if not hs:
                    raise RuntimeError("no internet, and no phone hotspot saved")
                if ap_was_on:
                    self.status("working", "Switching the booth Wi-Fi off for the update…")
                    run(["nmcli", "connection", "down", AP], timeout=30)
                switched = True
                self.join_hotspot(hs)
                end = time.monotonic() + 40
                while not self.online(src, owner):
                    if time.monotonic() > end:
                        raise RuntimeError("joined the hotspot but there's no internet through it")
                    time.sleep(4)
            old, new = self.git_update(src, owner)
            if old == new and installed_version(self.app_dir).split()[0:1] == [new]:
                result = f"Already up to date ({new})."
            else:
                self.install(src)
                installed = True
                result = f"Updated {old} → {new}."
            ok = True
        except Exception as e:                    # noqa: BLE001 - always restore below
            result = f"Update failed: {e}"
            log.exception("update failed")
        finally:
            if switched:
                self.status("working", "Turning the booth Wi-Fi back on…")
                self.restore_wifi(ap_was_on)
        running = self.restart_and_check(src, rollback_possible=installed)
        if running == "rolled back":
            ok = False
            result += " The new version didn't start, so the booth went back to the " \
                      "previous version."
        elif running != "ok":
            ok = False
            result += " The booth isn't running properly - see the log."
        self.status("done" if ok else "failed", result,
                    version=installed_version(self.app_dir))
        return 0 if ok else 1


def launch(cfg):
    """Start the updater in its own systemd unit (survives the booth restarting)."""
    if not shutil.which("systemd-run") or os.geteuid() != 0:
        raise RuntimeError("updates only run on the Pi (as root, with systemd)")
    rc, out = run(["systemd-run", "--unit", "booth-update", "--collect",
                   "--description", "Confession booth software update",
                   "--working-directory", APP_DIR, sys.executable, "-m", "booth.updater",
                   "--run"],
                  timeout=30)
    if rc != 0:
        raise RuntimeError(f"couldn't start the update: {out[-200:]}")


def main():
    cfg = config.load()
    os.makedirs("/var/log", exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[
        logging.FileHandler("/var/log/booth-update.log"), logging.StreamHandler(sys.stderr)])
    if os.geteuid() != 0:
        sys.exit("Please run with sudo:  sudo booth-update")
    if "--hotspot" in sys.argv:
        i = sys.argv.index("--hotspot")
        save_hotspot(sys.argv[i + 1], sys.argv[i + 2] if len(sys.argv) > i + 2 else "")
        print("Hotspot saved.")
        return 0
    if "--run" in sys.argv:
        return Updater(cfg).go()
    # Typed by a person: ask the running booth, which pauses safely first.
    if run(["systemctl", "is-active", SERVICE], timeout=10)[1].strip() == "active":
        return ask_booth(cfg)
    return Updater(cfg).go()


def ask_booth(cfg):
    import base64
    import urllib.request
    port = int(cfg.get("web_port") or 0)
    if port <= 0:
        sys.exit("The web page is off (web_port = 0); stop the booth first: "
                 "sudo systemctl stop confession-booth")
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/update",
                                 data=json.dumps({"action": "start"}).encode(),
                                 headers={"Content-Type": "application/json"})
    if cfg.get("web_password"):
        token = base64.b64encode(f"booth:{cfg['web_password']}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read())
    except Exception as e:                       # noqa: BLE001
        sys.exit(f"Couldn't reach the booth: {e}")
    if not res.get("ok"):
        sys.exit(res.get("error") or "The booth said no.")
    print("Update started. Progress: sudo journalctl -u booth-update -f")
    print("(or the web page, System tab, once the booth Wi-Fi is back)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
