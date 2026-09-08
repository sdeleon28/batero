"""Drawing: the play field, flashes, HUD, results. Also shared fonts and helpers."""
import math
import time

import pygame

from .chart import COUNT_LABELS, HH_GLYPH
from .game import CONTRAST_TARGET, Game
from .ghost import PEDAL_CLOSED_CC, openness_label

LOOKAHEAD_S = 2.0        # seconds of chart visible above the line at speed 1.0
FLASH_S = 0.18           # duration of the hit flash
GLOW_H = 220             # height of the lane glow above the hit line, in px
GLOW_STRENGTH = 0.16     # how far the lane background moves toward the judgement color
JUDGE_TEXT_S = 0.6       # how long the judgement word stays up
MISS_FADE_S = 0.6        # missed notes fade out below the line for this long
HIT_LINE_FRAC = 0.82     # vertical position of the hit line (fraction of window height)
NOTE_H = 16
MAX_LANE_W = 260         # lanes never get wider than this; few lanes sit centered

BG = (14, 14, 18)
LANE_BG = (22, 22, 28)
LANE_EDGE = (40, 40, 50)
LINE = (235, 235, 235)
TEXT = (220, 220, 220)
DIM = (120, 120, 130)
ACCENT = (80, 200, 230)
JUDGE_COLORS = {
    "PERFECT": (80, 230, 140),
    "GOOD": (240, 210, 70),
    "OK": (240, 140, 60),
    "MISS": (230, 70, 70),
    "STRAY": (170, 90, 200),
}
DYN_COLORS = {"ACCENT": (255, 255, 255), "TAP": (150, 200, 160), "SOFT": (240, 140, 60), "LOUD": (240, 140, 60)}
DYN_LABELS = {"ACCENT": "ACCENT", "TAP": "tap", "SOFT": "no accent!", "LOUD": "too loud!"}
ACCENT_NOTE_SCALE = 1.7   # accented notes are this much taller


def lerp(a, b, k):
    k = max(0.0, min(1.0, k))
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


OPENNESS_COLORS = {"tight": (245, 90, 90), "mid": (250, 200, 60), "open": (110, 220, 110)}


STAR = (250, 200, 60)
STAR_OFF = (60, 60, 72)


def draw_stars(surf, fonts, stars, right_x, y, S, size="small", pop=None):
    """Five stars ending at right_x, `stars` of them lit. pop: seconds since the stars
    appeared (the lit ones scale in one after another). Returns the width drawn."""
    r = (7 if size == "small" else 16) * S
    gap = (6 if size == "small" else 14) * S
    w = 5 * (2 * r) + 4 * gap
    x0 = right_x - w
    for i in range(5):
        cx = x0 + r + i * (2 * r + gap)
        lit = i < stars
        k = 1.0
        if pop is not None and lit:
            k = max(0.0, min(1.0, (pop - 0.25 * i) / 0.25))
            k = 1.0 + 0.6 * math.sin(math.pi * k) if k < 1 else 1.0
            if pop - 0.25 * i < 0:
                lit = False
        pts = []
        for j in range(10):
            ang = -math.pi / 2 + j * math.pi / 5
            rad = r * k if j % 2 == 0 else r * k * 0.45
            pts.append((cx + rad * math.cos(ang), y + r + rad * math.sin(ang)))
        pygame.draw.polygon(surf, STAR if lit else STAR_OFF, pts)
    return w


