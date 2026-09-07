#!/usr/bin/env python3
"""drumhero: a Guitar Hero style drum trainer driven by MIDI.

Feed it any MIDI file as the chart. Notes fall down their lanes towards the hit
line; play along on a MIDI drum kit (or the keyboard) and get instant visual
feedback the moment each hit lands, with the timing error in milliseconds.

Usage:
    python drumhero.py song.mid --port TD-17
    python drumhero.py --demo --port TD-17          # built-in rock beat
    python drumhero.py song.mid --out IAC           # also play the chart to a synth
    python drumhero.py song.mid --alias 22=42,26=46 # fold extra input notes into lanes

Controls:
    Esc / Q      quit             Space   pause / resume
    R            restart          [ / ]   scroll speed
    , / .        input latency offset -/+ 5 ms (see README on calibration)
    1..9, 0      hit lanes 1..10 from the keyboard
"""
import argparse
import csv
import math
import statistics
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import mido
import pygame

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------
PERFECT_MS = 25          # |error| <= this -> PERFECT
GOOD_MS = 60             # |error| <= this -> GOOD
OK_MS = 100              # |error| <= this -> OK; beyond -> the hit is stray / the note is missed
SCORE = {"PERFECT": 100, "GOOD": 50, "OK": 20}

LEAD_IN_S = 3.0          # seconds before the first note reaches the line
LOOKAHEAD_S = 2.0        # seconds of chart visible above the line at speed 1.0
TAIL_S = 2.5             # seconds after the last note before the summary
FLASH_S = 0.18           # duration of the hit flash
GLOW_H = 220             # height of the lane glow above the hit line, in px
GLOW_STRENGTH = 0.16     # how far the lane background moves toward the judgement color
JUDGE_TEXT_S = 0.6       # how long the judgement word stays up
MISS_FADE_S = 0.6        # missed notes fade out below the line for this long

HIT_LINE_FRAC = 0.82     # vertical position of the hit line (fraction of window height)
NOTE_H = 16
TARGET_FPS = 240         # uncapped-ish; feedback shows on the very next frame

BG = (14, 14, 18)
LANE_BG = (22, 22, 28)
LANE_EDGE = (40, 40, 50)
LINE = (235, 235, 235)
TEXT = (220, 220, 220)
DIM = (120, 120, 130)
JUDGE_COLORS = {
    "PERFECT": (80, 230, 140),
    "GOOD": (240, 210, 70),
    "OK": (240, 140, 60),
    "MISS": (230, 70, 70),
    "STRAY": (170, 90, 200),
}
LANE_PALETTE = [
    (245, 90, 90), (250, 170, 60), (245, 230, 80), (110, 220, 110), (80, 200, 230),
    (100, 130, 250), (190, 110, 240), (240, 120, 190), (140, 200, 160), (200, 200, 200),
]

GM_DRUM_NAMES = {
    35: "Kick 2", 36: "Kick", 37: "Side Stick", 38: "Snare", 39: "Clap", 40: "Snare 2",
    41: "Floor Tom 2", 42: "HH Closed", 43: "Floor Tom", 44: "HH Pedal", 45: "Low Tom",
    46: "HH Open", 47: "Mid Tom", 48: "High Tom 2", 49: "Crash", 50: "High Tom",
    51: "Ride", 52: "China", 53: "Ride Bell", 54: "Tambourine", 55: "Splash",
    56: "Cowbell", 57: "Crash 2", 58: "Vibraslap", 59: "Ride 2",
}


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------
@dataclass
class ChartNote:
    t: float               # seconds from chart start
    note: int
    velocity: int
    lane: int = -1
    state: str = "pending"  # pending / hit / miss
    judge: str = None       # PERFECT / GOOD / OK / MISS
    error_ms: float = None  # hit time - note time (negative = early)


@dataclass
class Lane:
    index: int
    label: str
    color: tuple
    notes: set = field(default_factory=set)


def load_midi_chart(path: str, channel: int = None):
    """Return (chart notes, seconds) from a MIDI file. Honors tempo changes."""
    mid = mido.MidiFile(path)
    notes, t = [], 0.0
    for msg in mid:               # iterating a MidiFile yields real-time deltas
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            if channel is None or msg.channel == channel:
                notes.append(ChartNote(t, msg.note, msg.velocity))
    if not notes:
        sys.exit(f"No note_on events found in {path}" + (f" on channel {channel + 1}" if channel is not None else ""))
    first = notes[0].t
    for n in notes:               # chart time 0 = first note
        n.t -= first
    return notes


