"""Everything drawn on the screen. Sizes scale with the screen height, so it
works on 480x320, 800x480, 1024x600 or full HD screens."""

import math

import pygame
import pygame.gfxdraw

from .media import human_bytes

WHITE = (245, 245, 245)
RED = (236, 32, 32)
GREEN = (46, 178, 96)
AMBER = (255, 186, 52)
GREY = (125, 125, 125)
LEVEL_COLOURS = {"ok": WHITE, "warn": AMBER, "error": (255, 72, 72), "missing": GREY}


def ease_out(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3


def ease_in(x):
    x = max(0.0, min(1.0, x))
    return x * x * x


class UI:
    def __init__(self, screen, cfg):
        self.screen = screen
        self.W, self.H = screen.get_size()
        self.S = min(self.W, self.H)      # size unit: works in portrait too
        self.u = self.S / 480.0
        self.cfg = cfg
        self._fonts = {}
        self._texts = {}
        self.sb = max(26, int(self.S * 0.072))           # status bar height
        self._build_clapper()

    # ---------------------------------------------------------------- helpers
    def word(self, key):
        """On-screen text; with the touch screen in use, the 'tap' wording."""
        if self.cfg.get("input", "touch") != "button" and "touch_" + key in self.cfg:
            return self.cfg["touch_" + key]
        return self.cfg[key]

    def px(self, v):
        return max(1, int(round(v * self.u)))

    def font(self, size):
        size = max(10, int(size))
        if size not in self._fonts:
            self._fonts[size] = pygame.font.Font(None, size)
        return self._fonts[size]

    def text(self, s, size, colour=WHITE):
        key = (s, int(size), colour)
        surf = self._texts.get(key)
        if surf is None:
            if len(self._texts) > 300:
                self._texts.clear()
            surf = self._texts[key] = self.font(size).render(s, True, colour)
        return surf

    def blit_text(self, s, size, colour=WHITE, shadow=True, alpha=255, **pos):
        surf = self.text(s, size, colour)
        rect = surf.get_rect(**pos)
        if shadow:
            sh = self.text(s, size, (0, 0, 0))
            sh = sh.copy()
            sh.set_alpha(int(150 * alpha / 255))
            off = max(1, int(size / 22))
            self.screen.blit(sh, rect.move(off, off))
        if alpha < 255:
            surf = surf.copy()
            surf.set_alpha(alpha)
        self.screen.blit(surf, rect)
        return rect

    def disc(self, colour, centre, r, alpha=255, target=None):
        target = target or self.screen
        r = max(1, int(r))
        s = pygame.Surface((2 * r + 4, 2 * r + 4), pygame.SRCALPHA)
        c = (*colour[:3], int(alpha))
        pygame.gfxdraw.filled_circle(s, r + 2, r + 2, r, c)
        pygame.gfxdraw.aacircle(s, r + 2, r + 2, r, c)
        target.blit(s, (int(centre[0]) - r - 2, int(centre[1]) - r - 2))

    def ring(self, colour, centre, r_out, width, alpha=255, start=0.0, end=360.0, target=None):
        """Thick (anti-aliased) arc; angles clockwise from 12 o'clock, degrees."""
        target = target or self.screen
        if end - start <= 0.5:
            return
        if end - start >= 359.9:
            surf = self._full_ring(colour, r_out, width)
            surf.set_alpha(int(alpha))
            target.blit(surf, surf.get_rect(center=(int(centre[0]), int(centre[1]))))
            return
        r_in = max(0, r_out - width)
        steps = max(8, int((end - start) / 3))
        pts_out, pts_in = [], []
        size = int(r_out * 2 + 6)
        cx = cy = size / 2
        for i in range(steps + 1):
            a = math.radians(start + (end - start) * i / steps)
            sa, ca = math.sin(a), -math.cos(a)
            pts_out.append((cx + r_out * sa, cy + r_out * ca))
            pts_in.append((cx + r_in * sa, cy + r_in * ca))
        pts = pts_out + pts_in[::-1]
        s = pygame.Surface((size, size), pygame.SRCALPHA)
        c = (*colour[:3], int(alpha))
        pygame.gfxdraw.filled_polygon(s, pts, c)
        pygame.gfxdraw.aapolygon(s, pts, c)
        target.blit(s, (int(centre[0] - cx), int(centre[1] - cy)))

    def _full_ring(self, colour, r_out, width):
        key = ("ring", colour, int(r_out * 2), int(width * 2))
        surf = self._texts.get(key)
        if surf is None:
            k = 3        # draw 3x larger, then shrink = smooth edges
            size = int(r_out * 2 * k + 6)
            big = pygame.Surface((size, size), pygame.SRCALPHA)
            c = size // 2
            pygame.draw.circle(big, (*colour[:3], 255), (c, c), int(r_out * k))
            pygame.draw.circle(big, (0, 0, 0, 0), (c, c), max(0, int((r_out - width) * k)))
            surf = pygame.transform.smoothscale(big, (size // k, size // k))
            self._texts[key] = surf
        return surf.copy()

    def pie(self, colour, centre, r, frac, alpha):
        if frac <= 0:
            return
        size = int(2 * r + 4)
        s = pygame.Surface((size, size), pygame.SRCALPHA)
        c = size / 2
        pts = [(c, c)]
        steps = max(4, int(frac * 120))
        for i in range(steps + 1):
            a = 2 * math.pi * frac * i / steps
            pts.append((c + r * math.sin(a), c - r * math.cos(a)))
        if len(pts) >= 3:
            pygame.gfxdraw.filled_polygon(s, pts, (*colour, int(alpha)))
        self.screen.blit(s, (int(centre[0] - c), int(centre[1] - c)))

    def rrect(self, colour, rect, alpha, radius):
        s = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(s, (*colour, int(alpha)), s.get_rect(), border_radius=int(radius))
        self.screen.blit(s, rect.topleft)

    # ---------------------------------------------------------------- preview
    def preview(self, frame, size, rect=None):
        """Camera picture into rect (default: the whole screen)."""
        rect = rect or self.screen.get_rect()
        if rect.size != (self.W, self.H) or frame is None:
            self.screen.fill((0, 0, 0) if frame is not None else (14, 14, 16))
        if frame is None:
            return
        img = pygame.image.frombuffer(frame, size, "RGB")
        if size != rect.size:
            img = pygame.transform.scale(img, rect.size)
        self.screen.blit(img, rect.topleft)

    # ------------------------------------------------------------------- idle
    def button_centre(self):
        r = self.S * 0.075
        text_h = self.S * 0.045
        cy = self.H - self.sb - self.S * 0.045 - text_h - self.S * 0.025 - r
        return (self.W / 2, cy), r

    def idle(self, t, pressed):
        (cx, cy), r = self.button_centre()
        # soft pulse rings
        for k in (0.0, 0.5):
            p = ((t / 2.0) + k) % 1.0
            self.ring(WHITE, (cx, cy), r * (1.05 + 0.65 * p), self.px(3), alpha=110 * (1 - p))
        rr = r * (0.86 if pressed else 1.0)
        self.disc((0, 0, 0), (cx + self.px(2), cy + self.px(3)), rr, alpha=70)
        self.disc(RED, (cx, cy), rr, alpha=255 if pressed else 215)
        self.disc((255, 120, 120), (cx - rr * 0.25, cy - rr * 0.3), rr * 0.35, alpha=45)
        self.ring(WHITE, (cx, cy), rr + self.px(3), self.px(4), alpha=225)
        self.blit_text(self.word("idle_text"), self.S * 0.06, WHITE,
                       midtop=(cx, cy + r + self.S * 0.03))

    def hold(self, progress):
        c = (self.W / 2, self.H / 2 - self.sb / 2)
        r = self.S * 0.17
        self.disc((0, 0, 0), c, r * 1.12, alpha=170)
        self.ring(WHITE, c, r, self.px(10), alpha=55)
        self.ring(WHITE, c, r, self.px(10), alpha=240, end=360 * max(0.0, min(1.0, progress)))
        self.power_icon(c, r * 0.42, WHITE)

    def power_icon(self, c, r, colour, alpha=255):
        w = max(3, int(r * 0.2))
        self.ring(colour, c, r, w, alpha=alpha, start=35, end=325)
        line = pygame.Rect(0, 0, w, int(r * 1.05))
        line.midbottom = (int(c[0]), int(c[1] + r * 0.05))
        line.top = int(c[1] - r - w * 0.3)
        self.rrect(colour, line, alpha, w / 2)

    # -------------------------------------------------------------- countdown
    def countdown(self, number, frac):
        c = (self.W / 2, self.H / 2 - self.sb / 2)
        R = self.S * 0.30
        self.disc((0, 0, 0), c, R * 1.04, alpha=120)
        self.pie(WHITE, c, R * 0.96, frac, alpha=60)
        for a in (0, 90):            # cross hairs, like a film leader
            if a == 0:
                rect = pygame.Rect(0, 0, self.px(2), int(2 * R * 0.96))
            else:
                rect = pygame.Rect(0, 0, int(2 * R * 0.96), self.px(2))
            rect.center = (int(c[0]), int(c[1]))
            self.rrect(WHITE, rect, 70, 0)
        self.ring(WHITE, c, R, self.px(6), alpha=235)
        self.ring(WHITE, c, R * 0.82, self.px(2), alpha=120)
        pop = 1 + 0.45 * (1 - ease_out(frac / 0.28))
        base = self.text(str(number), self.S * 0.44)
        size = (int(base.get_width() * pop), int(base.get_height() * pop))
        num = pygame.transform.smoothscale(base, size)
        num.set_alpha(int(255 * ease_out(frac / 0.15)))
        rect = num.get_rect(center=(int(c[0]), int(c[1] + self.S * 0.012)))
        self.screen.blit(num, rect)

    # -------------------------------------------------------------- clapper
    CLAP_AT = 0.42      # seconds into the animation when the sticks close
    CLAP_END = 1.6

    def _build_clapper(self):
        H = self.S
        self.bw, self.bh, self.sh = int(H * 0.46), int(H * 0.29), int(H * 0.075)
        bw, bh, sh = self.bw, self.bh, self.sh
        border = max(2, int(H * 0.006))
        slate = pygame.Surface((bw, bh), pygame.SRCALPHA)
        pygame.draw.rect(slate, (26, 26, 30, 255), slate.get_rect(), border_radius=int(H * 0.012))
        pygame.draw.rect(slate, (235, 235, 235, 255), slate.get_rect(), border,
                         border_radius=int(H * 0.012))
        lc = (235, 235, 235, 150)
        for fy in (0.36, 0.68):
            y = int(bh * fy)
            pygame.draw.line(slate, lc, (int(bw * 0.06), y), (int(bw * 0.94), y), max(1, border // 2))
        pygame.draw.line(slate, lc, (bw // 2, int(bh * 0.36)), (bw // 2, int(bh * 0.68)),
                         max(1, border // 2))
        self.slate = slate

        def stick(flip):
            s = pygame.Surface((bw, sh), pygame.SRCALPHA)
            s.fill((240, 240, 240, 255))
            sw = bw / 7.0
            x = -sh
            while x < bw + sh:
                pts = [(x, sh), (x + sw / 2, sh), (x + sw / 2 + sh * 0.8, 0), (x + sh * 0.8, 0)]
                pygame.draw.polygon(s, (20, 20, 22, 255), pts)
                x += sw
            if flip:
                s = pygame.transform.flip(s, True, False)
            pygame.draw.rect(s, (20, 20, 22, 255), s.get_rect(), border)
            return s
        self.stick_top, self.stick_bottom = stick(False), stick(True)
        headroom = bw * math.sin(math.radians(34)) + sh
        self.cw = int(max(bw * 1.9, H * 0.8))
        self.slate_y = int(H * 0.02 + headroom + sh)
        self.ch = int(self.slate_y + bh + H * 0.2)

    def clapper(self, t, lead=0.0):
        """t = seconds since the animation started; lead = extra seconds the open
        clapperboard is held before it snaps (when it replaces the countdown).
        Returns False when done."""
        if t > 0.30:
            t = 0.30 if t < 0.30 + lead else t - lead
        if t > self.CLAP_END:
            return False
        H = self.S
        canvas = pygame.Surface((self.cw, self.ch), pygame.SRCALPHA)
        sx = (self.cw - self.bw) // 2
        rise = ease_out(t / 0.3)
        jolt = self.px(5) if self.CLAP_AT <= t < self.CLAP_AT + 0.06 else 0
        sy = int(self.slate_y + (1 - rise) * H * 0.12) + jolt
        canvas.blit(self.slate, (sx, sy))
        canvas.blit(self.stick_bottom, (sx, sy - self.sh))
        if t < 0.30:
            angle = 34.0
        else:
            angle = 34.0 * (1 - ease_in((t - 0.30) / (self.CLAP_AT - 0.30)))
        pivot = pygame.math.Vector2(sx, sy - self.sh)
        img = self.stick_top
        # rotate around the hinge (bottom-left corner of the top stick)
        offset = pygame.math.Vector2(self.bw / 2, -self.sh / 2).rotate(-angle)
        rot = pygame.transform.rotate(img, angle)
        canvas.blit(rot, rot.get_rect(center=pivot + offset))
        if t >= self.CLAP_AT:
            k = (t - self.CLAP_AT) / 0.14
            size = H * 0.16
            base = self.text(self.cfg["action_text"], size, (255, 214, 64))
            scale = 1 + 0.6 * (1 - ease_out(k))
            img = pygame.transform.smoothscale(
                base, (int(base.get_width() * scale), int(base.get_height() * scale)))
            shadow = pygame.transform.smoothscale(
                self.text(self.cfg["action_text"], size, (0, 0, 0)), img.get_size())
            shadow.set_alpha(150)
            mid = (self.cw // 2, sy + self.bh + int(H * 0.11))
            canvas.blit(shadow, shadow.get_rect(center=(mid[0] + self.px(3), mid[1] + self.px(3))))
            canvas.blit(img, img.get_rect(center=mid))
        alpha = ease_out(t / 0.12) * (1 - ease_in((t - 1.2) / (self.CLAP_END - 1.2)) if t > 1.2 else 1)
        canvas.set_alpha(int(255 * alpha))
        self.screen.blit(canvas, ((self.W - self.cw) // 2, (self.H - self.sb - self.ch) // 2))
        if self.CLAP_AT <= t < self.CLAP_AT + 0.3:      # white flash
            f = pygame.Surface((self.W, self.H))
            f.fill((255, 255, 255))
            f.set_alpha(int(170 * (1 - (t - self.CLAP_AT) / 0.3)))
            self.screen.blit(f, (0, 0))
        return True

    # ------------------------------------------------------------- recording
    def recording(self, elapsed, remaining, t):
        m, s = divmod(int(elapsed), 60)
        txt = f"{m}:{s:02d}"
        size = self.S * 0.068
        colour = AMBER if remaining < 30 else WHITE
        label = self.text(txt, size, colour)
        r = self.S * 0.021
        pad = self.S * 0.022
        h = int(max(label.get_height(), 2 * r) + pad * 1.1)
        w = int(pad + 2 * r + pad * 0.8 + label.get_width() + pad)
        rect = pygame.Rect(int(self.S * 0.03), int(self.S * 0.03), w, h)
        self.rrect((0, 0, 0), rect, 120, h / 2)
        if (t % 1.0) < 0.6:
            self.disc((255, 40, 40), (rect.x + pad + r, rect.centery), r)
        self.screen.blit(label, label.get_rect(midleft=(int(rect.x + pad + 2 * r + pad * 0.8),
                                                         rect.centery + self.px(1))))

    # ---------------------------------------------------------------- saving
    def saving(self, t):
        c = (self.W / 2, self.H / 2 - self.sb / 2)
        r = self.S * 0.085
        self.disc((0, 0, 0), c, r * 1.25, alpha=150)
        a = (t * 300) % 360
        self.ring(WHITE, c, r, self.px(7), alpha=60)
        self.ring(WHITE, c, r, self.px(7), alpha=240, start=a, end=a + 100)

    def saved(self, ok, t):
        c = (self.W / 2, self.H / 2 - self.sb / 2)
        r = self.S * 0.09 * (0.6 + 0.4 * ease_out(t / 0.18))
        self.disc((0, 0, 0), (c[0], c[1] + self.px(3)), r * 1.05, alpha=90)
        self.disc(GREEN if ok else RED, c, r)
        w = max(3, int(r * 0.16))
        cx, cy = c
        if ok:
            pts = [(cx - r * 0.42, cy + r * 0.02), (cx - r * 0.1, cy + r * 0.34),
                   (cx + r * 0.45, cy - r * 0.32)]
            pygame.draw.lines(self.screen, WHITE, False, pts, w)
            for p in pts:
                pygame.draw.circle(self.screen, WHITE, (int(p[0]), int(p[1])), w // 2)
        else:
            d = r * 0.36
            pygame.draw.line(self.screen, WHITE, (cx - d, cy - d), (cx + d, cy + d), w)
            pygame.draw.line(self.screen, WHITE, (cx - d, cy + d), (cx + d, cy - d), w)

    # ------------------------------------------------------- stop: TV switch-off
    TV_OFF = 0.85

    def tv_off(self, frozen, t):
        """Old CRT switching off: picture squashes to a bright line, the line
        shrinks to a dot, the dot fades. frozen = last camera picture."""
        self.screen.fill((0, 0, 0))
        W, H = self.W, self.H
        cy = H // 2
        if t < 0.25 and frozen is not None:
            p = ease_in(t / 0.25)
            h = max(2, int(H * (1 - p)))
            img = pygame.transform.scale(frozen, (W, h))
            glow = pygame.Surface((W, h))
            glow.fill((255, 255, 255))
            glow.set_alpha(int(230 * p))
            img.blit(glow, (0, 0))
            self.screen.blit(img, (0, cy - h // 2))
        elif t < 0.45:
            p = ease_in((t - 0.25) / 0.2)
            w = max(4, int(W * (1 - p)))
            th = max(2, self.px(3))
            self.rrect(WHITE, pygame.Rect(W // 2 - w // 2, cy - th * 2, w, th * 4), 70, th * 2)
            self.rrect((255, 255, 255), pygame.Rect(W // 2 - w // 2, cy - th // 2, w, th), 255, 0)
        elif t < self.TV_OFF:
            p = (t - 0.45) / (self.TV_OFF - 0.45)
            r = self.S * 0.012 * (1 - 0.5 * p)
            self.disc(WHITE, (W / 2, cy), r * 4, alpha=110 * (1 - p))
            self.disc(WHITE, (W / 2, cy), r, alpha=255 * (1 - p))

    # ------------------------------------------------------------ saving screen
    def fade(self, alpha):
        """Black over everything; alpha 0..255."""
        if alpha <= 0:
            return
        f = pygame.Surface((self.W, self.H))
        f.fill((0, 0, 0))
        f.set_alpha(int(min(255, alpha)))
        self.screen.blit(f, (0, 0))

    def saving_screen(self, title, progress, hint, hold, ok=None, t=0.0):
        """title, a loading bar (0..1), the delete hint and the delete-hold ring
        (hold 0..1, or None). ok: None while saving, True/False when done."""
        self.screen.fill((0, 0, 0))
        W, H = self.W, self.H
        colour = WHITE if ok is not False else (255, 90, 90)
        title_rect = self.blit_text(title, self.S * 0.095, colour, shadow=False,
                                    center=(W / 2, H * 0.34))
        bw, bh = int(min(W * (0.62 if W > H else 0.82), self.S * 1.1)), max(10, int(self.S * 0.045))
        bar = pygame.Rect(0, 0, bw, bh)
        bar.center = (W // 2, int(H * 0.5))
        pygame.draw.rect(self.screen, (90, 90, 90), bar, max(2, self.px(2)), border_radius=bh // 2)
        inner = bar.inflate(-self.px(8), -self.px(8))
        fill = inner.copy()
        fill.width = int(inner.width * max(0.0, min(1.0, progress)))
        if fill.width > 2:
            fill_colour = GREEN if ok else (255, 90, 90) if ok is False else WHITE
            pygame.draw.rect(self.screen, fill_colour, fill, border_radius=inner.height // 2)
            if ok is None:      # moving shine on the bar
                x = fill.x + (t * 1.2 % 1.0) * fill.width
                shine = pygame.Rect(int(x), fill.y, max(4, fill.height), fill.height)
                self.rrect((255, 255, 255), shine.clip(fill), 90, fill.height // 2)
        if ok:
            self.check_mark((bar.right + bh * 1.4, bar.centery), bh * 0.9, GREEN)
        if hint:
            hy = H * 0.68
            hc = (170, 170, 170) if hold is None else (255, 110, 110)
            r = self.blit_text(hint, self.S * 0.05, hc, shadow=False, center=(W / 2, hy))
            if hold is not None:
                c = (r.left - self.S * 0.055, hy)
                self.ring((255, 80, 80), c, self.S * 0.032, self.px(5), alpha=70)
                self.ring((255, 80, 80), c, self.S * 0.032, self.px(5), alpha=255, end=360 * hold)
        return title_rect

    def check_mark(self, c, size, colour):
        w = max(3, int(size * 0.18))
        cx, cy = c
        pts = [(cx - size * 0.45, cy), (cx - size * 0.12, cy + size * 0.33),
               (cx + size * 0.48, cy - size * 0.35)]
        pygame.draw.lines(self.screen, colour, False, pts, w)
        for p in pts:
            pygame.draw.circle(self.screen, colour, (int(p[0]), int(p[1])), w // 2)

    def trash_icon(self, c, size, colour):
        cx, cy = c
        s = size
        w = max(2, int(s * 0.09))
        scr = self.screen
        lid = pygame.Rect(0, 0, int(s * 0.9), w * 2)
        lid.midbottom = (int(cx), int(cy - s * 0.32))
        pygame.draw.rect(scr, colour, lid, border_radius=w)
        handle = pygame.Rect(0, 0, int(s * 0.32), int(s * 0.14) + w)
        handle.midbottom = (int(cx), lid.top + w)
        pygame.draw.rect(scr, colour, handle, w, border_radius=w)
        body = [(cx - s * 0.36, cy - s * 0.27), (cx + s * 0.36, cy - s * 0.27),
                (cx + s * 0.29, cy + s * 0.5), (cx - s * 0.29, cy + s * 0.5)]
        pygame.draw.polygon(scr, colour, body, w)
        for k in (-0.14, 0, 0.14):
            pygame.draw.line(scr, colour, (cx + s * k, cy - s * 0.13),
                             (cx + s * k * 0.85, cy + s * 0.37), max(2, w - 1))

    def confirm_delete(self, text, left, t):
        """left = 1..0, time left to press again."""
        self.screen.fill((0, 0, 0))
        c = (self.W / 2, self.H * 0.38)
        pulse = 0.5 + 0.5 * math.sin(t * 9)
        r = self.S * 0.15
        self.disc((200, 30, 30), c, r, alpha=150 + 90 * pulse)
        self.ring(WHITE, c, r * 1.12, self.px(5), alpha=80)
        self.ring(WHITE, c, r * 1.12, self.px(5), alpha=230, end=360 * max(0.0, left))
        self.trash_icon(c, r * 1.05, WHITE)
        self.blit_text(text, self.S * 0.075, WHITE, shadow=False, center=(self.W / 2, self.H * 0.7))

    def deleted(self, text):
        self.screen.fill((0, 0, 0))
        self.trash_icon((self.W / 2, self.H * 0.38), self.S * 0.2, (160, 160, 160))
        self.blit_text(text, self.S * 0.075, (200, 200, 200), shadow=False,
                       center=(self.W / 2, self.H * 0.68))

    def starting(self, t):
        self.screen.fill((0, 0, 0))
        c = (self.W / 2, self.H / 2)
        a = (t * 300) % 360
        self.ring(WHITE, c, self.S * 0.06, self.px(6), alpha=50)
        self.ring(WHITE, c, self.S * 0.06, self.px(6), alpha=220, start=a, end=a + 100)

    # ------------------------------------------------------------ status bar
    def drive_icon(self, x, cy, h, colour, kind):
        """SSD = small box with a light; SD = card with a cut corner."""
        if kind == "ssd":
            w = int(h * 1.55)
            rect = pygame.Rect(x, int(cy - h / 2), w, h)
            pygame.draw.rect(self.screen, colour, rect, max(2, h // 7), border_radius=max(2, h // 5))
            pygame.draw.circle(self.screen, colour, (rect.right - h // 3 - 1, rect.centery),
                               max(2, h // 8))
            return w
        w = int(h * 0.8)
        top, left = int(cy - h / 2), x
        cut = h * 0.3
        pts = [(left, top), (left + w - cut, top), (left + w, top + cut),
               (left + w, top + h), (left, top + h)]
        pygame.draw.polygon(self.screen, colour, pts, max(2, h // 7))
        for i in range(3):
            lx = left + w * (0.25 + 0.2 * i)
            pygame.draw.line(self.screen, colour, (lx, top + h * 0.18), (lx, top + h * 0.38),
                             max(1, h // 10))
        return w

    def status_bar(self, snap, mic_ok, t):
        sb = self.sb
        bar = pygame.Rect(0, self.H - sb, self.W, sb)
        self.rrect((0, 0, 0), bar, 135, 0)
        h = int(sb * 0.5)
        cy = bar.centery
        x = int(sb * 0.45)
        size = sb * 0.74
        for kind in ("ssd", "sd"):
            d = snap.get(kind, {})
            colour = LEVEL_COLOURS.get(d.get("level"), GREY)
            x += self.drive_icon(x, cy, h, colour, kind) + int(sb * 0.22)
            label = human_bytes(d.get("free")) if d.get("present") else "—"
            r = self.blit_text(label, size, colour, shadow=False, midleft=(x, cy + 1))
            x = r.right + int(sb * 0.7)
        # right side: background copying / problems
        x = self.W - int(sb * 0.45)
        if snap.get("damaged"):
            x -= self.warn_icon(x, cy, h, (255, 72, 72))
        if snap.get("undervolt"):
            x -= self.bolt_icon(x, cy, h)
        if not mic_ok:
            x -= self.mic_off_icon(x, cy, h)
        if snap.get("pending"):
            r = h * 0.5
            a = (t * 240) % 360
            self.ring(WHITE, (x - r, cy), r, max(2, h // 6), alpha=200, start=a, end=a + 270)
            x -= int(2 * r + sb * 0.3)

    def warn_icon(self, right, cy, h, colour):
        w = int(h * 1.1)
        pts = [(right - w / 2, cy - h / 2), (right, cy + h / 2), (right - w, cy + h / 2)]
        pygame.draw.polygon(self.screen, colour, pts)
        pygame.draw.line(self.screen, (0, 0, 0), (right - w / 2, cy - h * 0.12),
                         (right - w / 2, cy + h * 0.2), max(2, h // 7))
        pygame.draw.circle(self.screen, (0, 0, 0), (int(right - w / 2), int(cy + h * 0.36)),
                           max(1, h // 12))
        return w + int(self.sb * 0.3)

    def bolt_icon(self, right, cy, h):
        w = h * 0.7
        x0, y0 = right - w, cy - h / 2
        pts = [(x0 + w * 0.6, y0), (x0, y0 + h * 0.58), (x0 + w * 0.45, y0 + h * 0.58),
               (x0 + w * 0.3, y0 + h), (x0 + w, y0 + h * 0.4), (x0 + w * 0.55, y0 + h * 0.4)]
        pygame.draw.polygon(self.screen, AMBER, pts)
        return int(w + self.sb * 0.3)

    def mic_off_icon(self, right, cy, h):
        w = int(h * 0.55)
        body = pygame.Rect(0, 0, w, int(h * 0.75))
        body.midtop = (int(right - h / 2), int(cy - h / 2))
        pygame.draw.rect(self.screen, AMBER, body, border_radius=w // 2)
        pygame.draw.line(self.screen, AMBER, (body.centerx, body.bottom),
                         (body.centerx, cy + h / 2), max(2, h // 8))
        pygame.draw.line(self.screen, (255, 72, 72), (right - h, cy + h / 2), (right, cy - h / 2),
                         max(2, h // 6))
        return h + int(self.sb * 0.3)

    # ---------------------------------------------------------- other screens
    def no_camera(self, t):
        self.screen.fill((16, 16, 18))
        c = (self.W / 2, self.H / 2 - self.sb / 2)
        a = int(140 + 80 * math.sin(t * 2.5))
        bw, bh = self.S * 0.26, self.S * 0.17
        body = pygame.Rect(0, 0, int(bw), int(bh))
        body.center = (int(c[0]), int(c[1]))
        bump = pygame.Rect(0, 0, int(bw * 0.3), int(bh * 0.22))
        bump.midbottom = (int(c[0] - bw * 0.15), body.top + 2)
        self.rrect(GREY, bump, a, bh * 0.06)
        self.rrect(GREY, body, a, bh * 0.16)
        self.disc((16, 16, 18), c, bh * 0.32)
        self.ring(GREY, c, bh * 0.32, self.px(5), alpha=a)
        pygame.draw.line(self.screen, (255, 72, 72), (c[0] - bw * 0.6, c[1] + bh * 0.75),
                         (c[0] + bw * 0.6, c[1] - bh * 0.75), self.px(8))

    def shutdown(self, stage, t):
        self.screen.fill((0, 0, 0))
        if stage == "off":
            return
        c = (self.W / 2, self.H * 0.38)
        self.power_icon(c, self.S * 0.09, WHITE, alpha=200)
        h = int(self.S * 0.07)
        y = int(self.H * 0.66)
        w = int(h * 1.55)
        x0 = int(self.W / 2 - (w + h * 1.6) / 2)
        colour = {"ejected": GREEN, "failed": RED}.get(stage, WHITE)
        self.drive_icon(x0, y, h, colour, "ssd")
        ic = (x0 + w + h, y)
        if stage == "ejected":
            pygame.draw.lines(self.screen, GREEN, False,
                              [(ic[0] - h * 0.4, ic[1]), (ic[0] - h * 0.1, ic[1] + h * 0.3),
                               (ic[0] + h * 0.45, ic[1] - h * 0.35)], max(3, h // 6))
        elif stage == "failed":
            d = h * 0.35
            pygame.draw.line(self.screen, RED, (ic[0] - d, ic[1] - d), (ic[0] + d, ic[1] + d), max(3, h // 6))
            pygame.draw.line(self.screen, RED, (ic[0] - d, ic[1] + d), (ic[0] + d, ic[1] - d), max(3, h // 6))
        else:
            a = (t * 300) % 360
            self.ring(WHITE, ic, h * 0.42, max(3, h // 7), alpha=230, start=a, end=a + 100)
