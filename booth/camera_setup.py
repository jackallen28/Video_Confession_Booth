"""Advanced camera settings over SSH.

    sudo booth-camera                      menu
    sudo booth-camera show                 current settings
    sudo booth-camera timestamp on|off
    sudo booth-camera resolution max       (or 1280x720, 1920x1080@30, ...)
    sudo booth-camera set brightness=140 sharpness=3
    sudo booth-camera reset                picture controls back to the camera's defaults
    sudo booth-camera time "2026-12-31 21:30"
    sudo booth-camera restart              restart the booth (applies resolution/timestamp)

Picture controls (brightness, exposure, white balance, focus, ...) change on
the booth's screen straight away. Resolution and timestamp changes need the
booth restarted (the menu offers it). Settings are saved in
/etc/confession-booth/camera.json and survive reboots.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

from . import config, devices, media
from .config import size_tuple

CORNERS = ["bottom-right", "bottom-left", "top-left", "top-right"]
SIZES = ["small", "medium", "large"]
FORMATS = ["%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %I:%M %p",
           "%a %d %b %Y %H:%M", "%H:%M:%S"]
SERVICE = "confession-booth"


class Setup:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = cfg["camera_settings_file"]
        self.saved = config.load_camera_settings(self.path)
        self.dev = devices.find_camera(cfg["camera"])
        self.restart_needed = False

    # ------------------------------------------------------------ settings
    def get(self, key):
        return self.saved.get(key, self.cfg[key])

    def put(self, key, value, restart=True):
        self.saved[key] = value
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        media.write_atomic(self.path, json.dumps(self.saved, indent=1, sort_keys=True) + "\n")
        if restart:
            self.restart_needed = True

    def booth_status(self):
        try:
            with open(os.path.join(self.cfg["state_dir"], "status.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def recording_format(self):
        try:
            with open(os.path.join(self.cfg["state_dir"], "video-check.json")) as f:
                c = json.load(f)["choice"]
            return f"{c['w']}x{c['h']} @ {c['fps']:g} fps ({c['encoder']})"
        except (OSError, ValueError, KeyError):
            return "not tested yet"

    # -------------------------------------------------------------- show
    def show(self):
        st = self.booth_status()
        print(f"Camera:      {devices.camera_name(self.dev) if self.dev else 'NOT FOUND'}"
              + (f"  ({self.dev})" if self.dev else ""))
        print(f"Booth:       {st.get('state', 'not running')}")
        res = self.get("video_size")
        fps = self.get("framerate")
        print(f"Resolution:  {'best available' if res == 'max' else res}"
              f"{'' if str(fps) == 'max' else f' @ {fps} fps'}   (recording now: {self.recording_format()})")
        ts = self.get("timestamp")
        print(f"Timestamp:   {'ON' if ts else 'off'}"
              + (f"  {self.get('timestamp_corner')}, {self.get('timestamp_size')}, "
                 f"e.g. {datetime.datetime.now().strftime(self.get('timestamp_format'))}" if ts else ""))
        print(f"Mirror:      {'on' if self.get('mirror_preview') else 'off'} (screen only)")
        print(f"Pi clock:    {datetime.datetime.now():%a %d %b %Y %H:%M:%S}")
        saved = self.get("v4l2_controls")
        if saved:
            print("Saved picture settings: " + ", ".join(f"{k}={v}" for k, v in saved.items()))
        if self.restart_needed:
            print("\n* Restart the booth to use the new resolution/timestamp settings.")

    # ------------------------------------------------------------ actions
    def set_resolution(self, text):
        if text == "max":
            self.put("video_size", "max")
            self.put("framerate", "max")
            return True
        size, _, fps = text.partition("@")
        try:
            size_tuple(size)
        except ValueError:
            print("Use e.g. 1280x720 or 1920x1080@30")
            return False
        modes = devices.list_modes(self.dev) if self.dev else []
        if modes and not any(f"{m['w']}x{m['h']}" == size for m in modes):
            print(f"Warning: the camera does not list {size}.")
        self.put("video_size", size)
        self.put("framerate", float(fps) if fps else "max")
        return True

    def set_control(self, name, value):
        ok, msg = devices.set_control(self.dev, name, value) if self.dev else (False, "no camera")
        controls = dict(self.get("v4l2_controls"))
        controls[name] = value
        self.put("v4l2_controls", controls, restart=False)
        print(f"{name} = {value}" + ("" if ok else f"   (camera said: {msg}; saved anyway)"))

    def reset_controls(self):
        for c in devices.list_controls(self.dev) if self.dev else []:
            if "default" in c and "inactive" not in c.get("flags", ""):
                devices.set_control(self.dev, c["name"], c["default"])
        self.put("v4l2_controls", {}, restart=False)
        print("Picture controls back to the camera's defaults.")

    def set_time(self, text):
        try:
            when = datetime.datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            print('Use the form "2026-12-31 21:30"')
            return
        subprocess.run(["date", "-s", when.strftime("%Y-%m-%d %H:%M:00")], check=False)
        if shutil.which("fake-hwclock"):
            subprocess.run(["fake-hwclock", "save", "force"], check=False)
        print(f"Clock set: {datetime.datetime.now():%a %d %b %Y %H:%M:%S}")
        print("(Connecting the Pi to Wi-Fi once sets it automatically.)")

    def restart(self, force=False):
        st = self.booth_status().get("state")
        if st in ("countdown", "recording", "stopping", "review", "confirm") and not force:
            print(f"Someone is using the booth right now ({st}). Try again in a moment.")
            return False
        r = subprocess.run(["systemctl", "restart", SERVICE])
        if r.returncode == 0:
            self.restart_needed = False
            print("Booth restarting. A new resolution/timestamp is tested for a few seconds first.")
        return r.returncode == 0

    # --------------------------------------------------------------- menu
    def menu(self):
        while True:
            print("\n" + "=" * 60 + "\nCONFESSION BOOTH - camera settings\n" + "=" * 60)
            self.show()
            print("""
 1  Resolution / frame rate
 2  Timestamp on video: on/off
 3  Timestamp corner
 4  Timestamp format
 5  Timestamp size
 6  Picture controls (brightness, exposure, white balance, focus...)
 7  Reset picture controls to camera defaults
 8  Mirror the screen preview: on/off
 9  Set the Pi's date and time
 r  Restart the booth now (applies 1-5)
 q  Quit""")
            ch = ask("> ").lower()
            if ch == "1":
                self.menu_resolution()
            elif ch == "2":
                self.put("timestamp", not self.get("timestamp"))
            elif ch == "3":
                self.put("timestamp_corner", pick("Corner", CORNERS, self.get("timestamp_corner")))
            elif ch == "4":
                now = datetime.datetime.now()
                labels = [now.strftime(f) for f in FORMATS] + ["Type my own (strftime codes)"]
                cur = self.get("timestamp_format")
                choice = pick("Format", labels, now.strftime(cur))
                if choice == labels[-1]:
                    fmt = ask("Format, e.g. %d/%m/%Y %H:%M:%S : ")
                    if fmt:
                        self.put("timestamp_format", fmt)
                else:
                    self.put("timestamp_format", FORMATS[labels.index(choice)])
            elif ch == "5":
                self.put("timestamp_size", pick("Size", SIZES, self.get("timestamp_size")))
            elif ch == "6":
                self.menu_controls()
            elif ch == "7":
                self.reset_controls()
            elif ch == "8":
                self.put("mirror_preview", not self.get("mirror_preview"))
            elif ch == "9":
                print(f"Now: {datetime.datetime.now():%Y-%m-%d %H:%M}")
                t = ask("New date and time (YYYY-MM-DD HH:MM), blank to keep: ")
                if t:
                    self.set_time(t)
            elif ch == "r":
                self.restart()
            elif ch in ("q", "quit", "exit"):
                if self.restart_needed and ask("Restart the booth now to apply? [y/N] ").lower() == "y":
                    self.restart()
                return

    def menu_resolution(self):
        modes = devices.list_modes(self.dev) if self.dev else []
        capw, caph = size_tuple(self.cfg["max_video_size"])
        fmt = "mjpeg" if any(m["format"] == "mjpeg" for m in modes) else "yuyv422"
        usable = [m for m in modes if m["format"] == fmt and m["w"] <= capw and m["h"] <= caph]
        labels = ["Best available (automatic - tested on this Pi)"] + \
            [f"{m['w']}x{m['h']} up to {m['fps']:g} fps" for m in usable]
        if len(modes) > len(usable):
            print(f"(Bigger modes are hidden: the Pi 3B+ records up to {capw}x{caph}.)")
        choice = pick("Resolution", labels, None)
        if choice is None:
            return
        if choice == labels[0]:
            self.set_resolution("max")
            return
        m = usable[labels.index(choice) - 1]
        fps = ask(f"Frame rate (blank = {min(m['fps'], 30):g}): ")
        self.set_resolution(f"{m['w']}x{m['h']}" + (f"@{fps}" if fps else ""))

    def menu_controls(self):
        while True:
            controls = [c for c in devices.list_controls(self.dev) if c["type"] != "button"] \
                if self.dev else []
            if not controls:
                print("No adjustable controls found (is the camera plugged in?)")
                return
            print("\nPicture controls (changes show on the booth screen immediately):")
            group = None
            for i, c in enumerate(controls, 1):
                if c["group"] != group:
                    group = c["group"]
                    print(f"  -- {group}")
                print(f"  {i:2}  {describe(c)}")
            ch = ask("Number to change (blank = back): ")
            if not ch:
                return
            try:
                c = controls[int(ch) - 1]
            except (ValueError, IndexError):
                continue
            if "inactive" in c.get("flags", ""):
                print("Note: this is locked by an automatic mode (turn the matching auto setting off first).")
            if c["type"] == "bool":
                self.set_control(c["name"], 0 if c.get("value") else 1)
            elif c["menu"]:
                labels = [f"{k}: {v}" for k, v in c["menu"].items()]
                choice = pick(c["name"], labels, None)
                if choice:
                    self.set_control(c["name"], int(choice.split(":")[0]))
            else:
                v = ask(f"{c['name']} ({c.get('min')}..{c.get('max')}, default {c.get('default')}): ")
                try:
                    self.set_control(c["name"], int(v))
                except ValueError:
                    pass


def describe(c):
    v = c.get("value")
    if c["menu"] and v in c["menu"]:
        v = f"{v} ({c['menu'][v]})"
    elif c["type"] == "bool":
        v = "on" if v else "off"
    rng = f"  [{c.get('min')}..{c.get('max')}]" if c["type"] in ("int",) else ""
    flags = "  (locked by auto)" if "inactive" in c.get("flags", "") else ""
    return f"{c['name']:<34} {v}{rng}{flags}"


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        print()
        return "q"


def pick(title, options, current):
    print(f"\n{title}:")
    for i, o in enumerate(options, 1):
        print(f"  {i}  {o}{'   <- now' if o == current else ''}")
    ch = ask("Choose (blank = keep): ")
    try:
        return options[int(ch) - 1]
    except (ValueError, IndexError):
        return current


def main(argv=None):
    ap = argparse.ArgumentParser(prog="booth-camera", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="booth settings file")
    ap.add_argument("--dev", action="store_true", help="use ./booth-data (laptop testing)")
    ap.add_argument("--force", action="store_true", help="restart even while recording")
    ap.add_argument("command", nargs="?", default="menu")
    ap.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    overrides = config.dev_overrides(os.path.abspath("booth-data")) if a.dev else None
    cfg = config.load(a.config, overrides)
    if not a.dev and os.geteuid() != 0 and a.command not in ("show",):
        sys.exit("Please run with sudo:  sudo booth-camera")
    s = Setup(cfg)
    cmd = a.command
    if cmd == "menu":
        s.menu()
    elif cmd == "show":
        s.show()
        if s.dev and s.dev != "test":
            print("\nCamera modes:")
            for m in devices.list_modes(s.dev):
                print(f"  {m['format']:<8} {m['w']}x{m['h']} up to {m['fps']:g} fps")
            print("\nPicture controls:")
            for c in devices.list_controls(s.dev):
                print("  " + describe(c))
    elif cmd == "timestamp" and a.args and a.args[0] in ("on", "off"):
        s.put("timestamp", a.args[0] == "on")
        print(f"Timestamp {a.args[0]}. Run: sudo booth-camera restart")
    elif cmd == "resolution" and a.args:
        if s.set_resolution(a.args[0]):
            print("Saved. Run: sudo booth-camera restart")
    elif cmd == "set" and a.args:
        for kv in a.args:
            k, _, v = kv.partition("=")
            try:
                s.set_control(k, int(v))
            except ValueError:
                print(f"bad value in {kv}")
    elif cmd == "reset":
        s.reset_controls()
    elif cmd == "time" and a.args:
        s.set_time(" ".join(a.args))
    elif cmd == "restart":
        s.restart(force=a.force)
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
