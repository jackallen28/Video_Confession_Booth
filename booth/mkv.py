"""A tiny Matroska (MKV) writer for handing camera JPEGs to ffmpeg *with* the
time each one arrived from the camera.

ffmpeg's raw JPEG inputs don't keep real arrival times: when a webcam slows
down (dim light) or frames arrive in bursts, frames end up with the same or
made-up timestamps and the frame-rate filter drops and repeats them, so the
video looks jumpy. Matroska carries an exact time per frame instead.
"""

import struct

UNKNOWN = b"\x01\xff\xff\xff\xff\xff\xff\xff"      # "size not known" (live stream)


def _size(n):
    return b"\x01" + n.to_bytes(7, "big")


def _el(eid, payload):
    return eid + _size(len(payload)) + payload


def _uint(eid, v):
    return _el(eid, v.to_bytes(max(1, (v.bit_length() + 7) // 8), "big"))


def _str(eid, s):
    return _el(eid, s.encode())


def header(w, h):
    """EBML header, open-ended Segment, Info and one MJPEG video track (1 ms ticks)."""
    ebml = _el(b"\x1a\x45\xdf\xa3", _uint(b"\x42\x86", 1) + _uint(b"\x42\xf7", 1)
               + _uint(b"\x42\xf2", 4) + _uint(b"\x42\xf3", 8) + _str(b"\x42\x82", "matroska")
               + _uint(b"\x42\x87", 4) + _uint(b"\x42\x85", 2))
    info = _el(b"\x15\x49\xa9\x66", _uint(b"\x2a\xd7\xb1", 1_000_000)
               + _str(b"\x4d\x80", "booth") + _str(b"\x57\x41", "booth"))
    video = _el(b"\xe0", _uint(b"\xb0", w) + _uint(b"\xba", h))
    track = _el(b"\xae", _uint(b"\xd7", 1) + _uint(b"\x73\xc5", 1) + _uint(b"\x83", 1)
                + _str(b"\x86", "V_MJPEG") + video)
    return ebml + b"\x18\x53\x80\x67" + UNKNOWN + info + _el(b"\x16\x54\xae\x6b", track)


class Writer:
    """write(jpeg, ms) -> bytes to send. ms must not go backwards."""
    SPAN = 30_000          # start a new cluster before the 16-bit block offset overflows

    def __init__(self, w, h):
        self.head = header(w, h)
        self.cluster = None

    def frame(self, jpeg, ms):
        out = b""
        if self.head:
            out, self.head = self.head, b""
        if self.cluster is None or ms - self.cluster > self.SPAN:
            self.cluster = ms
            out += b"\x1f\x43\xb6\x75" + UNKNOWN + _uint(b"\xe7", ms)
        block = b"\x81" + struct.pack(">hB", ms - self.cluster, 0x80) + jpeg
        return out + _el(b"\xa3", block)
