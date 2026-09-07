"""Screens (menu, level select, kit wizard, play) and the main loop."""
import argparse
import sys
import threading
import time

import mido
import pygame

from . import chart as C
from .chart import LEVELS, build_lanes, load_midi_chart
from .game import Game
from .kit import default_kit, describe, load_kit, save_kit
from .render import ACCENT, BG, DIM, JUDGE_COLORS, LANE_BG, TEXT, Fonts, Renderer, lerp
from .sounds import SoundBank

TARGET_FPS = 240
CAPTURE_S = 1.5           # wizard: keep collecting note numbers this long after the first hit
KEY_LANES = {pygame.K_1: 0, pygame.K_2: 1, pygame.K_3: 2, pygame.K_4: 3, pygame.K_5: 4,
             pygame.K_6: 5, pygame.K_7: 6, pygame.K_8: 7, pygame.K_9: 8, pygame.K_0: 9}
MODULE_HINTS = ("td-", "td1", "td2", "td5", "alesis", "nitro", "strike", "dtx", "roland", "drum")
WIZARD_PROMPTS = {
    "kick": "Hit the KICK a few times",
    "snare": "Hit the SNARE a few times",
    "hihat": "Hit the HI-HAT a few times, edge and top",
    "crash": "Hit the CRASH a few times",
}


class App:
    def __init__(self, args):
        self.args = args
        self.kit = load_kit(args.kit) if args.kit else load_kit()
        self.first_run = self.kit is None
        if self.kit is None:
            self.kit = default_kit()
        self.guide = not args.no_guide
        self.offset_ms = args.offset
        self.speed = args.speed
        self.results = {}          # chart name -> stats of the best run this session
        self.levels = list(LEVELS)
        if args.midi:
            self.levels.append(load_midi_chart(args.midi, None if args.channel is None else args.channel - 1))
        self.midi_name = None
        self.midi_in = None
        self.screen_obj = None

        pygame.init()
        self.sounds = SoundBank(enabled=not args.no_sound)
        w, h = (int(v) for v in args.size.lower().split("x"))
        self.surface = pygame.display.set_mode((w, h), pygame.FULLSCREEN if args.fullscreen else 0)
        pygame.display.set_caption("drumhero")
        self.size = self.surface.get_size()
        self.fonts = Fonts()
        self.open_midi(args.port)

    # --- MIDI ------------------------------------------------------------------
    def open_midi(self, wanted):
        names = mido.get_input_names()
        name = None
        if wanted:
            matches = [n for n in names if wanted.lower() in n.lower()]
            if not matches:
                print(f"No MIDI input matching '{wanted}'. Available: {', '.join(names) or 'none'}")
            else:
                name = matches[0]
        else:
            for n in names:
                if any(h in n.lower() for h in MODULE_HINTS):
                    name = n
                    break
        if name:
            self.midi_in = mido.open_input(name, callback=self.on_midi)
            self.midi_name = name
            print(f"MIDI input: {name}")
        else:
            print("No MIDI input: keyboard only (keys 1-9, 0). Inputs: " + (", ".join(names) or "none"))

    def on_midi(self, msg):
        if msg.type == "note_on" and msg.velocity > 0:
            scr = self.screen_obj
            if scr is not None:
                scr.on_note(msg.note, msg.velocity)

    # --- screens ---------------------------------------------------------------
    def go(self, screen):
        self.screen_obj = screen

    def run(self):
        self.go(SetupScreen(self, first_run=True) if self.first_run and self.midi_in else MenuScreen(self))
        clock = pygame.time.Clock()
        running = True
        while running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type == pygame.KEYDOWN:
                    if self.screen_obj.on_key(ev.key) is False:
                        running = False
            self.screen_obj.update()
            self.screen_obj.draw(self.surface, clock.get_fps())
            pygame.display.flip()
            clock.tick(TARGET_FPS)
        if self.midi_in:
            self.midi_in.close()
        pygame.quit()


class Screen:
    def __init__(self, app: App):
        self.app = app
        self.f = app.fonts
        self.w, self.h = app.size

    def on_note(self, note, velocity):
        pass

    def on_key(self, key):
        """Return False to quit the app."""
        return True

    def update(self):
        pass

    def draw(self, surf, fps):
        pass

    def footer(self, surf, s):
        self.f.center(surf, s, self.f.small, DIM, self.h - 28)


