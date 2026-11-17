"""Short snippets of the recording, played behind the saving bar.

While someone records, a few short moments (about 1.2 s each) are kept in
memory, spread over the whole recording, from the live preview frames the
booth already has (so nothing has to be decoded afterwards). Small copies
only: a few dozen MB at most, fine on a Pi 3B+.

How they stay spread out without knowing how long the recording will be: a
snippet is started every `interval` seconds; when there are twice as many as
needed, every other one is dropped and the interval doubles.
"""

import pygame

MAX_W = 400          # stored frame width (scaled up when shown)


class Highlights:
    def __init__(self, fps, keep=4, seconds=1.2, interval=3.0):
        self.fps = max(1.0, float(fps))
        self.keep = keep
        self.per = max(2, int(round(seconds * self.fps)))   # frames per snippet
        self.first_interval = interval
        self.reset()

    def reset(self):
        self.snippets = []        # [[start_t, [Surface, ...]], ...]
        self.interval = self.first_interval
        self.next_t = 0.6         # skip the very first moment (still getting settled)
        self.play = []            # frames chosen for playback
        self.recording = False

    def start(self):
        self.reset()
        self.recording = True

    def add(self, frame, size, t, mirrored=False):
        """frame: RGB bytes of a preview frame (size); t: seconds since recording began."""
        if not self.recording or frame is None:
            return
        cur = self.snippets[-1] if self.snippets else None
        if cur is None or len(cur[1]) >= self.per:
            if t < self.next_t:
                return
            cur = [t, []]
            self.snippets.append(cur)
            self.next_t = t + self.interval
            if len(self.snippets) > 2 * self.keep:
                self.snippets = self.snippets[::2]     # keep them spread over the whole time
                self.interval *= 2
                self.next_t = self.snippets[-1][0] + self.interval
                cur = self.snippets[-1]
                if len(cur[1]) >= self.per:
                    return
        img = pygame.image.frombuffer(frame, size, "RGB")
        w, h = size
        k = min(1.0, MAX_W / w)
        small = pygame.transform.scale(img, (max(2, int(w * k)), max(2, int(h * k))))
        if mirrored:          # the screen is a mirror; the recording isn't
            small = pygame.transform.flip(small, True, False)
        cur[1].append(small)

    def finish(self):
        """Stop collecting and pick up to `keep` snippets spread over the recording."""
        self.recording = False
        full = [s for s in self.snippets if len(s[1]) >= max(2, self.per // 3)]
        if len(full) > self.keep:
            step = len(full) / self.keep
            full = [full[int(i * step)] for i in range(self.keep)]
        self.play = [f for _, frames in full for f in frames]
        self.snippets = []

    def frame(self, t):
        """Frame to show t seconds into playback (loops), or None."""
        if not self.play:
            return None
        return self.play[int(t * self.fps) % len(self.play)]