def demo_chart(bpm: float = 110, bars: int = 16):
    """A plain rock beat: kick 1 & 3, snare 2 & 4, eighth-note closed hats, crash on bar 1."""
    beat = 60 / bpm
    notes = []
    for bar in range(bars):
        t0 = bar * 4 * beat
        for eighth in range(8):
            notes.append(ChartNote(t0 + eighth * beat / 2, 42, 80))
        notes.append(ChartNote(t0, 36, 110))
        notes.append(ChartNote(t0 + 2 * beat, 36, 110))
        if bar % 4 == 3:
            notes.append(ChartNote(t0 + 2.5 * beat, 36, 100))
        notes.append(ChartNote(t0 + beat, 38, 115))
        notes.append(ChartNote(t0 + 3 * beat, 38, 115))
        if bar % 4 == 0:
            notes.append(ChartNote(t0, 49, 120))
    notes.sort(key=lambda n: n.t)
    return notes


def build_lanes(notes, aliases: dict):
    """One lane per distinct chart note number, ascending. aliases: input note -> chart note."""
    numbers = sorted({n.note for n in notes})
    lanes = []
    for i, num in enumerate(numbers):
        label = GM_DRUM_NAMES.get(num, f"note {num}")
        lanes.append(Lane(i, f"{label}\n{num}", LANE_PALETTE[i % len(LANE_PALETTE)], {num}))
    by_note = {num: i for i, num in enumerate(numbers)}
    for src, dst in aliases.items():
        if dst in by_note:
            lanes[by_note[dst]].notes.add(src)
            by_note[src] = by_note[dst]
        else:
            print(f"alias {src}={dst}: chart has no note {dst}, ignored")
    for n in notes:
        n.lane = by_note[n.note]
    return lanes, by_note


def parse_aliases(text: str):
    out = {}
    if text:
        for pair in text.split(","):
            a, b = pair.split("=")
            out[int(a)] = int(b)
    return out


# ---------------------------------------------------------------------------
# Game state (no drawing here; safe to drive from tests)
# ---------------------------------------------------------------------------
@dataclass
class Flash:
    wall_t: float
    lane: int
    judge: str
    error_ms: float
    velocity: int