# ---------------------------------------------------------------------------
class MenuScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.sel = 0

    def items(self):
        return [
            ("Play", "pick a level"),
            ("Set up kit", describe(self.app.kit)),
            (f"Guide sounds: {'on' if self.app.guide else 'off'}", "hear the chart as it crosses the line"),
            ("Quit", ""),
        ]

    def on_key(self, key):
        n = len(self.items())
        if key in (pygame.K_UP, pygame.K_k):
            self.sel = (self.sel - 1) % n
        elif key in (pygame.K_DOWN, pygame.K_j):
            self.sel = (self.sel + 1) % n
        elif key in (pygame.K_RETURN, pygame.K_SPACE):
            return self.activate()
        elif key in (pygame.K_ESCAPE, pygame.K_q):
            return False
        return True

    def activate(self):
        if self.sel == 0:
            self.app.go(LevelScreen(self.app))
        elif self.sel == 1:
            self.app.go(SetupScreen(self.app))
        elif self.sel == 2:
            self.app.guide = not self.app.guide
        else:
            return False
        return True

    def draw(self, surf, fps):
        surf.fill(BG)
        self.f.center(surf, "drumhero", self.f.huge, TEXT, self.h * 0.22)
        y = self.h * 0.42
        for i, (label, sub) in enumerate(self.items()):
            color = ACCENT if i == self.sel else TEXT
            self.f.center(surf, ("> " if i == self.sel else "") + label, self.f.large, color, y)
            if sub:
                self.f.center(surf, sub, self.f.small, DIM, y + 30)
            y += 78
        midi = self.app.midi_name or "none, keyboard keys 1-9 hit the lanes"
        self.footer(surf, f"MIDI input: {midi}   ·   arrows + Enter")


# ---------------------------------------------------------------------------
class LevelScreen(Screen):
    def __init__(self, app, sel=0):
        super().__init__(app)
        self.sel = sel

    def on_key(self, key):
        n = len(self.app.levels)
        if key in (pygame.K_UP, pygame.K_k):
            self.sel = (self.sel - 1) % n
        elif key in (pygame.K_DOWN, pygame.K_j):
            self.sel = (self.sel + 1) % n
        elif key in (pygame.K_RETURN, pygame.K_SPACE):
            self.app.go(PlayScreen(self.app, self.sel))
        elif key == pygame.K_ESCAPE:
            self.app.go(MenuScreen(self.app))
        return True

    def draw(self, surf, fps):
        surf.fill(BG)
        self.f.center(surf, "Pick a level", self.f.big, TEXT, 70)
        self.f.center(surf, "The first ones are warm-ups: one drum at a time.", self.f.small, DIM, 115)
        y = 170
        for i, ch in enumerate(self.app.levels):
            color = ACCENT if i == self.sel else TEXT
            x = self.w * 0.12
            name = ch.name if len(ch.name) <= 22 else ch.name[:21] + "…"
            surf.blit(self.f.text(("> " if i == self.sel else "  ") + name, self.f.mid, color), (x, y))
            surf.blit(self.f.text(f"{ch.bpm:.0f} bpm · {len(ch.notes):3d} notes · {ch.desc}", self.f.small, DIM), (x + 370, y + 4))
            best = self.app.results.get(ch.name)
            if best:
                s = f"best {best['accuracy'] * 100:.0f}%  mean {best['mean_ms']:+.0f} ms"
                ts = self.f.text(s, self.f.small, JUDGE_COLORS["PERFECT"] if best["accuracy"] >= 0.9 else JUDGE_COLORS["GOOD"])
                surf.blit(ts, (self.w * 0.88 - ts.get_width(), y + 4))
            y += 44
        self.footer(surf, "Enter play · Esc back")