def draw_hihat_state(surf, fonts, ghosts, cx, cy, S=1.0, color=(245, 230, 80)):
    """Two cymbals whose gap follows CC4, the openness class, the last stroke's zone.
    (cx, cy) is the centre of the widget; it is about 120 x 110 px at S = 1."""
    if ghosts is None:
        return
    cc = ghosts.pedal_cc
    closed = max(0.0, min(1.0, cc / PEDAL_CLOSED_CC))
    label = openness_label(cc)
    ocol = OPENNESS_COLORS[label]
    now = time.perf_counter()
    w, hh = 88 * S, 12 * S
    gap = (2 + 26 * (1 - closed)) * S
    top_y = cy - gap / 2 - hh
    bot_y = cy + gap / 2
    flash = 0.0
    if ghosts.last_stroke and now - ghosts.last_stroke[0] < 0.25:
        flash = 1 - (now - ghosts.last_stroke[0]) / 0.25
    rim = lerp(color, (255, 255, 255), flash)
    pygame.draw.ellipse(surf, lerp(LANE_BG, color, 0.55), (cx - w / 2, top_y, w, hh))
    pygame.draw.ellipse(surf, rim, (cx - w / 2, top_y, w, hh), max(1, int(2 * S)))
    pygame.draw.ellipse(surf, lerp(LANE_BG, color, 0.35), (cx - w / 2, bot_y, w, hh))
    pygame.draw.ellipse(surf, color, (cx - w / 2, bot_y, w, hh), max(1, int(2 * S)))
    pygame.draw.line(surf, DIM, (cx, top_y - 10 * S), (cx, bot_y + hh + 6 * S), max(1, int(2 * S)))
    fonts.center(surf, f"{label.upper()}  {cc}", fonts.small, ocol, bot_y + hh + 20 * S, cx)
    if ghosts.last_stroke and now - ghosts.last_stroke[0] < 1.5:
        _, note, vel, zone, op = ghosts.last_stroke
        fonts.center(surf, f"{op} {zone} {note} v{vel}", fonts.small, lerp(TEXT, BG, (now - ghosts.last_stroke[0]) / 1.5),
                     bot_y + hh + 38 * S, cx)
    elif ghosts.last_ghost and now - ghosts.last_ghost[0] < 1.5:
        _, note, vel, why = ghosts.last_ghost
        fonts.center(surf, f"ignored {note} v{vel}: {why}", fonts.small, lerp(DIM, BG, (now - ghosts.last_ghost[0]) / 1.5),
                     bot_y + hh + 38 * S, cx)


class Fonts:
    """Font set for a given window scale (1.0 = 720 px tall)."""

    def __init__(self, scale=1.0):
        pygame.font.init()
        name = pygame.font.match_font("menlo,monaco,dejavusansmono,consolas,couriernew") or None
        self.scale = scale
        px = lambda n: max(8, round(n * scale))
        self.small = pygame.font.Font(name, px(16))
        self.mid = pygame.font.Font(name, px(24))
        self.large = pygame.font.Font(name, px(36))
        self.big = pygame.font.Font(name, px(56))
        self.huge = pygame.font.Font(name, px(88))
        self.cache = {}

    def text(self, s, font, color):
        key = (s, id(font), color)
        surf = self.cache.get(key)
        if surf is None:
            surf = font.render(s, True, color)
            if len(self.cache) > 1024:
                self.cache.clear()
            self.cache[key] = surf
        return surf

    def center(self, surf, s, font, color, y, x=None):
        ts = self.text(s, font, color)
        x = surf.get_width() / 2 if x is None else x
        surf.blit(ts, (x - ts.get_width() / 2, y - ts.get_height() / 2))
        return ts.get_height()