class Game:
    def __init__(self, notes, lanes, by_note, offset_ms=0.0, speed=1.0):
        self.notes = notes
        self.lanes = lanes
        self.by_note = by_note
        self.offset_ms = offset_ms
        self.speed = speed
        self.lock = threading.Lock()
        self.reset()

    # --- clock -------------------------------------------------------------
    def reset(self):
        with self.lock:
            for n in self.notes:
                n.state, n.judge, n.error_ms = "pending", None, None
            self.wall_start = time.perf_counter()
            self.paused_at = None
            self.paused_total = 0.0
            self.flashes = deque(maxlen=64)
            self.hits = []                      # every judged input, for stats / CSV
            self.combo = self.max_combo = self.score = 0
            self.counts = {k: 0 for k in ("PERFECT", "GOOD", "OK", "MISS", "STRAY")}
            self.cursor = 0                     # first note that may still be pending
            self.finished = False

    def song_time(self, wall_t=None):
        if wall_t is None:
            wall_t = time.perf_counter()
        if self.paused_at is not None:
            wall_t = self.paused_at
        return wall_t - self.wall_start - self.paused_total - LEAD_IN_S

    @property
    def paused(self):
        return self.paused_at is not None

    def toggle_pause(self):
        with self.lock:
            if self.paused_at is None:
                self.paused_at = time.perf_counter()
            else:
                self.paused_total += time.perf_counter() - self.paused_at
                self.paused_at = None

    # --- input -------------------------------------------------------------
    def hit(self, note: int, velocity: int = 100, wall_t: float = None):
        """Judge an incoming hit. Called from the MIDI thread; must be quick."""
        if wall_t is None:
            wall_t = time.perf_counter()
        with self.lock:
            if self.paused or self.finished:
                return None
            lane = self.by_note.get(note)
            if lane is None:
                return None
            t = self.song_time(wall_t) + self.offset_ms / 1000
            best, best_err = None, None
            for n in self.notes[self.cursor:]:
                if n.t - t > OK_MS / 1000:
                    break
                if n.lane != lane or n.state != "pending":
                    continue
                err = t - n.t
                if abs(err) <= OK_MS / 1000 and (best is None or abs(err) < abs(best_err)):
                    best, best_err = n, err
            if best is None:
                judge, err_ms = "STRAY", None
            else:
                err_ms = best_err * 1000
                a = abs(err_ms)
                judge = "PERFECT" if a <= PERFECT_MS else "GOOD" if a <= GOOD_MS else "OK"
                best.state, best.judge, best.error_ms = "hit", judge, err_ms
            self._register(judge, lane, err_ms, velocity, wall_t, best)
            return judge

    def _register(self, judge, lane, err_ms, velocity, wall_t, note):
        self.counts[judge] += 1
        if judge in SCORE:
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
            self.score += SCORE[judge] * (1 + self.combo // 10)
        elif judge in ("MISS", "STRAY"):
            self.combo = 0
        self.flashes.append(Flash(wall_t, lane, judge, err_ms, velocity))
        self.hits.append((note.t if note else None, lane, judge, err_ms))

    # --- per-frame housekeeping ----------------------------------------------
    def update(self):
        """Mark notes that scrolled past the window as missed; advance the cursor."""
        with self.lock:
            if self.paused:
                return
            t = self.song_time()
            for i in range(self.cursor, len(self.notes)):
                n = self.notes[i]
                if n.t > t - OK_MS / 1000:
                    break
                if n.state == "pending":
                    n.state, n.judge = "miss", "MISS"
                    self._register("MISS", n.lane, None, 0, time.perf_counter(), n)
            while self.cursor < len(self.notes) and self.notes[self.cursor].state != "pending":
                self.cursor += 1
            if not self.finished and t > self.notes[-1].t + TAIL_S:
                self.finished = True

    # --- stats ---------------------------------------------------------------
    def stats(self):
        errs = [e for _, _, j, e in self.hits if e is not None]
        total = len(self.notes)
        hit = sum(self.counts[k] for k in SCORE)
        return {
            "notes": total,
            "hit": hit,
            "accuracy": hit / total if total else 0.0,
            "mean_ms": statistics.fmean(errs) if errs else 0.0,
            "std_ms": statistics.pstdev(errs) if len(errs) > 1 else 0.0,
            "early": sum(1 for e in errs if e < 0),
            "late": sum(1 for e in errs if e > 0),
        }

    def write_csv(self, path):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["chart_time_s", "lane", "label", "judge", "error_ms"])
            for t, lane, judge, err in self.hits:
                w.writerow([f"{t:.4f}" if t is not None else "", lane,
                            self.lanes[lane].label.replace("\n", " "), judge,
                            f"{err:.1f}" if err is not None else ""])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
