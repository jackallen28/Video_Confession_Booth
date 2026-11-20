"""Waiting screens for the physical button ("Press button to begin recording"),
in reality-TV styles. Chosen on the web page: Screen -> Waiting screen.

    confessional  pink/magenta bokeh like a reality-show interview backdrop
    neon          flickering pink and blue neon sign on a dark wall
    spotlight     sweeping gold stage spotlights and sparkles
    drama         spinning hot-pink sunburst title card
    hand          the little hand pressing a glowing arcade button
    fessup        "FESS UP!": a paper star and the words stuck on, scrapbook style,
                  "Go on, press the button" pulsing; replays every ~13 s

Everything except fessup (artwork in assets/fessup) is drawn by the booth itself, sized for the
screen, with the slow parts drawn once at start so a Pi 3B+ stays smooth.
Fonts come from Debian packages installed by install.sh (Lobster, Bebas Neue,
Montserrat); without them it falls back to the default font.
"""

import glob
import math
import os
import random

import pygame
import pygame.gfxdraw

THEMES = ["confessional", "neon", "spotlight", "drama", "hand", "fessup"]
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

FONT_FILES = {
    "script": ["lobster.otf", "Lobster*.ttf", "Lobster*.otf"],
    "title": ["BebasNeue-Bold.otf", "BebasNeue-Regular.otf", "BebasNeue*.ttf"],
    "heavy": ["Montserrat-Black.ttf", "Montserrat-Black.otf", "LeagueSpartan-Black.otf"],
}
_font_paths = {}


def font_path(kind):
    if kind in _font_paths:
        return _font_paths[kind]
    dirs = ["/usr/share/fonts", "/usr/local/share/fonts"] + \
        [d for d in os.environ.get("BOOTH_FONT_DIRS", "").split(":") if d]
    found = None
    for pattern in FONT_FILES.get(kind, []):
        for d in dirs:
            hits = sorted(glob.glob(os.path.join(d, "**", pattern), recursive=True))
            if hits:
                found = hits[0]
                break
        if found:
            break
    _font_paths[kind] = found
    return found


def font(kind, size):
    path = font_path(kind)
    try:
        return pygame.font.Font(path, max(8, int(size)))
    except (OSError, pygame.error):
        return pygame.font.Font(None, max(8, int(size)))


def fit(kind, text, max_w, size):
    """Biggest font size (up to `size`) where `text` fits in max_w pixels."""
    f = font(kind, size)
    w = f.size(text)[0]
    if w > max_w:
        f = font(kind, size * max_w / w)
    return f


