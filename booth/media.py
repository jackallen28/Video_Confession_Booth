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


def _read_chunks(path, throttle=None, abort=None):
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
    """Reads the file from the drive once: SHA-256 of the bytes, and every
    audio/video packet run through ffmpeg's demuxer to get a hash of the
    content (equal for two good copies even if the container bytes differ)."""
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(low_priority() + [
        "ffmpeg", "-v", "error", "-nostdin", "-f", "matroska", "-i", "pipe:0",
        "-map", "0", "-c", "copy", "-f", "hash", "-hash", "sha256", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
    h = hashlib.sha256()
    size = 0
    try:
        for b in _read_chunks(path, throttle, abort):
            h.update(b)
            size += len(b)
            try:
                proc.stdin.write(b)
            except BrokenPipeError:
                pass          # ffmpeg gave up; keep hashing, report below
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
    out = proc.stdout.read().decode(errors="replace")
    proc.wait()
    err.seek(0)
    errors = [l for l in err.read().decode(errors="replace").splitlines() if l.strip()]
    m = re.search(r"SHA256=([0-9a-f]{64})", out)
    return {"sha256": h.hexdigest(), "size": size,
            "content": m.group(1) if m else None,
            "demux_errors": len(errors), "demux_ok": proc.returncode == 0 and m is not None,
            "error_sample": errors[:3]}


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


def human_bytes(n):
    if n is None:
        return "—"
    gb = n / 1e9
    if gb >= 100:
        return f"{gb:.0f} GB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / 1e6:.0f} MB"
