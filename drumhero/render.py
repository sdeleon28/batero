"""Drawing: the play field, flashes, HUD, results. Also shared fonts and helpers."""
import math
import time

import pygame

from .game import Game

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


def lerp(a, b, k):
    k = max(0.0, min(1.0, k))
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


class Fonts:
    def __init__(self):
        pygame.font.init()
        name = pygame.font.match_font("menlo,monaco,dejavusansmono,consolas,couriernew") or None
        self.small = pygame.font.Font(name, 16)
        self.mid = pygame.font.Font(name, 24)
        self.large = pygame.font.Font(name, 36)
        self.big = pygame.font.Font(name, 56)
        self.huge = pygame.font.Font(name, 88)
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
    def __init__(self, game: Game, size, fonts: Fonts):
        self.game = game
        self.w, self.h = size
        self.f = fonts
        self.judge_surfs = {k: fonts.big.render(k, True, c) for k, c in JUDGE_COLORS.items()}
        n = len(game.lanes)
        margin = 40
        self.lane_w = min((self.w - 2 * margin) / n, MAX_LANE_W)
        left = (self.w - self.lane_w * n) / 2
        self.lane_x = [left + i * self.lane_w for i in range(n)]
        self.line_y = int(self.h * HIT_LINE_FRAC)
        self.pps = (self.line_y - 60) / LOOKAHEAD_S     # pixels per second at speed 1.0

    def y_for(self, note_t, now):
        return self.line_y - (note_t - now) * self.pps * self.game.speed

    def draw(self, surf, fps=0.0):
        g = self.game
        f = self.f
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
            f.center(surf, lane.label, f.small, lane.color, self.line_y + 34, cx)
            sub = "/".join(map(str, sorted(lane.notes))) if lane.notes else "no pad assigned"
            f.center(surf, sub, f.small, DIM if lane.notes else JUDGE_COLORS["MISS"], self.line_y + 52, cx)

        # flashes: lane glow first so notes draw on top
        for fl in flashes:
            age = (wall - fl.wall_t) / FLASH_S
            if age <= 1:
                glow = lerp(LANE_BG, JUDGE_COLORS[fl.judge], GLOW_STRENGTH * (1 - age))
                pygame.draw.rect(surf, glow, (int(self.lane_x[fl.lane]), self.line_y - GLOW_H, int(self.lane_w) - 2, GLOW_H))

        pygame.draw.line(surf, LINE, (self.lane_x[0], self.line_y), (self.lane_x[-1] + self.lane_w, self.line_y), 3)
        for i, lane in enumerate(g.lanes):
            cx = int(self.lane_x[i] + self.lane_w / 2)
            pygame.draw.circle(surf, lane.color, (cx, self.line_y), 9, 2)

        # notes: from a bit before the cursor so missed ones can fade out
        top = now + LOOKAHEAD_S / speed + 0.2
        for n in g.notes[max(0, lo - 64):]:
            if n.t > top:
                break
            if n.state == "hit":
                continue
            y = self.y_for(n.t, now)
            if y > self.h + NOTE_H:
                continue
            x = int(self.lane_x[n.lane]) + 6
            w = int(self.lane_w) - 14
            color = g.lanes[n.lane].color
            if n.state == "miss":
                age = (now - n.t) / MISS_FADE_S
                if age > 1:
                    continue
                color = lerp(JUDGE_COLORS["MISS"], BG, age)
            rect = (x, int(y - NOTE_H / 2), w, NOTE_H)
            pygame.draw.rect(surf, color, rect, border_radius=6)
            if n.velocity >= 100:
                pygame.draw.rect(surf, (255, 255, 255), rect, 2, border_radius=6)

        # flashes: ring at the line + error number, drawn the frame after the hit arrives
        latest = None
        for fl in flashes:
            age = (wall - fl.wall_t) / FLASH_S
            cx = int(self.lane_x[fl.lane] + self.lane_w / 2)
            color = JUDGE_COLORS[fl.judge]
            if age <= 1:
                k = 1 - age
                pygame.draw.circle(surf, lerp(BG, color, k), (cx, self.line_y), int(14 + 50 * age), max(1, int(6 * k)))
                pygame.draw.circle(surf, color, (cx, self.line_y), 9)
            jt = (wall - fl.wall_t) / JUDGE_TEXT_S
            if jt <= 1:
                latest = fl
                if fl.error_ms is not None:
                    label = f"{'+' if fl.error_ms >= 0 else '-'}{abs(fl.error_ms):.0f}"
                    ts = f.text(label, f.mid, lerp(color, BG, jt))
                    surf.blit(ts, (cx - ts.get_width() / 2, self.line_y - 70 - 40 * jt))

        if latest is not None:
            jt = (wall - latest.wall_t) / JUDGE_TEXT_S
            js = self.judge_surfs[latest.judge]
            js.set_alpha(int(255 * (1 - jt ** 2)))
            surf.blit(js, (self.w / 2 - js.get_width() / 2, self.h * 0.30 - 10 * jt))
            js.set_alpha(255)
            if latest.error_ms is not None:
                e = latest.error_ms
                label = "on time" if abs(e) < 0.5 else f"{abs(e):.0f} ms {'early' if e < 0 else 'late'}"
                es = f.text(label, f.mid, JUDGE_COLORS[latest.judge])
                es.set_alpha(int(255 * (1 - jt)))
                surf.blit(es, (self.w / 2 - es.get_width() / 2, self.h * 0.30 + 60))
                es.set_alpha(255)

        # HUD with a backing so it stays readable over notes
        backing = pygame.Surface((360, 100))
        backing.fill(BG)
        backing.set_alpha(200)
        surf.blit(backing, (0, 0))
        surf.blit(f.text(g.chart.name, f.mid, ACCENT), (12, 8))
        surf.blit(f.text(f"score {score}   combo {combo}", f.mid, TEXT), (12, 38))
        surf.blit(f.text(f"P {counts['PERFECT']}  G {counts['GOOD']}  O {counts['OK']}  M {counts['MISS']}  S {counts['STRAY']}",
                         f.small, DIM), (12, 70))
        right = [f"{fps:5.0f} fps", f"offset {offset:+.0f} ms", f"speed {speed:.2f}x", f"{g.chart.bpm:.0f} bpm",
                 f"guide {'on' if g.guide else 'off'}"]
        for i, s in enumerate(right):
            ts = f.text(s, f.small, DIM)
            surf.blit(ts, (self.w - ts.get_width() - 12, 10 + i * 20))

        if paused:
            f.center(surf, "PAUSED", f.huge, TEXT, self.h * 0.45)
            f.center(surf, "space resume · R restart · Esc menu", f.small, DIM, self.h * 0.45 + 70)
        elif now < 0:
            beats_left = math.ceil(-now / g.beat)
            f.center(surf, str((beats_left - 1) % 4 + 1), f.huge, TEXT, self.h * 0.45)
            f.center(surf, g.chart.desc, f.mid, DIM, self.h * 0.45 + 80)
        if finished:
            self.results(surf)

    def results(self, surf):
        g = self.game
        st = g.stats()
        box = pygame.Surface((560, 320))
        box.fill((10, 10, 14))
        box.set_alpha(250)
        cy = self.h * 0.42
        surf.blit(box, (self.w / 2 - 280, cy - 160))
        y = cy - 125
        for s, font, color in [
            ("RESULTS", self.f.big, TEXT),
            (f"{st['hit']}/{st['notes']} notes  ·  {st['accuracy'] * 100:.1f}%", self.f.mid, TEXT),
            (f"max combo {g.max_combo}   score {g.score}", self.f.mid, TEXT),
            (f"timing: mean {st['mean_ms']:+.1f} ms, std {st['std_ms']:.1f} ms", self.f.mid, TEXT),
            (f"{st['early']} early · {st['late']} late · {g.counts['STRAY']} stray", self.f.small, DIM),
            ("Enter next · R retry · Esc back", self.f.small, DIM),
        ]:
            y += self.f.center(surf, s, font, color, y) + 12