def glow(surf, colour, radius):
    """A soft coloured halo around the shape of surf (cheap blur: shrink + grow)."""
    w, h = surf.get_size()
    pad = int(radius * 2)
    base = pygame.Surface((w + 2 * pad, h + 2 * pad), pygame.SRCALPHA)
    mask = surf.copy()
    mask.fill((*colour, 0), special_flags=pygame.BLEND_RGBA_MAX)
    mask.fill((*colour, 255), special_flags=pygame.BLEND_RGBA_MIN)
    base.blit(mask, (pad, pad))
    k = max(1, int(radius / 2))
    small = pygame.transform.smoothscale(base, (max(1, base.get_width() // k),
                                                max(1, base.get_height() // k)))
    return pygame.transform.smoothscale(small, base.get_size()), pad


def text_with_glow(fnt, text, colour, glow_colour, radius, outline=None, outline_w=0):
    s = fnt.render(text, True, colour)
    if outline:
        o = fnt.render(text, True, outline)
        big = pygame.Surface((s.get_width() + 2 * outline_w, s.get_height() + 2 * outline_w),
                             pygame.SRCALPHA)
        for dx in range(-outline_w, outline_w + 1, max(1, outline_w // 2)):
            for dy in range(-outline_w, outline_w + 1, max(1, outline_w // 2)):
                if dx * dx + dy * dy <= outline_w * outline_w:
                    big.blit(o, (dx + outline_w, dy + outline_w))
        big.blit(s, (outline_w, outline_w))
        s = big
    g, pad = glow(s, glow_colour, radius)
    out = pygame.Surface(g.get_size(), pygame.SRCALPHA)
    out.blit(g, (0, 0))
    out.blit(g, (0, 0))
    out.blit(s, (pad, pad))
    return out


def soft_disc(r, colour, alpha):
    """A blurred circle (bokeh)."""
    size = int(r * 2.6)
    s = pygame.Surface((size, size), pygame.SRCALPHA)
    pygame.draw.circle(s, (*colour, alpha), (size // 2, size // 2), int(r))
    k = max(1, int(r / 6))
    small = pygame.transform.smoothscale(s, (max(1, size // k), max(1, size // k)))
    return pygame.transform.smoothscale(small, (size, size))


def vertical_gradient(size, top, bottom):
    w, h = size
    s = pygame.Surface((1, 64))
    for y in range(64):
        k = y / 63
        s.set_at((0, y), [int(top[i] + (bottom[i] - top[i]) * k) for i in range(3)])
    return pygame.transform.smoothscale(s, (w, h))


def vignette(size, strength=200):
    w, h = size
    small = pygame.Surface((64, 40), pygame.SRCALPHA)
    for y in range(40):
        for x in range(64):
            d = math.hypot((x - 31.5) / 32, (y - 19.5) / 20)
            small.set_at((x, y), (0, 0, 0, int(min(255, max(0, (d - 0.55) * 2.2) * strength))))
    return pygame.transform.smoothscale(small, (w, h))


def _ease_out_back(p, s=1.9):
    """0 -> 1 with a little overshoot at the end (lands, squashes, settles)."""
    p -= 1
    return 1 + (s + 1) * p ** 3 + s * p ** 2


def _shadow(sprite, alpha):
    """Same shape, dark and see-through: the paper's shadow while it's lifted."""
    sh = sprite.copy()
    sh.fill((25, 0, 4, 255), special_flags=pygame.BLEND_RGBA_MIN)   # dark, same outline
    sh.set_alpha(alpha)
    return sh


class WaitingScreen:
    def __init__(self, screen, cfg):
        self.screen = screen
        self.cfg = cfg
        self.W, self.H = screen.get_size()
        self.S = min(self.W, self.H)
        self._built = None
        self.rng = random.Random(7)

    # ------------------------------------------------------------- drawing
    def draw(self, t, theme, x_frac, glow_level):
        key = (theme, self.cfg.get("waiting_title"), self.cfg.get("waiting_subtitle"))
        if self._built != key:
            getattr(self, f"_build_{theme}")()
            self._built = key
        getattr(self, f"_draw_{theme}")(t)

    def _lines(self):
        return (self.cfg.get("waiting_title") or "Press button",
                self.cfg.get("waiting_subtitle") or "to begin recording")

    def _centre_text(self, surf, cy):
        self.screen.blit(surf, surf.get_rect(center=(self.W // 2, int(cy))))

    # ---------------------------------------------------- 1. confessional
    def _build_confessional(self):
        W, H, S = self.W, self.H, self.S
        self.bg = vertical_gradient((W, H), (40, 4, 38), (14, 0, 20))
        self.bokeh = []
        for _ in range(36):
            r = self.rng.uniform(0.05, 0.13) * S
            col = self.rng.choice([(255, 60, 200), (230, 40, 190), (255, 110, 230), (190, 30, 170)])
            sprite = soft_disc(r, col, self.rng.randint(70, 150))
            self.bokeh.append((sprite, self.rng.uniform(0, W), self.rng.uniform(0, H),
                               self.rng.uniform(-6, 6), self.rng.uniform(-4, 4),
                               self.rng.uniform(0, 6.3), self.rng.uniform(0.4, 1.0)))
        self.vig = vignette((W, H), 230)
        title, sub = self._lines()
        self.t1 = text_with_glow(fit("script", title, W * 0.86, S * 0.24), title,
                                 (255, 255, 255), (255, 60, 200), S * 0.03)
        self.t2 = text_with_glow(fit("title", sub.upper(), W * 0.8, S * 0.11), sub.upper(),
                                 (255, 200, 240), (255, 40, 180), S * 0.018)

    def _draw_confessional(self, t):
        self.screen.blit(self.bg, (0, 0))
        W, H = self.W, self.H
        for sprite, x, y, vx, vy, ph, sp in self.bokeh:
            px = (x + vx * t) % (W + sprite.get_width()) - sprite.get_width() / 2
            py = (y + vy * t) % (H + sprite.get_height()) - sprite.get_height() / 2
            a = int(160 + 95 * math.sin(t * sp + ph))
            sprite.set_alpha(a)
            self.screen.blit(sprite, (px - sprite.get_width() / 2, py - sprite.get_height() / 2),
                             special_flags=pygame.BLEND_RGB_ADD)
        self.screen.blit(self.vig, (0, 0))
        # "Press button" bobs slowly up and down; "to begin recording" stays still
        bob = math.sin(t * 2 * math.pi / 2.6) * self.S * 0.03
        self._centre_text(self.t1, H * 0.46 + bob)
        self._centre_text(self.t2, H * 0.71)

    # ------------------------------------------------------------ 2. neon
    def _build_neon(self):
        W, H, S = self.W, self.H, self.S
        bg = vertical_gradient((W, H), (12, 10, 26), (4, 4, 10))
        # faint brick wall
        bw, bh = int(S * 0.16), int(S * 0.07)
        for row in range(H // bh + 1):
            off = (bw // 2) if row % 2 else 0
            for col in range(-1, W // bw + 2):
                r = pygame.Rect(col * bw + off, row * bh, bw - 3, bh - 3)
                pygame.draw.rect(bg, (24, 20, 40), r, border_radius=2)
        bg.blit(vignette((W, H), 255), (0, 0))
        self.bg = bg
        title, sub = self._lines()
        f1 = fit("script", title, W * 0.8, S * 0.22)
        f2 = fit("title", sub.upper(), W * 0.72, S * 0.105)
        self.n1_on = text_with_glow(f1, title, (255, 235, 250), (255, 40, 170), S * 0.04)
        self.n1_off = text_with_glow(f1, title, (120, 40, 90), (60, 0, 40), S * 0.01)
        self.n2_on = text_with_glow(f2, sub.upper(), (225, 255, 255), (30, 200, 255), S * 0.03)
        # neon frame
        fw = max(self.n1_on.get_width(), self.n2_on.get_width()) + S * 0.08
        fh = H * 0.62
        frame = pygame.Surface((int(fw), int(fh)), pygame.SRCALPHA)
        pygame.draw.rect(frame, (200, 245, 255), frame.get_rect().inflate(-S * 0.08, -S * 0.08),
                         max(2, int(S * 0.008)), border_radius=int(S * 0.06))
        self.frame, _ = glow(frame, (40, 200, 255), S * 0.03)
        self.frame_line = frame

    def _draw_neon(self, t):
        self.screen.blit(self.bg, (0, 0))
        H = self.H
        # occasional flicker on the pink sign
        f = (t * 7.3) % 11
        on = not (3.0 < f < 3.15 or 3.3 < f < 3.4 or 7.6 < f < 7.7)
        fr = self.frame.get_rect(center=(self.W // 2, int(H * 0.56)))
        self.screen.blit(self.frame, fr)
        self.screen.blit(self.frame_line, self.frame_line.get_rect(center=fr.center))
        self._centre_text(self.n1_on if on else self.n1_off, H * 0.47)
        self._centre_text(self.n2_on, H * 0.70)

    # -------------------------------------------------------- 3. spotlight
    def _build_spotlight(self):
        W, H, S = self.W, self.H, self.S
        self.bg = vertical_gradient((W, H), (10, 8, 18), (30, 18, 6))
        floor = pygame.Surface((W, int(H * 0.25)), pygame.SRCALPHA)
        pygame.draw.ellipse(floor, (255, 200, 90, 50), floor.get_rect())
        self.floor, _ = glow(floor, (255, 190, 80), S * 0.06)
        self.sparkles = [(self.rng.uniform(0, W), self.rng.uniform(0, H * 0.9),
                          self.rng.uniform(0, 6.3), self.rng.uniform(0.6, 1.6),
                          self.rng.uniform(0.004, 0.011) * S) for _ in range(40)]
        title, sub = self._lines()
        self.s1 = text_with_glow(fit("heavy", title.upper(), W * 0.86, S * 0.16), title.upper(),
                                 (255, 214, 90), (255, 150, 30), S * 0.025,
                                 outline=(90, 50, 0), outline_w=max(2, int(S * 0.006)))
        self.s2 = text_with_glow(fit("script", sub, W * 0.7, S * 0.13), sub,
                                 (255, 255, 255), (255, 200, 120), S * 0.02)

    def _draw_spotlight(self, t):
        W, H, S = self.W, self.H, self.S
        self.screen.blit(self.bg, (0, 0))
        beams = pygame.Surface((W, H), pygame.SRCALPHA)
        for i, (bx, speed, ph) in enumerate(((0.12, 0.5, 0.0), (0.88, 0.43, 2.0), (0.5, 0.31, 4.0))):
            ang = math.sin(t * speed + ph) * 0.45
            ox, oy = W * bx, -S * 0.05
            length = H * 1.4
            spread = 0.17
            pts = [(ox, oy),
                   (ox + math.sin(ang - spread) * length, oy + math.cos(ang - spread) * length),
                   (ox + math.sin(ang + spread) * length, oy + math.cos(ang + spread) * length)]
            pygame.gfxdraw.filled_polygon(beams, pts, (255, 230, 160, 34))
        self.screen.blit(beams, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)
        self.screen.blit(self.floor, self.floor.get_rect(center=(W // 2, int(H * 0.98))))
        for x, y, ph, sp, r in self.sparkles:
            a = 0.5 + 0.5 * math.sin(t * sp * 3 + ph)
            if a > 0.55:
                rr = r * a
                c = (255, 235, 170)
                pygame.draw.line(self.screen, c, (x - rr * 2, y), (x + rr * 2, y), 2)
                pygame.draw.line(self.screen, c, (x, y - rr * 2), (x, y + rr * 2), 2)
        self._centre_text(self.s1, H * 0.46)
        self._centre_text(self.s2, H * 0.68)

    # ------------------------------------------------------------ 4. drama
    def _build_drama(self):
        W, H, S = self.W, self.H, self.S
        self.rays = 18
        self.ray_len = math.hypot(W, H)
        title, sub = self._lines()
        text = f"{title} {sub}".upper()
        words = text.split()
        half = (len(words) + 1) // 2
        l1, l2 = " ".join(words[:half]), " ".join(words[half:])
        ow = max(3, int(S * 0.01))
        self.d1 = text_with_glow(fit("title", l1, W * 0.86, S * 0.2), l1, (255, 236, 60),
                                 (0, 0, 0), S * 0.02, outline=(40, 0, 40), outline_w=ow)
        self.d2 = text_with_glow(fit("title", l2, W * 0.86, S * 0.2), l2, (255, 255, 255),
                                 (0, 0, 0), S * 0.02, outline=(40, 0, 40), outline_w=ow)
        self.vig = vignette((W, H), 160)

    def _draw_drama(self, t):
        W, H, S = self.W, self.H, self.S
        self.screen.fill((210, 20, 130))
        cx, cy = W / 2, H * 0.55
        a0 = t * 0.25
        for i in range(self.rays):
            if i % 2:
                continue
            a = a0 + i * 2 * math.pi / self.rays
            b = a + 2 * math.pi / self.rays
            pts = [(cx, cy), (cx + math.cos(a) * self.ray_len, cy + math.sin(a) * self.ray_len),
                   (cx + math.cos(b) * self.ray_len, cy + math.sin(b) * self.ray_len)]
            pygame.draw.polygon(self.screen, (150, 0, 120), pts)
        self.screen.blit(self.vig, (0, 0))
        bounce = abs(math.sin(t * 2.2)) * S * 0.015
        self._centre_text(self.d1, H * 0.42 - bounce)
        self._centre_text(self.d2, H * 0.64 - bounce)

    # ----------------------------------------------------------- 6. fess up
    # The artwork is 1024x600 (this booth's screen). Timeline, in seconds:
    #   0.15-0.85 the star is stuck on; 0.9-1.45 "FESS UP!"; 1.5-2.0 the small line;
    #   then the small line pulses softly for 10 s; 0.5 s fade; again.
    FESS_RED = (170, 18, 33)
    FESS_STEPS = 12                     # stop-motion frames per second while sticking on
    # (layer, starts at, takes, tilt it starts at, size it starts at)
    FESS = [("star", 0.15, 0.70, -28.0, 0.45), ("title", 0.90, 0.55, 9.0, 1.35),
            ("sub", 1.50, 0.50, -7.0, 1.35)]
    FESS_HOLD, FESS_FADE = 10.0, 0.5

    def _build_fessup(self):
        W, H = self.W, self.H
        folder = os.path.join(ASSETS, "fessup")
        art = {k: pygame.image.load(os.path.join(folder, k + ".png")).convert_alpha()
               for k in ("star", "title", "sub")}
        if H > W:                     # portrait screen: turn the star to fit it the same way
            art["star"] = pygame.transform.rotate(art["star"], -90)
        sw, sh = art["star"].get_size()
        cover = max(W / sw, H / sh)                      # the star fills the screen
        # the words as big as on a landscape screen of the same size (and always fitting)
        fit_ = min(max(W, H) / 1024, W * 0.88 / art["title"].get_width())
        place = {"star": (cover, (W / 2, H / 2)),
                 "title": (fit_, (W / 2 + 3.5 * fit_, H / 2 - 27 * fit_)),
                 "sub": (fit_, (W / 2 + 3.5 * fit_, H / 2 + 62.5 * fit_))}
        rng = random.Random(3)
        self.fess = {}
        for name, start, dur, tilt, zoom0 in self.FESS:
            scale, centre = place[name]
            img = pygame.transform.smoothscale(
                art[name], (max(1, round(art[name].get_width() * scale)),
                            max(1, round(art[name].get_height() * scale))))
            frames = []
            n = max(2, round(dur * self.FESS_STEPS))
            for i in range(n):
                p = (i + 1) / n
                e = _ease_out_back(p)
                last = i == n - 1
                # tilted, a different size and casting a shadow -> pressed down flat
                zoom = 1.0 if last else zoom0 + (1 - zoom0) * e
                angle = 0.0 if last else tilt * (1 - e) + rng.uniform(-1.2, 1.2)
                jx, jy = (0, 0) if last else (rng.uniform(-2, 2), rng.uniform(-2, 2))
                spr = img if last else pygame.transform.rotozoom(img, angle, zoom)
                shadow = None if last else _shadow(spr, 70 if name == "star" else 55)
                off = self.S * 0.035 * (1 - min(1.0, e))
                frames.append((spr, shadow, off, (centre[0] + jx, centre[1] + jy)))
            self.fess[name] = (start, dur, frames, img, centre)
        # once everything is stuck on, the still parts are one picture
        self.fess_base = pygame.Surface((W, H)).convert()
        self.fess_base.fill(self.FESS_RED)
        for name in ("star", "title"):
            _, _, _, img, centre = self.fess[name]
            self.fess_base.blit(img, img.get_rect(center=(round(centre[0]), round(centre[1]))))
        self.fess_t0 = None
        self.fess_last = -1e9

    def _draw_fessup(self, t):
        if t - self.fess_last > 0.5:            # waiting screen just came back: start over
            self.fess_t0 = t
        self.fess_last = t
        built = self.FESS[-1][1] + self.FESS[-1][2]       # all stuck on
        cycle = built + self.FESS_HOLD + self.FESS_FADE + 0.15
        lt = (t - self.fess_t0) % cycle
        scr = self.screen
        if lt >= built:
            # all on: the still picture, and the small line pulsing softly
            scr.blit(self.fess_base, (0, 0))
            _, _, _, img, (cx, cy) = self.fess["sub"]
            k = 1 + 0.045 * (0.5 - 0.5 * math.cos((lt - built) * 2 * math.pi / 1.6))
            spr = pygame.transform.smoothscale(img, (round(img.get_width() * k),
                                                     round(img.get_height() * k)))
            spr.set_alpha(int(255 - 50 * (k - 1) / 0.045))
            scr.blit(spr, spr.get_rect(center=(round(cx), round(cy))))
            fade = (lt - built - self.FESS_HOLD) / self.FESS_FADE
            if fade > 0:                         # off again before the next round
                veil = pygame.Surface(scr.get_size())
                veil.fill(self.FESS_RED)
                veil.set_alpha(int(255 * min(1.0, fade)))
                scr.blit(veil, (0, 0))
            return
        scr.fill(self.FESS_RED)
        for name, start, dur, _, _ in self.FESS:
            if lt < start:
                break
            _, _, frames, img, centre = self.fess[name]
            i = min(len(frames) - 1, int((lt - start) * self.FESS_STEPS))
            spr, shadow, off, (cx, cy) = frames[i]
            rect = spr.get_rect(center=(round(cx), round(cy)))
            if shadow is not None:
                scr.blit(shadow, rect.move(round(off * 0.6), round(off)))
            scr.blit(spr, rect)                  # paper: solid from the first frame

    # ------------------------------------------------------------- 5. hand
    def _build_hand(self):
        pass

    def _draw_hand(self, t):
        pass                        # drawn by UI.press_hint