# ---------------------------------------------------------------------------
class SetupScreen(Screen):
    """Onboarding wizard: hit each drum in turn; every distinct note number heard is assigned."""

    def __init__(self, app, first_run=False):
        super().__init__(app)
        self.first_run = first_run
        self.lock = threading.Lock()
        self.step = 0
        self.captured = {k: [] for k in C.INSTRUMENTS}
        self.capture_start = None
        self.flash = None              # (wall_t, note, velocity)
        self.done_at = None

    @property
    def key(self):
        return C.INSTRUMENTS[self.step] if self.step < len(C.INSTRUMENTS) else None

    def on_note(self, note, velocity):
        with self.lock:
            key = self.key
            if key is None:
                return
            now = time.perf_counter()
            if self.capture_start is None:
                self.capture_start = now
            if note not in self.captured[key]:
                self.captured[key].append(note)
            self.flash = (now, note, velocity)
        self.app.sounds.play(key, velocity)

    def advance(self):
        self.capture_start = None
        self.step += 1
        if self.step >= len(C.INSTRUMENTS):
            self.finish()

    def finish(self):
        kit = {}
        taken = set()
        for k in reversed(C.INSTRUMENTS):        # a number heard for two drums goes to the later one
            kit[k] = [n for n in self.captured[k] if n not in taken]
            taken.update(kit[k])
        self.app.kit = kit
        save_kit(kit) if not self.app.args.kit else save_kit(kit, self.app.args.kit)
        self.done_at = time.perf_counter()

    def on_key(self, key):
        if key == pygame.K_ESCAPE:
            self.app.go(MenuScreen(self.app))
        elif key in (pygame.K_RETURN, pygame.K_s):
            with self.lock:
                if self.done_at is None:
                    self.advance()
        elif key == pygame.K_BACKSPACE:
            with self.lock:
                if self.done_at is None and self.step > 0:
                    self.step -= 1
                    self.captured[self.key] = []
                    self.capture_start = None
        elif key in KEY_LANES and self.key is not None:
            # keyboard fallback: pretend the pad sent a GM number
            self.on_note(C.DEFAULT_KIT[self.key][0], 100)
        return True

    def update(self):
        with self.lock:
            if self.done_at is None and self.capture_start is not None and \
                    time.perf_counter() - self.capture_start >= CAPTURE_S:
                self.advance()
        if self.done_at is not None and time.perf_counter() - self.done_at > 1.2:
            self.app.go(LevelScreen(self.app) if self.first_run else MenuScreen(self.app))

    def draw(self, surf, fps):
        surf.fill(BG)
        with self.lock:
            step, key, captured = self.step, self.key, {k: list(v) for k, v in self.captured.items()}
            capture_start, flash, done_at = self.capture_start, self.flash, self.done_at
        now = time.perf_counter()
        self.f.center(surf, "Set up your kit", self.f.large, TEXT, 60)
        if not self.app.midi_in:
            self.f.center(surf, "No MIDI input connected. Start with --port NAME, or press Esc and play with keys 1-4.",
                          self.f.small, JUDGE_COLORS["MISS"], self.h - 76)
            self.f.center(surf, "Keys 1-4 here stand in for a pad and assign the default General MIDI numbers.",
                          self.f.small, DIM, self.h - 54)

        # progress
        for i, k in enumerate(C.INSTRUMENTS):
            x = self.w / 2 + (i - 1.5) * 150
            state_color = C.COLORS[k] if (i < step or done_at) else (ACCENT if i == step else DIM)
            pygame.draw.circle(surf, state_color, (int(x), 120), 10, 0 if (i < step or done_at) else 2)
            self.f.center(surf, C.LABELS[k], self.f.small, state_color, 145, x)
            got = captured[k]
            self.f.center(surf, "/".join(map(str, got)) if got else ("skipped" if i < step else ""), self.f.small, DIM, 165, x)

        if done_at is not None:
            self.f.center(surf, "Kit saved", self.f.big, JUDGE_COLORS["PERFECT"], self.h * 0.45)
            self.f.center(surf, describe(self.app.kit), self.f.small, DIM, self.h * 0.45 + 60)
            return

        color = C.COLORS[key]
        # big pad: flashes on hit
        cx, cy = self.w / 2, self.h * 0.5
        r = 110
        if flash and now - flash[0] < 0.25:
            k = 1 - (now - flash[0]) / 0.25
            pygame.draw.circle(surf, lerp(LANE_BG, color, k), (int(cx), int(cy)), int(r + 40 * (1 - k)))
        pygame.draw.circle(surf, color, (int(cx), int(cy)), r, 4)
        self.f.center(surf, C.LABELS[key].upper(), self.f.big, color, cy)
        self.f.center(surf, WIZARD_PROMPTS[key], self.f.mid, TEXT, cy - r - 40)

        if captured[key]:
            self.f.center(surf, "got note " + ", ".join(map(str, captured[key])), self.f.mid, JUDGE_COLORS["PERFECT"], cy + r + 40)
            if flash:
                self.f.center(surf, f"last: note {flash[1]} · velocity {flash[2]}", self.f.small, DIM, cy + r + 70)
            if capture_start is not None:
                frac = max(0.0, 1 - (now - capture_start) / CAPTURE_S)
                bw = 300
                pygame.draw.rect(surf, LANE_BG, (cx - bw / 2, cy + r + 95, bw, 6))
                pygame.draw.rect(surf, color, (cx - bw / 2, cy + r + 95, bw * frac, 6))
                self.f.center(surf, "keep hitting, moving on...", self.f.small, DIM, cy + r + 118)
        else:
            self.f.center(surf, "waiting...", self.f.mid, DIM, cy + r + 40)
        self.footer(surf, "Enter next · S skip this drum · Backspace redo previous · Esc cancel")


