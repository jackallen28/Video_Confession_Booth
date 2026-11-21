"""File checks: ffprobe, packet hashes, decode tests, SHA-256, safe writes."""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

log = logging.getLogger("booth.media")

CHUNK = 1 << 20


class Aborted(Exception):
    pass


def low_priority():
    """Command prefix so background checks never slow down the live camera."""
    pre = []
    if shutil.which("nice"):
        pre += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        pre += ["ionice", "-c", "3"]
    return pre


def nice(n):
    """Command prefix that runs a program at lower CPU priority (n: 1-19)."""
    return ["nice", "-n", str(n)] if shutil.which("nice") else []


def drop_cache(path):
    """Forget the cached copy of a file so the next read really comes from the drive."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass


def fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def write_atomic(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(d)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def probe(path, timeout=30):
    """-> {"ok", "duration", "video", "audio"}"""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type", "-of", "json", path],
                           capture_output=True, timeout=timeout)
        info = json.loads(r.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"ok": False, "error": str(e), "duration": 0.0, "video": False, "audio": False}
    kinds = [s.get("codec_type") for s in info.get("streams", [])]
    try:
        dur = float(info.get("format", {}).get("duration", 0) or 0)
    except ValueError:
        dur = 0.0
    return {"ok": r.returncode == 0 and "video" in kinds and dur > 0.2,
            "duration": round(dur, 2), "video": "video" in kinds, "audio": "audio" in kinds}


def _read_chunks(path, throttle=None, abort=None, drop=True):
    if drop:
        drop_cache(path)
    with open(path, "rb") as f:
        while True:
            if abort is not None and abort.is_set():
                raise Aborted()
            t = time.monotonic()
            b = f.read(CHUNK)
            if not b:
                return
            yield b
            if throttle:
                limit = throttle()       # bytes per second, or None
                if limit:
                    wait = len(b) / limit - (time.monotonic() - t)
                    if wait > 0:
                        time.sleep(wait)


def sha256_file(path, throttle=None, abort=None):
    h = hashlib.sha256()
    for b in _read_chunks(path, throttle, abort):
        h.update(b)
    return h.hexdigest()


def check_copy(path, throttle=None, abort=None):
    """Reads the file back from the drive: SHA-256 of the bytes, and every
    audio/video packet run through ffmpeg's demuxer for a hash of the content
    (two good copies have the same content hash)."""
    drop_cache(path)
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(low_priority() + [
        "ffmpeg", "-v", "error", "-nostdin", "-i", path,
        "-map", "0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-"],
        stdout=subprocess.PIPE, stderr=err)
    try:
        h = hashlib.sha256()
        size = 0
        for b in _read_chunks(path, throttle, abort, drop=False):
            h.update(b)
            size += len(b)
        out = proc.communicate(timeout=600)[0].decode(errors="replace")
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    err.seek(0)
    errors = [l for l in err.read().decode(errors="replace").splitlines() if l.strip()]
    m = re.search(r"SHA256=([0-9a-f]{64})", out)
    return {"sha256": h.hexdigest(), "size": size,
            "content": m.group(1) if m else None,
            "demux_errors": len(errors), "demux_ok": proc.returncode == 0 and m is not None,
            "error_sample": errors[:3]}


def creation_time(name):
    """'2026-11-20_21-04-33.mp4' (local time) -> '2026-11-20T10:04:33Z' (UTC), for
    the date inside the MP4, which Photos and Finder use. None if not a date."""
    m = re.match(r"(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)", os.path.basename(name))
    if not m:
        return None
    try:
        t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S"))
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    except (ValueError, OverflowError):
        return None


def remux_to_mp4(src, dst, timeout=900, when=None):
    """Recording (MPEG-TS) -> standard MP4 (H.264 + AAC) that plays on
    iPhone/Mac. No re-encoding. Written via dst.tmp, fsynced, then renamed."""
    tmp = dst + ".tmp"
    # the exact start time if known (no daylight-saving guesswork), else the name
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when)) if when \
        else creation_time(dst)
    meta = ["-metadata", f"creation_time={when}"] if when else []
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", src,
                            "-map", "0:v?", "-map", "0:a?", "-c", "copy"] + meta + [
                            # Fill in decode times the Pi's hardware encoder can leave
                            # out (Apple players refuse those files; Chrome doesn't mind).
                            "-bsf:v", r"setts=dts=if(eq(DTS\,NOPTS)\,PTS\,DTS)",
                            # 'avc1' video in an Apple-friendly MP4 ("mp42"), with the
                            # index at the front so it plays straight away
                            "-tag:v", "avc1", "-brand", "mp42",
                            "-movflags", "+faststart",
                            "-f", "mp4", tmp], capture_output=True, timeout=timeout)
        if r.returncode != 0:
            raise OSError(r.stderr.decode(errors="replace").strip()[-300:] or "ffmpeg failed")
        fsync_file(tmp)
        os.replace(tmp, dst)
        fsync_dir(os.path.dirname(dst))
        return True, ""
    except (OSError, subprocess.SubprocessError) as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, str(e)


def decode_check(path, timeout=3600):
    """Decodes every frame (slow, low priority). -> (ok, error_count)"""
    try:
        r = subprocess.run(low_priority() + [
            "ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-i", path,
            "-map", "0", "-f", "null", "-"], capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False, -1
    # Count decoder complaints only (not the null muxer's timestamp notes).
    n = len([l for l in r.stderr.decode(errors="replace").splitlines()
             if l.strip() and "[null @" not in l and "Last message repeated" not in l])
    # A handful of glitchy webcam frames is normal; the file is still fine.
    return r.returncode == 0 and n <= 5, n


def copy_file(src, dst, throttle=None, abort=None):
    """Copies src to dst (via dst.part), fsyncs, then re-reads dst from the
    drive. Returns the SHA-256 of the data now on the drive at dst."""
    part = dst + ".part"
    try:
        with open(part, "wb") as out:
            for b in _read_chunks(src, throttle, abort):
                out.write(b)
            out.flush()
            os.fsync(out.fileno())
        digest = sha256_file(part, throttle, abort)
        os.replace(part, dst)
        fsync_dir(os.path.dirname(dst))
        return digest
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def iphone_issues(path):
    """Reasons an MP4 may not play on iPhone/Safari/QuickTime ([] = fine)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=codec_type,codec_name,codec_tag_string,profile,level,pix_fmt,"
                            "is_avc,extradata_size,width,height", "-of", "json", path],
                           capture_output=True, timeout=60)
        streams = json.loads(r.stdout or b"{}").get("streams", [])
        r2 = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "packet=pts,dts", "-of", "csv=p=0", path],
                            capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return [f"could not check: {e}"]
    issues = []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        return ["no video"]
    if v.get("codec_name") != "h264":
        issues.append(f"video is {v.get('codec_name')}, not H.264")
    if v.get("codec_tag_string") != "avc1":
        issues.append(f"video tag {v.get('codec_tag_string')} (needs avc1)")
    if str(v.get("is_avc")) != "true" or not int(v.get("extradata_size") or 0):
        issues.append("H.264 settings (SPS/PPS) missing from the MP4 header")
    if v.get("profile") not in ("Constrained Baseline", "Baseline", "Main", "High"):
        issues.append(f"H.264 profile {v.get('profile')}")
    if v.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
        issues.append(f"colour format {v.get('pix_fmt')} (needs 4:2:0)")
    if int(v.get("level") or 0) > 52:
        issues.append(f"H.264 level {v.get('level')}")
    if int(v.get("width") or 0) % 2 or int(v.get("height") or 0) % 2:
        issues.append("odd picture size")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if a and a.get("codec_name") != "aac":
        issues.append(f"sound is {a.get('codec_name')}, not AAC")
    bad_ts, prev = 0, None
    for line in r2.stdout.decode(errors="replace").splitlines():
        pts, _, dts = line.partition(",")
        if not dts.strip().lstrip("-").isdigit():
            bad_ts += 1
            continue
        d = int(dts)
        if prev is not None and d <= prev:
            bad_ts += 1
        prev = d
    if bad_ts:
        issues.append(f"{bad_ts} frames with missing or repeated timestamps")
    return issues


def human_bytes(n):
    if n is None:
        return "—"
    gb = n / 1e9
    if gb >= 100:
        return f"{gb:.0f} GB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / 1e6:.0f} MB"
