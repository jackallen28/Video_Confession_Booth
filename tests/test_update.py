"""Software update and video safety.

  * updater: online update; via the phone hotspot (switches the booth Wi-Fi
    off and always back on); hotspot not found; new version doesn't start ->
    previous version restored. Uses real git and stand-ins for nmcli/systemctl.
  * booth: an update only starts when idle, pauses background work first;
    being stopped (systemctl stop/restart, shutdown) saves the recording first;
    a camera restart never interrupts a recording.

    python3 tests/test_update.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import logging  # noqa: E402

from booth import config, media, updater  # noqa: E402

logging.basicConfig(level=logging.WARNING)
ROOT = tempfile.mkdtemp(prefix="booth-update-")
STUBS = os.path.join(ROOT, "bin")
CALLS = os.path.join(ROOT, "calls.log")
os.makedirs(STUBS)


def stub(name, body):
    p = os.path.join(STUBS, name)
    with open(p, "w") as f:
        f.write("#!/bin/bash\necho \"%s $*\" >> %s\n%s\n" % (name, CALLS, body))
    os.chmod(p, 0o755)


stub("nmcli", '''case "$*" in
  *"GENERAL.STATE connection show booth-ap"*) echo activated ;;
  *"connection.autoconnect connection show booth-ap"*) echo yes ;;
  *"device wifi list"*) printf 'OtherNet\\nJack\\xe2\\x80\\x99s iPhone\\n' ;;
  *"connection up booth-update-hotspot"*) [ -e "$BOOTH_TEST_ROOT/wrong-password" ] && exit 4 ;;
esac
exit 0''')
stub("systemctl", '''case "$*" in
  "is-active confession-booth") cat %s/active 2>/dev/null || echo inactive ;;
  "restart confession-booth") echo active > %s/active ;;
  *NRestarts*) cat %s/nrestarts 2>/dev/null || echo 0 ;;
esac
exit 0''' % (ROOT, ROOT, ROOT))
os.environ["PATH"] = STUBS + ":" + os.environ["PATH"]
os.environ["BOOTH_TEST_ROOT"] = ROOT
updater.FIND_SECONDS = 2          # keep the "hotspot missing" test quick


def fresh():
    """Each scenario starts with the booth service stopped and no calls logged."""
    open(CALLS, "w").close()
    for f in ("active", "nrestarts"):
        if os.path.exists(os.path.join(ROOT, f)):
            os.remove(os.path.join(ROOT, f))


def calls():
    try:
        return open(CALLS).read()
    except OSError:
        return ""


def git(*a, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def make_repos(tag, fail_install=False):
    """origin (bare) + a clone like ~/Video_Confession_Booth, then a new commit
    on origin so there is something to update."""
    base = os.path.join(ROOT, tag)
    origin, src, work = (os.path.join(base, x) for x in ("origin.git", "src", "work"))
    os.makedirs(base)
    git("init", "-q", "--bare", "-b", "main", origin)
    git("clone", "-q", origin, work)
    with open(os.path.join(work, "install.sh"), "w") as f:
        f.write('#!/bin/bash\nset -e\nAPP="$APPDIR"\nmkdir -p "$APP"\n'
                '[ -d "$APP/booth" ] && rm -rf "$APP/booth.prev" && mv "$APP/booth" "$APP/booth.prev"\n'
                'mkdir "$APP/booth"\ngit -C "$(dirname "$0")" log -1 --format=%h > "$APP/VERSION"\n')
    git("add", ".", cwd=work)
    git("commit", "-qm", "v1", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    git("clone", "-q", origin, src)
    with open(os.path.join(work, "new.txt"), "w") as f:
        f.write("v2")
    if fail_install:          # the new version's installer breaks after swapping in
        with open(os.path.join(work, "install.sh"), "a") as f:
            f.write("echo broken >&2\nexit 1\n")
    git("add", ".", cwd=work)
    git("commit", "-qm", "v2", cwd=work)
    git("push", "-q", "origin", "HEAD:main", cwd=work)
    return src, git("rev-parse", "--short", "HEAD", cwd=work)


def new_updater(tag, src, online_after_hotspot=None):
    cfg = config.load(overrides=dict(config.dev_overrides(os.path.join(ROOT, tag, "data"))))
    os.makedirs(cfg["state_dir"], exist_ok=True)
    u = updater.Updater(cfg)
    u.app_dir = os.path.join(ROOT, tag, "opt")
    os.environ["APPDIR"] = u.app_dir
    u.settle = 0
    updater.source_dir = lambda: src
    if online_after_hotspot is not None:
        state = {"joined": False}
        real_join = u.join_hotspot

        def join(hs):
            real_join(hs)
            state["joined"] = online_after_hotspot
        u.join_hotspot = join
        u.online = lambda src, owner: state["joined"]
    return u, cfg


def result(cfg):
    return json.load(open(os.path.join(cfg["state_dir"], "update.json")))


updater.HOTSPOT_FILE = os.path.join(ROOT, "hotspot.json")

# --- 1. online: just updates, Wi-Fi untouched
src, newest = make_repos("online")
u, cfg = new_updater("online", src)
fresh()
assert u.go() == 0, result(cfg)
assert git("rev-parse", "--short", "HEAD", cwd=src) == newest
assert result(cfg)["state"] == "done" and newest in result(cfg)["message"]
assert "connection down booth-ap" not in calls() and "systemctl restart confession-booth" in calls()
print("ok  online update")

# --- 2. via the phone hotspot: AP off, join (curly apostrophe matched), update, AP back on
updater.save_hotspot("Jack's iPhone", "secret123")
src, newest = make_repos("hotspot")
u, cfg = new_updater("hotspot", src, online_after_hotspot=True)
fresh()
assert u.go() == 0, result(cfg)
c = calls()
order = [c.rindex(s) for s in ("connection down booth-ap", "ssid Jack’s iPhone",
                               "connection up booth-update-hotspot",
                               "connection delete booth-update-hotspot",
                               "connection up booth-ap", "systemctl restart")]
assert order == sorted(order), c
assert git("rev-parse", "--short", "HEAD", cwd=src) == newest
print("ok  update through the phone hotspot, booth Wi-Fi back on")

# --- 3. hotspot not there: nothing changed, booth Wi-Fi back on
updater.save_hotspot("Nobody's Phone", "secret123")
src, newest = make_repos("nohotspot")
before = git("rev-parse", "HEAD", cwd=src)
u, cfg = new_updater("nohotspot", src, online_after_hotspot=True)
updater.time.sleep, real_sleep = (lambda s: None), updater.time.sleep
fresh()
assert u.go() == 1
updater.time.sleep = real_sleep
assert result(cfg)["state"] == "failed" and "couldn't see the hotspot" in result(cfg)["message"]
assert git("rev-parse", "HEAD", cwd=src) == before
assert "connection up booth-ap" in calls() and "systemctl restart" in calls()
print("ok  hotspot missing: nothing changed, booth Wi-Fi back on")

# --- 3b. hotspot there but joining fails (wrong password): ONE attempt, then straight back
updater.save_hotspot("Jack's iPhone", "wrongpass")
src, newest = make_repos("badpass")
before = git("rev-parse", "HEAD", cwd=src)
u, cfg = new_updater("badpass", src, online_after_hotspot=True)
open(os.path.join(ROOT, "wrong-password"), "w").close()
fresh()
t0 = time.monotonic()
assert u.go() == 1
os.remove(os.path.join(ROOT, "wrong-password"))
c = calls()
assert c.count("connection up booth-update-hotspot") == 1, c
assert c.rindex("connection up booth-ap") > c.rindex("connection up booth-update-hotspot")
assert "connection delete booth-update-hotspot" in c
r = result(cfg)
assert r["state"] == "failed" and r["message"].startswith("Update failed") \
    and "wrong password" in r["message"], r
assert git("rev-parse", "HEAD", cwd=src) == before
assert time.monotonic() - t0 < 30
print("ok  wrong hotspot password: one try, booth Wi-Fi straight back, 'Update failed'")

# --- 4. new version doesn't keep running: previous one restored
src, newest = make_repos("rollback")
u, cfg = new_updater("rollback", src)
os.makedirs(os.path.join(u.app_dir, "booth"))
open(os.path.join(u.app_dir, "booth", "old-version"), "w").close()
open(os.path.join(u.app_dir, "VERSION"), "w").write("old")
real_run = updater.run


restarts = iter(range(100))


def crashing_run(cmd, *a, **kw):           # systemd has to restart it again and again
    if cmd[:2] == ["systemctl", "show"] and "NRestarts" in cmd:
        return 0, str(next(restarts))
    return real_run(cmd, *a, **kw)


fresh()
updater.run = crashing_run
assert u.go() == 1
updater.run = real_run
assert os.path.exists(os.path.join(u.app_dir, "booth", "old-version")), "previous version back"
assert "Update failed" in result(cfg)["message"] and "went back to the version it had" in result(cfg)["message"]
print("ok  new version failing: previous version restored")

# --- 4b. install fails part-way (via the hotspot): old version back, booth Wi-Fi back,
#         "Update failed" shown, and trying again works once fixed
updater.save_hotspot("Jack's iPhone", "secret123")
src, newest = make_repos("halfway", fail_install=True)
u, cfg = new_updater("halfway", src, online_after_hotspot=True)
os.makedirs(os.path.join(u.app_dir, "booth"))
open(os.path.join(u.app_dir, "booth", "old-version"), "w").close()
open(os.path.join(u.app_dir, "VERSION"), "w").write("old")
fresh()
assert u.go() == 1
r = result(cfg)
assert r["state"] == "failed" and r["message"].startswith("Update failed"), r
assert "went back to the version it had (old)" in r["message"], r
assert os.path.exists(os.path.join(u.app_dir, "booth", "old-version")), "old version back"
assert calls().rindex("connection up booth-ap") > calls().rindex("connection up booth-update-hotspot")
print("ok  install failing part-way: old version back, booth Wi-Fi back, 'Update failed'")

# --- 5. hotspot settings
for bad in (("", "x" * 8), ("a" * 33, "x" * 8), ("ok", "short")):
    try:
        updater.save_hotspot(*bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError:
        pass
assert oct(os.stat(updater.HOTSPOT_FILE).st_mode & 0o777) == "0o600"
print("ok  hotspot settings checked, password file private")

# ======================================================== the booth itself
import pygame  # noqa: E402

from booth.app import App  # noqa: E402

tmp = os.path.join(ROOT, "booth")
cfg = config.load(overrides=dict(config.dev_overrides(tmp), camera="test", log_file=False,
                                 max_video_size="640x480", web_port=0, countdown_from=1,
                                 saving_min_seconds=0.5, black_seconds=0.2))
os.makedirs(cfg["ssd_mount"])
pygame.display.init()
pygame.font.init()
app = App(cfg)
app.storage.start()
threading.Thread(target=app._prepare_camera, daemon=True).start()
launched = []
updater.launch = lambda cfg: launched.append(True)


def run(until, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end and app.running:
        app.step(time.monotonic())
        time.sleep(1 / 30)
        if until():
            return
    if not until():
        raise TimeoutError(app.state)


def press():
    app.button.key(True)
    app.step(time.monotonic())
    app.button.key(False)


run(lambda: app.state == "idle" and app.camera.ok)

# camera restart (mirror setting) waits while recording
press()
run(lambda: app.state == "recording")
proc = app.camera._proc
app.events.put(("camera_restart", None))
run(lambda: app.recorder.elapsed() > 1.0)
assert app.camera._proc is proc and app.recorder.alive, "camera restarted mid-recording"

# no update while recording
assert app.begin_update() is False and app.state == "recording"
print("ok  no camera restart or update while recording")

press()
run(lambda: app.state == "idle")
run(lambda: app.camera._proc is not proc and app.camera.ok, timeout=20)   # restarted once idle
app.storage.wait_idle(30)

# update from idle: pauses background work, marks ready, starts the updater
assert app.begin_update() is True
run(lambda: launched, timeout=30)
st = json.load(open(os.path.join(cfg["state_dir"], "status.json")))
assert app.state == "updating" and st["update_ready"] and app.storage.paused.is_set()
press()
end = time.monotonic() + 0.5
run(lambda: time.monotonic() > end)
assert app.state == "updating", "taps ignored while updating"
print("ok  update only from idle; booth paused and marked ready first")
app.events.put(("update_failed", "test"))
run(lambda: app.state == "idle")
assert not app.storage.paused.is_set()

# SSD filling up mid-recording: stopped and saved before it's full; next one on the microSD
assert app.storage.ssd_first and list(app.storage.targets()) == ["ssd"]
press()
run(lambda: app.state == "recording")
run(lambda: app.recorder.elapsed() > 1.2)
full_name = app.recorder.name
real_refresh = app.storage.refresh
app.storage.refresh = lambda first=False: None      # keep the fake "full" reading
app.storage.ssd.free = 1
run(lambda: app.state in ("stopping", "review"), timeout=5)
run(lambda: app.state == "idle", timeout=60)
assert media.probe(app.storage.ssd.path(full_name))["ok"], "recording saved, not cut off"
assert list(app.storage.targets()) == ["sd"]
press()
run(lambda: app.state == "recording")
assert list(app.recorder.parts) == ["sd"], app.recorder.parts
run(lambda: app.recorder.elapsed() > 1.0)
press()
run(lambda: app.state == "idle", timeout=60)
app.storage.refresh = real_refresh
app.storage.refresh()
print("ok  SSD full mid-recording: saved, then the microSD is used")

# being stopped mid-recording: the recording is saved first
press()
run(lambda: app.state == "recording")
run(lambda: app.recorder.elapsed() > 1.5)
name = app.recorder.name
app.term_requested = True
run(lambda: not app.running, timeout=60)
e = app.storage.manifest.entry(name)
paths = [d.path(name) for d in app.storage.drives if d.name in e["recorded_to"]]
assert paths
assert e and all(media.probe(p)["ok"] for p in paths), (e, paths)
print("ok  stopped mid-recording: the recording was saved first")

app.camera.stop()
pygame.quit()
shutil.rmtree(ROOT, ignore_errors=True)
print("update checks OK")