# ---------------------------------------------------------------------------
class PlayScreen(Screen):
    def __init__(self, app, level_index):
        super().__init__(app)
        self.level_index = level_index
        self.chart = app.levels[level_index]
        self.lanes, self.by_note = build_lanes(self.chart, app.kit)
        self.game = Game(self.chart, self.lanes, self.by_note, offset_ms=app.offset_ms, speed=app.speed,
                         sounds=app.sounds, guide=app.guide)
        self.renderer = Renderer(self.game, app.size, app.fonts)
        self.recorded = False
        self.game.reset()

    def on_note(self, note, velocity):
        self.game.hit(note, velocity)

    def on_key(self, key):
        g = self.game
        if key == pygame.K_ESCAPE:
            self.leave()
            self.app.go(LevelScreen(self.app, self.level_index))
        elif key == pygame.K_SPACE:
            if not g.finished:
                g.toggle_pause()
        elif key == pygame.K_r:
            self.leave()
            self.app.go(PlayScreen(self.app, self.level_index))
        elif key == pygame.K_RETURN and g.finished:
            self.leave()
            nxt = self.level_index + 1
            self.app.go(PlayScreen(self.app, nxt) if nxt < len(self.app.levels) else LevelScreen(self.app, self.level_index))
        elif key == pygame.K_LEFTBRACKET:
            g.speed = self.app.speed = max(0.25, g.speed - 0.25)
        elif key == pygame.K_RIGHTBRACKET:
            g.speed = self.app.speed = min(4.0, g.speed + 0.25)
        elif key == pygame.K_COMMA:
            g.offset_ms = self.app.offset_ms = g.offset_ms - 5
        elif key == pygame.K_PERIOD:
            g.offset_ms = self.app.offset_ms = g.offset_ms + 5
        elif key == pygame.K_g:
            g.guide = self.app.guide = not g.guide
        elif key in KEY_LANES and KEY_LANES[key] < len(self.lanes):
            g.hit_lane(KEY_LANES[key], 100)
        return True

    def leave(self):
        self.record()
        if self.app.args.log:
            self.game.write_csv(self.app.args.log)

    def record(self):
        if self.recorded or not self.game.hits:
            return
        st = self.game.stats()
        best = self.app.results.get(self.chart.name)
        if best is None or st["accuracy"] > best["accuracy"]:
            self.app.results[self.chart.name] = st
        self.recorded = True

    def update(self):
        self.game.update()
        if self.game.finished:
            self.record()

    def draw(self, surf, fps):
        self.renderer.draw(surf, fps)


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="drumhero", description="Guitar Hero style drum trainer driven by MIDI.")
    ap.add_argument("midi", nargs="?", help="MIDI file to add as an extra level")
    ap.add_argument("--channel", type=int, help="only use chart notes on this MIDI channel (1-16)")
    ap.add_argument("--port", help="MIDI input port (substring). Default: first port that looks like a drum module")
    ap.add_argument("--kit", help="kit file to load/save instead of ~/.config/drumhero/kit.json")
    ap.add_argument("--offset", type=float, default=0.0, help="input latency compensation in ms (positive = treat hits as earlier)")
    ap.add_argument("--speed", type=float, default=1.0, help="scroll speed multiplier")
    ap.add_argument("--size", default="1280x720", help="window size WxH")
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--no-sound", action="store_true", help="disable all audio")
    ap.add_argument("--no-guide", action="store_true", help="start with the guide track off")
    ap.add_argument("--log", help="write every judged hit of the last run to this CSV")
    args = ap.parse_args(argv)
    App(args).run()


if __name__ == "__main__":
    main()