class Renderer:
    def __init__(self, game: Game, size):
        self.game = game
        self.w, self.h = size
        pygame.font.init()
        name = pygame.font.match_font("menlo,monaco,dejavusansmono,consolas,couriernew") or None
        self.f_small = pygame.font.Font(name, 16)
        self.f_mid = pygame.font.Font(name, 24)
        self.f_big = pygame.font.Font(name, 56)
        self.f_huge = pygame.font.Font(name, 72)
        self.judge_surfs = {k: self.f_big.render(k, True, c) for k, c in JUDGE_COLORS.items()}
        self.text_cache = {}
        self.layout()

    def layout(self):
        n = len(self.game.lanes)
        margin = 40
        self.lane_w = (self.w - 2 * margin) / n
        self.lane_x = [margin + i * self.lane_w for i in range(n)]
        self.line_y = int(self.h * HIT_LINE_FRAC)
        self.pps = (self.line_y - 60) / LOOKAHEAD_S     # pixels per second at speed 1.0

    def text(self, s, font, color):
        key = (s, id(font), color)
        surf = self.text_cache.get(key)
        if surf is None:
            surf = font.render(s, True, color)
            if len(self.text_cache) > 512:
                self.text_cache.clear()
            self.text_cache[key] = surf
        return surf

    def y_for(self, note_t, now):
        return self.line_y - (note_t - now) * self.pps * self.game.speed

    def draw(self, surf, fps=0.0):
        g = self.game
        wall = time.perf_counter()
        with g.lock:
            now = g.song_time(wall)
            flashes = list(g.flashes)
            lo = g.cursor
            paused, finished = g.paused, g.finished
            score, combo, counts, offset, speed = g.score, g.combo, dict(g.counts), g.offset_ms, g.speed

        surf.fill(BG)
        # lanes
        for i, lane in enumerate(g.lanes):
            x = int(self.lane_x[i])
            pygame.draw.rect(surf, LANE_BG, (x, 0, int(self.lane_w) - 2, self.h))
            pygame.draw.line(surf, LANE_EDGE, (x, 0), (x, self.h))
            for k, part in enumerate(lane.label.split("\n")):
                ts = self.text(part, self.f_small, lane.color if k == 0 else DIM)
                surf.blit(ts, (x + self.lane_w / 2 - ts.get_width() / 2, self.line_y + 26 + k * 18))

        # hit line + per-lane target markers
        pygame.draw.line(surf, LINE, (self.lane_x[0], self.line_y), (self.lane_x[-1] + self.lane_w, self.line_y), 3)
        for i, lane in enumerate(g.lanes):
            cx = int(self.lane_x[i] + self.lane_w / 2)
            pygame.draw.circle(surf, lane.color, (cx, self.line_y), 9, 2)

        # notes: draw from a bit before the cursor so missed ones can fade out
        top = now + LOOKAHEAD_S / speed + 0.2
        start = max(0, lo - 64)
        for n in g.notes[start:]:
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
            pygame.draw.rect(surf, color, (x, int(y - NOTE_H / 2), w, NOTE_H), border_radius=6)
            if n.velocity >= 100:
                pygame.draw.rect(surf, (255, 255, 255), (x, int(y - NOTE_H / 2), w, NOTE_H), 2, border_radius=6)

        # flashes: expanding ring + lane glow, drawn the frame after the hit arrives
        latest_judge = None
        for fl in flashes:
            age = (wall - fl.wall_t) / FLASH_S
            cx = int(self.lane_x[fl.lane] + self.lane_w / 2)
            color = JUDGE_COLORS[fl.judge]
            if age <= 1:
                k = 1 - age
                glow = lerp(LANE_BG, color, GLOW_STRENGTH * k)
                pygame.draw.rect(surf, glow, (int(self.lane_x[fl.lane]), self.line_y - GLOW_H, int(self.lane_w) - 2, GLOW_H))
                r = int(14 + 50 * age)
                pygame.draw.circle(surf, lerp(BG, color, k), (cx, self.line_y), r, max(1, int(6 * k)))
                pygame.draw.circle(surf, color, (cx, self.line_y), 9)
            jt = (wall - fl.wall_t) / JUDGE_TEXT_S
            if jt <= 1:
                latest_judge = fl
                if fl.error_ms is not None:
                    sign = "+" if fl.error_ms >= 0 else "-"
                    label = f"{sign}{abs(fl.error_ms):.0f}"
                    ts = self.text(label, self.f_mid, lerp(color, BG, jt))
                    surf.blit(ts, (cx - ts.get_width() / 2, self.line_y - 70 - 40 * jt))

        if latest_judge is not None:
            jt = (wall - latest_judge.wall_t) / JUDGE_TEXT_S
            js = self.judge_surfs[latest_judge.judge]
            js.set_alpha(int(255 * (1 - jt ** 2)))
            surf.blit(js, (self.w / 2 - js.get_width() / 2, self.h * 0.30 - 10 * jt))
            js.set_alpha(255)
            if latest_judge.error_ms is not None:
                e = latest_judge.error_ms
                label = "on time" if abs(e) < 0.5 else f"{abs(e):.0f} ms {'early' if e < 0 else 'late'}"
                es = self.text(label, self.f_mid, JUDGE_COLORS[latest_judge.judge])
                es.set_alpha(int(255 * (1 - jt)))
                surf.blit(es, (self.w / 2 - es.get_width() / 2, self.h * 0.30 + 60))
                es.set_alpha(255)

        # HUD (with a backing so it stays readable over notes)
        backing = pygame.Surface((330, 96)); backing.fill(BG); backing.set_alpha(200)
        surf.blit(backing, (0, 0))
        hud = [
            (f"score {score}", TEXT), (f"combo {combo}", TEXT),
            (f"P {counts['PERFECT']}  G {counts['GOOD']}  O {counts['OK']}  M {counts['MISS']}  S {counts['STRAY']}", DIM),
        ]
        for i, (s, c) in enumerate(hud):
            surf.blit(self.text(s, self.f_mid if i < 2 else self.f_small, c), (12, 10 + i * 28))
        right = [
            (f"{fps:5.0f} fps", DIM), (f"offset {offset:+.0f} ms", DIM), (f"speed {speed:.2f}x", DIM),
            (f"t {now:6.2f} s", DIM),
        ]
        for i, (s, c) in enumerate(right):
            ts = self.text(s, self.f_small, c)
            surf.blit(ts, (self.w - ts.get_width() - 12, 10 + i * 20))

        if paused:
            self.center(surf, "PAUSED", self.f_huge, TEXT, 0.45)
            self.center(surf, "space to resume · R restart · Esc quit", self.f_small, DIM, 0.45, 60)
        if now < 0 and not paused:
            self.center(surf, f"{math.ceil(-now)}", self.f_huge, TEXT, 0.45)
        if finished:
            self.summary(surf)

    def center(self, surf, s, font, color, frac, dy=0):
        ts = self.text(s, font, color)
        surf.blit(ts, (self.w / 2 - ts.get_width() / 2, self.h * frac - ts.get_height() / 2 + dy))

    def summary(self, surf):
        st = self.game.stats()
        box = pygame.Surface((520, 300))
        box.fill((10, 10, 14))
        box.set_alpha(235)
        surf.blit(box, (self.w / 2 - 260, self.h * 0.45 - 150))
        lines = [
            ("RESULTS", self.f_big, TEXT),
            (f"{st['hit']}/{st['notes']} notes  ·  {st['accuracy'] * 100:.1f}%", self.f_mid, TEXT),
            (f"max combo {self.game.max_combo}   score {self.game.score}", self.f_mid, TEXT),
            (f"timing: mean {st['mean_ms']:+.1f} ms, std {st['std_ms']:.1f} ms", self.f_mid, TEXT),
            (f"{st['early']} early · {st['late']} late · {self.game.counts['STRAY']} stray", self.f_small, DIM),
            ("R to play again · Esc to quit", self.f_small, DIM),
        ]
        y = self.h * 0.45 - 120
        for s, font, color in lines:
            ts = self.text(s, font, color)
            surf.blit(ts, (self.w / 2 - ts.get_width() / 2, y))
            y += ts.get_height() + 8