class Renderer:
    def __init__(self, game: Game, size, fonts: Fonts, ghosts=None):
        self.ghosts = ghosts
        self.game = game
        self.w, self.h = size
        self.f = fonts
        self.judge_surfs = {k: fonts.big.render(k, True, c) for k, c in JUDGE_COLORS.items()}
        self.s = self.h / 720                            # pixel scale relative to the 720p design
        n = len(game.lanes)
        margin = 40 * self.s
        self.lane_w = min((self.w - 2 * margin) / n, MAX_LANE_W * self.s)
        left = (self.w - self.lane_w * n) / 2
        self.lane_x = [left + i * self.lane_w for i in range(n)]
        self.line_y = int(self.h * HIT_LINE_FRAC)
        self.note_h = int(NOTE_H * self.s)
        self.glow_h = int(GLOW_H * self.s)
        self.pps = (self.line_y - 60 * self.s) / LOOKAHEAD_S     # pixels per second at speed 1.0
        self.finished_at = None                                  # set by the play screen for the star animation

    def y_for(self, note_t, now):
        return self.line_y - (note_t - now) * self.pps * self.game.speed

    def draw(self, surf, fps=0.0):
        g = self.game
        f = self.f
        S = self.s
        wall = time.perf_counter()
        with g.lock:
            now = g.song_time(wall)
            flashes = list(g.flashes)
            lo = g.cursor
            paused, finished = g.paused, g.finished
            score, combo, counts, offset, speed = g.score, g.combo, dict(g.counts), g.offset_ms, g.speed

        surf.fill(BG)
        for i, lane in enumerate(g.lanes):
            x = int(self.lane_x[i])
            pygame.draw.rect(surf, LANE_BG, (x, 0, int(self.lane_w) - 2, self.h))
            pygame.draw.line(surf, LANE_EDGE, (x, 0), (x, self.h))
            cx = x + self.lane_w / 2
            f.center(surf, lane.label, f.small, lane.color, self.line_y + 34 * S, cx)
            sub = "/".join(map(str, sorted(lane.notes))) if lane.notes else "no pad assigned"
            f.center(surf, sub, f.small, DIM if lane.notes else JUDGE_COLORS["MISS"], self.line_y + 52 * S, cx)

        # flashes: lane glow first so notes draw on top
        for fl in flashes:
            age = (wall - fl.wall_t) / FLASH_S
            if age <= 1:
                glow = lerp(LANE_BG, JUDGE_COLORS[fl.judge], GLOW_STRENGTH * (1 - age))
                pygame.draw.rect(surf, glow, (int(self.lane_x[fl.lane]), self.line_y - self.glow_h, int(self.lane_w) - 2, self.glow_h))

        pygame.draw.line(surf, LINE, (self.lane_x[0], self.line_y), (self.lane_x[-1] + self.lane_w, self.line_y), max(2, int(3 * S)))
        for i, lane in enumerate(g.lanes):
            cx = int(self.lane_x[i] + self.lane_w / 2)
            pygame.draw.circle(surf, lane.color, (cx, self.line_y), int(9 * S), 2)

        # notes: from a bit before the cursor so missed ones can fade out
        top = now + LOOKAHEAD_S / speed + 0.2
        for n in g.notes[max(0, lo - 64):]:
            if n.t > top:
                break
            if n.state == "hit":
                continue
            y = self.y_for(n.t, now)
            if y > self.h + self.note_h:
                continue
            x = int(self.lane_x[n.lane] + 6 * S)
            w = int(self.lane_w - 14 * S)
            color = g.lanes[n.lane].color
            if n.state == "miss":
                age = (now - n.t) / MISS_FADE_S
                if age > 1:
                    continue
                color = lerp(JUDGE_COLORS["MISS"], BG, age)
            accent = n.accent or (not g.chart.dynamics and n.velocity >= 100)
            nh = int(self.note_h * ACCENT_NOTE_SCALE) if accent else self.note_h
            if g.chart.dynamics and not accent:
                x, w = x + int(w * 0.15), int(w * 0.7)        # taps: narrower and dimmer
                if n.state != "miss":
                    color = lerp(color, LANE_BG, 0.35)
            rect = (x, int(y - nh / 2), w, nh)
            pygame.draw.rect(surf, color, rect, border_radius=int(6 * S))
            if accent:
                pygame.draw.rect(surf, (255, 255, 255), rect, 2, border_radius=int(6 * S))
            if n.state != "miss":
                if n.art:
                    label = HH_GLYPH.get(n.art, "")                 # + tight, / mid, o open, > edge, ^ chick
                else:
                    label = (">" + n.hand if accent and n.hand else (">" if accent else n.hand))
                if label:
                    f.center(surf, label, f.mid if (accent or n.art) else f.small, (20, 20, 24), y, x + w / 2)

        # flashes: ring at the line + error number, drawn the frame after the hit arrives
        latest = None
        newest_in_lane = {}                      # the dynamic tag is shown for the newest flash per lane only
        for fl in flashes:
            newest_in_lane[fl.lane] = fl
        for fl in flashes:
            age = (wall - fl.wall_t) / FLASH_S
            cx = int(self.lane_x[fl.lane] + self.lane_w / 2)
            color = JUDGE_COLORS[fl.judge]
            if age <= 1:
                k = 1 - age
                pygame.draw.circle(surf, lerp(BG, color, k), (cx, self.line_y), int((14 + 50 * age) * S), max(1, int(6 * k * S)))
                pygame.draw.circle(surf, color, (cx, self.line_y), int(9 * S))
            jt = (wall - fl.wall_t) / JUDGE_TEXT_S
            if jt <= 1:
                latest = fl
                if fl.error_ms is not None:
                    label = f"{'+' if fl.error_ms >= 0 else '-'}{abs(fl.error_ms):.0f}"
                    ts = f.text(label, f.mid, lerp(color, BG, jt))
                    surf.blit(ts, (cx - ts.get_width() / 2, self.line_y - (70 + 40 * jt) * S))
                if fl.dyn and newest_in_lane[fl.lane] is fl:
                    ds = f.text(DYN_LABELS[fl.dyn], f.mid if fl.dyn in ("SOFT", "LOUD") else f.small, lerp(DYN_COLORS[fl.dyn], BG, jt))
                    surf.blit(ds, (cx - ds.get_width() / 2, self.line_y - (44 + 40 * jt) * S))
                if fl.art and newest_in_lane[fl.lane] is fl:
                    want, ok = fl.art
                    col = DYN_COLORS["ACCENT"] if ok else DYN_COLORS["SOFT"]
                    ds = f.text(want if ok else f"want {want}", f.small if ok else f.mid, lerp(col, BG, jt))
                    surf.blit(ds, (cx - ds.get_width() / 2, self.line_y - (44 + 40 * jt) * S))

        if latest is not None:
            jt = (wall - latest.wall_t) / JUDGE_TEXT_S
            js = self.judge_surfs[latest.judge]
            js.set_alpha(int(255 * (1 - jt ** 2)))
            surf.blit(js, (self.w / 2 - js.get_width() / 2, self.h * 0.30 - 10 * jt * S))
            js.set_alpha(255)
            if latest.error_ms is not None:
                e = latest.error_ms
                label = "on time" if abs(e) < 0.5 else f"{abs(e):.0f} ms {'early' if e < 0 else 'late'}"
                es = f.text(label, f.mid, JUDGE_COLORS[latest.judge])
                es.set_alpha(int(255 * (1 - jt)))
                surf.blit(es, (self.w / 2 - es.get_width() / 2, self.h * 0.30 + 60 * S))
                es.set_alpha(255)
            if latest.dyn in ("SOFT", "LOUD"):
                ds = f.text("ACCENT MISSING" if latest.dyn == "SOFT" else "TAP TOO LOUD", f.mid, DYN_COLORS[latest.dyn])
                ds.set_alpha(int(255 * (1 - jt)))
                surf.blit(ds, (self.w / 2 - ds.get_width() / 2, self.h * 0.30 + 90 * S))
                ds.set_alpha(255)
            elif latest.art and not latest.art[1]:
                ds = f.text(f"HAT: {latest.art[0].upper()}", f.mid, DYN_COLORS["SOFT"])
                ds.set_alpha(int(255 * (1 - jt)))
                surf.blit(ds, (self.w / 2 - ds.get_width() / 2, self.h * 0.30 + 90 * S))
                ds.set_alpha(255)

        self.metronome(surf, now)
        if any(l.key == "hihat" for l in g.lanes):
            draw_hihat_state(surf, f, self.ghosts, self.w - 110 * S, 200 * S, S)

        # HUD with a backing so it stays readable over notes
        dyn = g.dynamics(32) if g.chart.dynamics else None
        expr = g.chart.expression
        backing = pygame.Surface((int(360 * S), int((150 if dyn is not None else (140 if expr else 100)) * S)))
        backing.fill(BG)
        backing.set_alpha(235)
        surf.blit(backing, (0, 0))
        if dyn is not None:
            self.dynamics_meter(surf, dyn, 12 * S, 96 * S)
        elif expr:
            a = g.art_counts
            surf.blit(f.text(f"hat articulations {a['ok']}/{a['ok'] + a['wrong']}",
                             f.small, TEXT if a["wrong"] == 0 else JUDGE_COLORS["OK"]), (12 * S, 96 * S))
            surf.blit(f.text("+ tight   / mid   o open   > edge   ^ foot", f.small, DIM), (12 * S, 116 * S))
        surf.blit(f.text(g.chart.name, f.mid, ACCENT), (12 * S, 8 * S))
        surf.blit(f.text(f"score {score}   combo {combo}", f.mid, TEXT), (12 * S, 38 * S))
        line = f"P {counts['PERFECT']}  G {counts['GOOD']}  O {counts['OK']}  M {counts['MISS']}  S {counts['STRAY']}"
        if self.ghosts is not None and self.ghosts.filtered:
            line += f"  ghosts {self.ghosts.filtered}"
        surf.blit(f.text(line, f.small, DIM), (12 * S, 70 * S))
        right = [f"{fps:5.0f} fps", f"offset {offset:+.0f} ms",
                 f"tempo {g.chart.rate:.2f}x" if g.chart.rate != 1.0 else "tempo 1x",
                 f"{g.chart.bpm:.0f} bpm" + (f" (of {g.chart.bpm / g.chart.rate:.0f})" if g.chart.rate != 1.0 else ""),
                 f"guide {'on' if g.guide else 'off'}" + ("" if g.sounds is None or g.sounds.drums else "  ·  drums off"),
                 f"backing {'on' if g.track_enabled('backing') else 'off'}" if 'backing' in g.tracks else "no backing",
                 f"metronome {g.metronome_mode}"]
        for i, s in enumerate(right):
            ts = f.text(s, f.small, DIM)
            surf.blit(ts, (self.w - ts.get_width() - 12 * S, (10 + i * 20) * S))

        if paused:
            f.center(surf, "PAUSED", f.huge, TEXT, self.h * 0.45)
            f.center(surf, "space resume · R restart · Esc menu", f.small, DIM, self.h * 0.45 + 70 * S)
        elif now < 0:
            beats_left = math.ceil(-now / g.beat)
            f.center(surf, str((beats_left - 1) % 4 + 1), f.huge, TEXT, self.h * 0.45)
            f.center(surf, g.chart.desc, f.mid, DIM, self.h * 0.45 + 80 * S)
        if finished:
            self.results(surf)

    def dynamics_meter(self, surf, dyn, x, y):
        """Accent and tap tallies plus a contrast bar (median accent / median tap velocity
        over the last strokes) with the target marked."""
        f, S = self.f, self.s
        dc = self.game.dyn_counts
        line = f"accents {dc['ACCENT']}/{dc['ACCENT'] + dc['SOFT']}  taps {dc['TAP']}/{dc['TAP'] + dc['LOUD']}"
        surf.blit(f.text(line, f.small, TEXT), (x, y))
        c = dyn.get("contrast")
        bw, bh = 200 * S, 10 * S
        by = y + 26 * S
        pygame.draw.rect(surf, LANE_BG, (x, by, bw, bh), border_radius=int(5 * S))
        top = 2.5                                   # bar spans ratios 1.0 .. 2.5
        tx = x + bw * (CONTRAST_TARGET - 1) / (top - 1)
        if c is not None:
            k = max(0.0, min(1.0, (c - 1) / (top - 1)))
            col = JUDGE_COLORS["PERFECT"] if c >= CONTRAST_TARGET else JUDGE_COLORS["OK"]
            pygame.draw.rect(surf, col, (x, by, bw * k, bh), border_radius=int(5 * S))
        pygame.draw.line(surf, TEXT, (tx, by - 3 * S), (tx, by + bh + 3 * S), max(1, int(2 * S)))
        label = f"contrast {c:.2f}x" if c is not None else "contrast -"
        surf.blit(f.text(label, f.small, DIM), (x + bw + 12 * S, by - 4 * S))

    def metronome(self, surf, t):
        """Four beat squares, each split into the current subdivision, lit in time."""
        g, f, S = self.game, self.f, self.s
        sub = g.chart.subdivision_at(t)
        labels = COUNT_LABELS.get(sub, [str(i + 1) for i in range(sub)])
        size, gap = 74 * S, 10 * S
        total = 4 * size + 3 * gap
        x0, y0 = self.w / 2 - total / 2, 10 * S
        panel = pygame.Surface((int(total + 40 * S), int(size + 44 * S)))
        panel.fill(BG)
        panel.set_alpha(215)
        surf.blit(panel, (int(x0 - 20 * S), 0))
        pos = g.chart.beat_pos(t)                # in beats, negative during the count-in
        beat_i = int(math.floor(pos)) % 4
        frac = pos - math.floor(pos)
        sub_i = min(sub - 1, int(frac * sub))
        prog = frac * sub - sub_i                # 0..1 inside the current cell
        for b in range(4):
            x = x0 + b * (size + gap)
            rect = pygame.Rect(int(x), int(y0), int(size), int(size))
            pygame.draw.rect(surf, LANE_BG, rect, border_radius=int(8 * S))
            cw = size / sub
            for k in range(sub):
                cell = pygame.Rect(int(x + k * cw), int(y0), int(cw) + 1, int(size))
                if b == beat_i and k == sub_i:
                    base = (255, 255, 255) if k == 0 else ACCENT
                    fill = lerp(base, LANE_BG, 0.25 + 0.6 * prog)
                    pygame.draw.rect(surf, fill, cell.inflate(-2, -2), border_radius=int(6 * S))
                label = str(b + 1) if k == 0 else labels[k]
                color = (20, 20, 24) if (b == beat_i and k == sub_i and prog < 0.5) else (TEXT if k == 0 else DIM)
                f.center(surf, label, f.small if sub > 2 else f.mid, color, y0 + size / 2, x + (k + 0.5) * cw)
            pygame.draw.rect(surf, ACCENT if b == beat_i else (50, 50, 60), rect, 3 if b == beat_i else 1, border_radius=int(8 * S))
        name = {1: "quarter notes", 2: "eighth notes", 3: "triplets", 4: "sixteenth notes"}.get(sub, f"{sub} per beat")
        f.center(surf, name, f.small, DIM, y0 + size + 12 * S)
        if g.chart.sticking:
            self.sticking_strip(surf, pos, sub, y0 + size + (44 if g.chart.accents else 34) * S)

    def sticking_strip(self, surf, pos, sub, y):
        """The rudiment's hand pattern, the stroke being played lit up."""
        f, S = self.f, self.s
        pattern = self.game.chart.sticking
        n = len(pattern)
        idx = int(math.floor(pos * sub)) % n if pos >= 0 else -1
        cw = 26 * S
        x0 = self.w / 2 - n * cw / 2
        accents = self.game.chart.accents or set()
        for i, hand in enumerate(pattern):
            cx = x0 + (i + 0.5) * cw
            hot = i == idx
            color = (255, 255, 255) if hot else ((245, 90, 90) if hand == "R" else (80, 200, 230))
            if hot:
                pygame.draw.circle(surf, lerp(LANE_BG, color, 0.5), (int(cx), int(y)), int(12 * S))
            if i in accents:
                f.center(surf, ">", f.small, (255, 255, 255) if hot else TEXT, y - 17 * S, cx)
                f.center(surf, hand, f.large, color, y + 4 * S, cx)
            else:
                f.center(surf, hand, f.small, lerp(color, LANE_BG, 0.35), y + 2 * S, cx)
            if i % (n // max(1, n // 4) if n >= 4 else n) == 0 and i > 0:
                pygame.draw.line(surf, (60, 60, 70), (int(cx - cw / 2), int(y - 12 * S)), (int(cx - cw / 2), int(y + 12 * S)))

    def results(self, surf):
        g = self.game
        st = g.stats()
        S = self.s
        lines = [
            ("RESULTS", self.f.big, TEXT),
            (None, None, None),                                       # the stars go here
            (f"{st['hit']}/{st['notes']} notes  ·  {st['accuracy'] * 100:.1f}%  ·  grade {st['grade']:.0f}", self.f.mid, TEXT),
            (f"max combo {g.max_combo}   score {g.score}", self.f.mid, TEXT),
            (f"timing: mean {st['mean_ms']:+.1f} ms, std {st['std_ms']:.1f} ms", self.f.mid, TEXT),
            (f"{st['early']} early · {st['late']} late · {g.counts['STRAY']} stray", self.f.small, DIM),
        ]
        if g.chart.dynamics:
            c = st.get("contrast")
            ok = c is not None and c >= CONTRAST_TARGET
            lines.append((f"accents {st['accents_ok']}/{st['accents']} · taps {st['taps_ok']}/{st['taps']} · "
                          f"contrast {c:.2f}x" if c is not None else
                          f"accents {st['accents_ok']}/{st['accents']} · taps {st['taps_ok']}/{st['taps']}",
                          self.f.mid, JUDGE_COLORS["PERFECT"] if ok else JUDGE_COLORS["OK"]))
        if g.chart.expression and st.get("art_ok", 0) + st.get("art_wrong", 0):
            r = st["art_rate"]
            lines.append((f"hat articulations {st['art_ok']}/{st['art_ok'] + st['art_wrong']}  ({r * 100:.0f}%)", self.f.mid,
                          JUDGE_COLORS["PERFECT"] if r >= 0.85 else JUDGE_COLORS["OK"]))
        lines.append(("Enter next · R retry · Esc back", self.f.small, DIM))
        bh = 370 + (40 if g.chart.dynamics else 0) + (40 if g.chart.expression else 0)
        box = pygame.Surface((int(600 * S), int(bh * S)))
        box.fill((10, 10, 14))
        box.set_alpha(250)
        cy = self.h * 0.42
        surf.blit(box, (self.w / 2 - 300 * S, cy - bh / 2 * S))
        y = cy - (bh / 2 - 35) * S
        pop = None if self.finished_at is None else time.perf_counter() - self.finished_at
        for s, font, color in lines:
            if s is None:
                w = 5 * 32 * S + 4 * 14 * S
                draw_stars(surf, self.f, st["stars"], self.w / 2 + w / 2, y - 6 * S, S, size="big", pop=pop)
                y += 52 * S
                continue
            y += self.f.center(surf, s, font, color, y) + 12 * S