def lerp(a, b, k):
    k = max(0.0, min(1.0, k))
    return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))


# ---------------------------------------------------------------------------
# MIDI in / out
# ---------------------------------------------------------------------------
def pick_port(substring, names, kind):
    matches = [n for n in names if substring.lower() in n.lower()]
    if not matches:
        print(f"No MIDI {kind} matching '{substring}'. Available:")
        for n in names:
            print(f"  - {n}")
        sys.exit(1)
    return matches[0]


class ChartPlayer(threading.Thread):
    """Plays the chart's notes to a MIDI output as they cross the line."""

    def __init__(self, game: Game, port_name: str, channel=9):
        super().__init__(daemon=True)
        self.game, self.channel = game, channel
        self.port = mido.open_output(port_name)
        self.stop = threading.Event()

    def run(self):
        i = 0
        last_reset = self.game.wall_start
        while not self.stop.is_set():
            if self.game.wall_start != last_reset:      # restarted
                i, last_reset = 0, self.game.wall_start
            if self.game.paused or i >= len(self.game.notes):
                time.sleep(0.01)
                continue
            n = self.game.notes[i]
            dt = n.t - self.game.song_time()
            if dt > 0.002:
                time.sleep(min(dt - 0.001, 0.01))
                continue
            self.port.send(mido.Message("note_on", channel=self.channel, note=n.note, velocity=n.velocity))
            threading.Timer(0.03, self.port.send,
                            args=[mido.Message("note_off", channel=self.channel, note=n.note)]).start()
            i += 1


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
KEY_LANES = {pygame.K_1: 0, pygame.K_2: 1, pygame.K_3: 2, pygame.K_4: 3, pygame.K_5: 4,
             pygame.K_6: 5, pygame.K_7: 6, pygame.K_8: 7, pygame.K_9: 8, pygame.K_0: 9}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("midi", nargs="?", help="MIDI file to use as the chart")
    ap.add_argument("--demo", action="store_true", help="use the built-in rock beat instead of a file")
    ap.add_argument("--channel", type=int, help="only use chart notes on this MIDI channel (1-16)")
    ap.add_argument("--port", help="MIDI input port (substring). Without it, keyboard only")
    ap.add_argument("--out", help="MIDI output port (substring) to play the chart through")
    ap.add_argument("--alias", help="fold input notes into chart notes, e.g. 22=42,26=46")
    ap.add_argument("--offset", type=float, default=0.0, help="input latency compensation in ms (positive = treat hits as earlier)")
    ap.add_argument("--speed", type=float, default=1.0, help="scroll speed multiplier")
    ap.add_argument("--size", default="1280x720", help="window size WxH")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--log", help="write every judged hit to this CSV on exit")
    args = ap.parse_args(argv)

    if args.demo:
        notes = demo_chart()
    elif args.midi:
        notes = load_midi_chart(args.midi, None if args.channel is None else args.channel - 1)
    else:
        ap.error("give a MIDI file or --demo")
    lanes, by_note = build_lanes(notes, parse_aliases(args.alias))
    game = Game(notes, lanes, by_note, offset_ms=args.offset, speed=args.speed)
    print(f"{len(notes)} notes, {len(lanes)} lanes: " + ", ".join(l.label.replace(chr(10), " ") for l in lanes))

    midi_in = None
    if args.port:
        name = pick_port(args.port, mido.get_input_names(), "input")

        def on_msg(msg):
            if msg.type == "note_on" and msg.velocity > 0:
                game.hit(msg.note, msg.velocity)

        midi_in = mido.open_input(name, callback=on_msg)
        print(f"input: {name}")
    else:
        print("no --port given: keyboard only (keys 1-9, 0). Inputs available: " + ", ".join(mido.get_input_names()))

    player = None
    if args.out:
        player = ChartPlayer(game, pick_port(args.out, mido.get_output_names(), "output"))
        player.start()

    pygame.init()
    w, h = (int(v) for v in args.size.lower().split("x"))
    flags = pygame.FULLSCREEN if args.fullscreen else 0
    screen = pygame.display.set_mode((w, h), flags)
    pygame.display.set_caption("drumhero")
    renderer = Renderer(game, screen.get_size())
    clock = pygame.time.Clock()
    game.reset()                                   # start the clock after setup latency

    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                elif ev.key == pygame.K_SPACE:
                    game.toggle_pause()
                elif ev.key == pygame.K_r:
                    game.reset()
                elif ev.key == pygame.K_LEFTBRACKET:
                    game.speed = max(0.25, game.speed - 0.25)
                elif ev.key == pygame.K_RIGHTBRACKET:
                    game.speed = min(4.0, game.speed + 0.25)
                elif ev.key == pygame.K_COMMA:
                    game.offset_ms -= 5
                elif ev.key == pygame.K_PERIOD:
                    game.offset_ms += 5
                elif ev.key in KEY_LANES and KEY_LANES[ev.key] < len(lanes):
                    game.hit(next(iter(lanes[KEY_LANES[ev.key]].notes)), 100)
        game.update()
        renderer.draw(screen, clock.get_fps())
        pygame.display.flip()
        clock.tick(TARGET_FPS)

    if player:
        player.stop.set()
    if midi_in:
        midi_in.close()
    pygame.quit()
    st = game.stats()
    print(f"{st['hit']}/{st['notes']} hit ({st['accuracy'] * 100:.1f}%), mean {st['mean_ms']:+.1f} ms, std {st['std_ms']:.1f} ms, "
          f"max combo {game.max_combo}, score {game.score}")
    if args.log:
        game.write_csv(args.log)
        print(f"wrote {args.log}")


if __name__ == "__main__":
    main()
