"""Screens (hub, category lists, kit wizard, play) and the main loop.

Navigation is drum-driven: on the hub each drum opens its own colored section;
inside a section the hi-hat moves down, the crash moves up, the snare accepts
and the kick goes back. The keyboard always works too.
"""
import argparse
import glob
import os
import threading
import time
from collections import deque

import mido
import pygame

from . import chart as C
from .chart import BEATS, EXERCISES, build_lanes, load_midi_chart
from .game import Game
from .kit import default_kit, describe, load_kit, load_settings, save_kit, save_settings
from .render import ACCENT, BG, DIM, JUDGE_COLORS, LANE_BG, TEXT, Fonts, Renderer, lerp
from .game import TAIL_S, lead_in_for
from .sounds import (BACKING_GAIN, METRONOME_GAIN, PROGRESSIONS, SoundBank, Track, menu_music_sound,
                     output_devices, render_backing_track, render_metronome)

TARGET_FPS = 240
CAPTURE_S = 1.5           # wizard: keep collecting note numbers this long after the first hit
NAV_DEBOUNCE_S = 0.22     # one drum hit = one menu action, at most this often per drum
NAV_MIN_VELOCITY = 45     # softer hits (sticks resting on the snare) never navigate
RESULTS_GRACE_S = 1.0     # after a level ends, ignore drum hits this long before they navigate
KEY_LANES = {pygame.K_1: 0, pygame.K_2: 1, pygame.K_3: 2, pygame.K_4: 3, pygame.K_5: 4,
             pygame.K_6: 5, pygame.K_7: 6, pygame.K_8: 7, pygame.K_9: 8, pygame.K_0: 9}
MODULE_HINTS = ("td-", "td1", "td2", "td5", "alesis", "nitro", "strike", "dtx", "roland", "drum")
WIZARD_PROMPTS = {
    "kick": "Hit the KICK a few times",
    "snare": "Hit the SNARE a few times",
    "hihat": "Hit the HI-HAT a few times, edge and top, pedal up and down",
    "crash": "Hit the CRASH a few times",
}

# What each drum does inside a list. The hub uses the drums as section buttons instead.
NAV = {"snare": "accept", "kick": "back", "hihat": "next", "crash": "prev"}
CATEGORIES = [
    ("kick", "Exercises", "one drum at a time, slow"),
    ("snare", "Beats", "full grooves"),
    ("hihat", "Songs", "MIDI files from the songs folder"),
    ("crash", "Setup", "kit, sounds, quit"),
]
SONGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "songs")


class App:
    def __init__(self, args):
        self.args = args
        self.settings = load_settings()
        if args.audio_device:
            self.settings["audio_device"] = args.audio_device
        kit = load_kit(args.kit) if args.kit else load_kit()
        self.first_run = kit is None
        self.set_kit(kit or default_kit())
        self.guide = not args.no_guide
        self.backing_on = not args.no_backing
        self.metronome_mode = "off" if args.no_metronome else "full"    # full / beats / off
        self.menu_music_on = not args.no_menu_music
        self.menu_music = None
        self.track_cache = {}
        self.offset_ms = args.offset
        self.speed = args.speed
        self.results = {}          # chart name -> stats of the best run this session
        self.songs = None          # loaded lazily
        self.midi_name = None
        self.midi_in = None
        self.screen_obj = None
        self.drum_queue = deque()  # navigation hits, handed to the screen on the main thread
        self.last_nav = {}
        self.legend_flash = {}     # instrument -> wall time of its last navigation hit

        pygame.init()
        self.sounds = SoundBank(enabled=not args.no_sound, device=self.settings.get("audio_device"))
        w, h = (int(v) for v in args.size.lower().split("x"))
        # RESIZABLE gives the window macOS's green fullscreen button (native Spaces fullscreen).
        self.surface = pygame.display.set_mode((w, h), pygame.RESIZABLE)
        pygame.display.set_caption("drumhero")
        self.size = self.surface.get_size()
        self.fonts = Fonts(self.scale)
        fullscreen = self.settings.get("fullscreen", True)
        if args.fullscreen:
            fullscreen = True
        if args.windowed:
            fullscreen = False
        if fullscreen:
            self.toggle_fullscreen()
            self.on_resize()
        self.open_midi(args.port)

    # --- audio output ------------------------------------------------------------
    def set_audio_device(self, name):
        """Reopen the mixer on another output; every Sound belongs to the old mixer, so rebuild."""
        if self.menu_music is not None:
            self.menu_music.stop()
        if isinstance(self.screen_obj, PlayScreen):
            self.screen_obj.game.stop_tracks()
        self.settings["audio_device"] = name
        save_settings(self.settings)
        self.sounds = SoundBank(enabled=not self.args.no_sound, device=name)
        self.track_cache = {}
        self.menu_music = None
        if isinstance(self.screen_obj, PlayScreen):
            self.screen_obj.game.sounds = self.sounds
        self.update_menu_music()

    def cycle_audio_device(self):
        names = [None] + output_devices()
        current = self.sounds.device
        i = names.index(current) if current in names else 0
        self.set_audio_device(names[(i + 1) % len(names)])

    # --- window --------------------------------------------------------------------
    @property
    def scale(self):
        return self.size[1] / 720

    def toggle_fullscreen(self):
        try:
            pygame.display.toggle_fullscreen()
        except pygame.error as e:
            print(f"fullscreen: {e}")

    def on_resize(self):
        """The window changed size (green button, drag, fullscreen toggle): relayout everything."""
        self.surface = pygame.display.get_surface()
        new = self.surface.get_size()
        if new == self.size and self.fonts.scale == self.scale:
            return
        self.size = new
        self.fonts = Fonts(self.scale)
        if self.screen_obj is not None:
            self.screen_obj.on_resize()

    # --- kit / content -----------------------------------------------------------
    def set_kit(self, kit):
        self.kit = kit
        self.note_to_inst = {}
        for inst in reversed(C.INSTRUMENTS):
            for n in kit.get(inst, []):
                self.note_to_inst[n] = inst

    def instrument_for(self, note):
        return self.note_to_inst.get(note)

    def has_drum(self, inst):
        return bool(self.midi_in) and bool(self.kit.get(inst))

    def load_songs(self):
        if self.songs is None:
            self.songs = []
            paths = list(self.args.midi or [])
            for d in [self.args.songs or SONGS_DIR]:
                paths += sorted(glob.glob(os.path.join(d, "*.mid")) + glob.glob(os.path.join(d, "*.midi")))
            seen = set()
            for p in paths:
                if p in seen:
                    continue
                seen.add(p)
                try:
                    ch = load_midi_chart(p, None if self.args.channel is None else self.args.channel - 1)
                except SystemExit as e:
                    print(e)
                    continue
                self.songs.append(ch)
        return self.songs

    def items_for(self, cat):
        return {"kick": EXERCISES, "snare": BEATS, "hihat": self.load_songs()}.get(cat, [])

    def tracks_for(self, chart, prog_index):
        """Pre-rendered backing (built-in levels only, prog_index None = none) and metronome
        for this chart, on its timeline from -lead_in. Cached per chart and metronome mode."""
        if not self.sounds.ok:
            return {}
        lead_in = lead_in_for(chart.bpm)
        total = chart.length + TAIL_S + 0.5
        out = {}
        if prog_index is not None:
            key = ("backing", chart.name, round(chart.bpm, 3), prog_index)
            if key not in self.track_cache:
                self.track_cache[key] = Track(render_backing_track(chart.bpm, prog_index, lead_in, total), -lead_in, BACKING_GAIN)
            out["backing"] = self.track_cache[key]
        if self.metronome_mode != "off":
            key = ("metro", chart.name, round(chart.bpm, 3), self.metronome_mode)
            if key not in self.track_cache:
                self.track_cache[key] = Track(render_metronome(chart, lead_in, total, self.metronome_mode), -lead_in, METRONOME_GAIN)
            out["metronome"] = self.track_cache[key]
        return out

    # --- menu music ------------------------------------------------------------------
    def update_menu_music(self):
        """Ambient loop in every screen except play. Called on each screen change."""
        if not self.sounds.ok:
            return
        want = self.menu_music_on and not isinstance(self.screen_obj, PlayScreen)
        if want and self.menu_music is None:
            self.menu_music = menu_music_sound()
        if want:
            if not (self.menu_music.get_num_channels() > 0):
                self.menu_music.play(loops=-1, fade_ms=600)
        elif self.menu_music is not None:
            self.menu_music.fadeout(300)

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
            print("No MIDI input: keyboard only. Inputs: " + (", ".join(names) or "none"))

    def on_midi(self, msg):
        if msg.type == "note_on" and msg.velocity > 0:
            scr = self.screen_obj
            if scr is not None:
                scr.on_note(msg.note, msg.velocity)

    def nav_hit(self, note, velocity):
        """A drum hit used as a button. Debounced, sounded, queued for the main thread."""
        inst = self.instrument_for(note)
        if inst is None or velocity < NAV_MIN_VELOCITY:
            return
        now = time.perf_counter()
        if now - self.last_nav.get(inst, 0) < NAV_DEBOUNCE_S:
            return
        self.last_nav[inst] = now
        self.legend_flash[inst] = now
        self.sounds.play(inst, velocity, 0.8)
        self.drum_queue.append(inst)

    # --- screens ---------------------------------------------------------------
    def go(self, screen):
        self.screen_obj = screen
        self.update_menu_music()

    def run(self):
        self.go(SetupScreen(self, first_run=True) if self.first_run and self.midi_in else HubScreen(self))
        clock = pygame.time.Clock()
        running = True
        while running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    running = False
                elif ev.type in (pygame.VIDEORESIZE, pygame.WINDOWSIZECHANGED):
                    self.on_resize()
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_F11 or (ev.key == pygame.K_f and ev.mod & (pygame.KMOD_META | pygame.KMOD_CTRL)):
                        self.toggle_fullscreen()
                    elif self.screen_obj.on_key(ev.key) is False:
                        running = False
            while self.drum_queue:
                if self.screen_obj.on_drum(self.drum_queue.popleft()) is False:
                    running = False
            self.screen_obj.update()
            self.screen_obj.draw(self.surface, clock.get_fps())
            pygame.display.flip()
            clock.tick(TARGET_FPS)
        if self.midi_in:
            self.midi_in.close()
        pygame.quit()


# ---------------------------------------------------------------------------
class Screen:
    def __init__(self, app: App):
        self.app = app

    # window geometry is read live so a resize or fullscreen toggle relayouts every screen
    @property
    def f(self):
        return self.app.fonts

    @property
    def w(self):
        return self.app.size[0]

    @property
    def h(self):
        return self.app.size[1]

    @property
    def s(self):
        return self.app.scale

    def on_resize(self):
        pass

    def on_note(self, note, velocity):
        """MIDI thread. Default: the hit is a button press."""
        self.app.nav_hit(note, velocity)

    def on_drum(self, inst):
        """Main thread. Return False to quit."""
        return True

    def on_key(self, key):
        """Return False to quit."""
        return True

    def update(self):
        pass

    def draw(self, surf, fps):
        pass

    # --- shared widgets ----------------------------------------------------------
    def legend(self, surf, pairs, y=None, keys=None):
        """Colored drum chips: [(instrument, action label), ...]. Flash when that drum is hit."""
        S = self.s
        y = self.h - 44 * S if y is None else y
        now = time.perf_counter()
        chips = []
        for inst, label in pairs:
            ts = self.f.text(f"{C.LABELS[inst]}  {label}", self.f.small, TEXT)
            chips.append((inst, ts))
        gap, pad, r = 26 * S, 12 * S, 8 * S
        total = sum(ts.get_width() + 2 * r + pad for _, ts in chips) + gap * (len(chips) - 1)
        x = self.w / 2 - total / 2
        for inst, ts in chips:
            color = C.COLORS[inst]
            k = max(0.0, 1 - (now - self.app.legend_flash.get(inst, 0)) / 0.25)
            w = ts.get_width() + 2 * r + pad
            box = pygame.Rect(int(x - 10 * S), int(y - 14 * S), int(w + 20 * S), int(28 * S))
            pygame.draw.rect(surf, lerp(LANE_BG, color, 0.6 * k), box, border_radius=int(14 * S))
            pygame.draw.rect(surf, color if k > 0 else (50, 50, 60), box, 1, border_radius=int(14 * S))
            pygame.draw.circle(surf, color, (int(x + r), int(y)), int(r + 3 * k * S))
            surf.blit(ts, (x + 2 * r + pad - 4, y - ts.get_height() / 2))
            x += w + gap
        if keys:
            self.f.center(surf, keys, self.f.small, DIM, y + 26 * S)

    def midi_line(self):
        return f"MIDI: {self.app.midi_name}" if self.app.midi_in else "no MIDI input, keyboard only"


# ---------------------------------------------------------------------------
class HubScreen(Screen):
    """Four colored sections, one per drum. Strike the drum to open its section."""

    def __init__(self, app, sel=0):
        super().__init__(app)
        self.sel = sel
        self.flash = None          # (wall_t, category index)

    def open(self, i):
        cat = CATEGORIES[i][0]
        self.flash = (time.perf_counter(), i)
        self.app.go(ListScreen(self.app, cat))

    def on_drum(self, inst):
        for i, (cat, _, _) in enumerate(CATEGORIES):
            if cat == inst:
                self.open(i)
        return True

    def on_key(self, key):
        if key in (pygame.K_ESCAPE, pygame.K_q):
            return False
        if key in (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4):
            self.open(key - pygame.K_1)
        elif key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_h, pygame.K_l):
            self.sel ^= 1
        elif key in (pygame.K_UP, pygame.K_DOWN, pygame.K_k, pygame.K_j):
            self.sel ^= 2
        elif key in (pygame.K_RETURN, pygame.K_SPACE):
            self.open(self.sel)
        return True

    def draw(self, surf, fps):
        surf.fill(BG)
        S = self.s
        self.f.center(surf, "drumhero", self.f.big, TEXT, 52 * S)
        self.f.center(surf, "strike a drum to open its section", self.f.small, DIM, 92 * S)
        gap, top, bottom, side = 18 * S, 118 * S, self.h - 70 * S, 60 * S
        pw = (self.w - 2 * side - gap) / 2
        ph = (bottom - top - gap) / 2
        now = time.perf_counter()
        for i, (cat, title, sub) in enumerate(CATEGORIES):
            col, row = i % 2, i // 2
            x, y = side + col * (pw + gap), top + row * (ph + gap)
            color = C.COLORS[cat]
            hot = max(0.0, 1 - (now - self.app.legend_flash.get(cat, 0)) / 0.3)
            fill = lerp(lerp(LANE_BG, color, 0.14), color, 0.5 * hot)
            rect = pygame.Rect(int(x), int(y), int(pw), int(ph))
            pygame.draw.rect(surf, fill, rect, border_radius=int(18 * S))
            pygame.draw.rect(surf, color if (i == self.sel or hot) else lerp(LANE_BG, color, 0.5), rect,
                             3 if i == self.sel else 2, border_radius=int(18 * S))
            # drum chip
            pygame.draw.circle(surf, color, (int(x + 34 * S), int(y + 34 * S)), int(12 * S))
            surf.blit(self.f.text(f"{C.LABELS[cat]}", self.f.mid, color), (x + 56 * S, y + 20 * S))
            surf.blit(self.f.text(f"key {i + 1}", self.f.small, DIM), (x + pw - 70 * S, y + 24 * S))
            self.f.center(surf, title, self.f.big, TEXT, y + ph / 2 - 6 * S, x + pw / 2)
            self.f.center(surf, sub, self.f.small, DIM, y + ph / 2 + 36 * S, x + pw / 2)
            if cat in ("kick", "snare"):
                n = len(self.app.items_for(cat))
                self.f.center(surf, f"{n} levels", self.f.small, color, y + ph - 26 * S, x + pw / 2)
            elif cat == "hihat":
                n = len(self.app.songs) if self.app.songs is not None else None
                self.f.center(surf, f"{n} songs" if n is not None else "songs/ folder", self.f.small, color, y + ph - 26 * S, x + pw / 2)
            else:
                self.f.center(surf, describe(self.app.kit), self.f.small, color, y + ph - 26 * S, x + pw / 2)
            if not self.app.has_drum(cat) and self.app.midi_in:
                self.f.center(surf, "no pad assigned", self.f.small, JUDGE_COLORS["MISS"], y + 60 * S, x + pw / 2)
        self.f.center(surf, f"{self.midi_line()}   ·   keys 1-4, arrows or hjkl + Enter   ·   F11 fullscreen   ·   Esc quit",
                      self.f.small, DIM, self.h - 30 * S)


# ---------------------------------------------------------------------------
class ListScreen(Screen):
    """A section's items. Hi-hat down, crash up, snare accept, kick back."""

    def __init__(self, app, cat, sel=0):
        super().__init__(app)
        self.cat = cat
        self.title, self.sub = next((t, s) for c, t, s in CATEGORIES if c == cat)
        self.color = C.COLORS[cat]
        self.sel = sel

    def items(self):
        if self.cat == "crash":
            return [("Set up kit", describe(self.app.kit)),
                    ("Soundcheck", "hit every pad, see where it lands and hear it"),
                    (f"Guide sounds: {'on' if self.app.guide else 'off'}", "hear the chart as it crosses the line"),
                    (f"Backing loop: {'on' if self.app.backing_on else 'off'}", "bass, chords and arpeggio under the built-in levels"),
                    (f"Metronome: {self.app.metronome_mode}", "congas: full follows the subdivision, beats only marks the beats"),
                    (f"Menu music: {'on' if self.app.menu_music_on else 'off'}", "ambient texture outside the game"),
                    (f"Audio output: {self.app.sounds.device or 'system default'}", "select cycles through the outputs, saved"),
                    (f"Start fullscreen: {'on' if self.app.settings.get('fullscreen', True) else 'off'}", "F11 or Cmd+F toggles any time, saved"),
                    ("Quit", "")]
        return [(ch.name, f"{ch.bpm:.0f} bpm · {len(ch.notes):3d} notes · {ch.desc}") for ch in self.app.items_for(self.cat)]

    def move(self, d):
        n = len(self.items())
        if n:
            self.sel = (self.sel + d) % n

    def accept(self):
        if self.cat == "crash":
            if self.sel == 0:
                self.app.go(SetupScreen(self.app))
            elif self.sel == 1:
                self.app.go(SoundcheckScreen(self.app))
            elif self.sel == 2:
                self.app.guide = not self.app.guide
            elif self.sel == 3:
                self.app.backing_on = not self.app.backing_on
            elif self.sel == 4:
                modes = ["full", "beats", "off"]
                self.app.metronome_mode = modes[(modes.index(self.app.metronome_mode) + 1) % 3]
            elif self.sel == 5:
                self.app.menu_music_on = not self.app.menu_music_on
                self.app.update_menu_music()
            elif self.sel == 6:
                self.app.cycle_audio_device()
            elif self.sel == 7:
                self.app.settings["fullscreen"] = not self.app.settings.get("fullscreen", True)
                save_settings(self.app.settings)
            else:
                return False
        elif self.items():
            self.app.go(PlayScreen(self.app, self.cat, self.sel))
        return True

    def back(self):
        self.app.go(HubScreen(self.app, CATEGORIES.index(next(c for c in CATEGORIES if c[0] == self.cat))))

    def on_drum(self, inst):
        action = NAV.get(inst)
        if action == "next":
            self.move(1)
        elif action == "prev":
            self.move(-1)
        elif action == "accept":
            return self.accept()
        elif action == "back":
            self.back()
        return True

    def on_key(self, key):
        if key in (pygame.K_DOWN, pygame.K_j):
            self.move(1)
        elif key in (pygame.K_UP, pygame.K_k):
            self.move(-1)
        elif key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_l):
            return self.accept()
        elif key in (pygame.K_ESCAPE, pygame.K_h):
            self.back()
        return True

    def draw(self, surf, fps):
        surf.fill(BG)
        S = self.s
        pygame.draw.rect(surf, lerp(LANE_BG, self.color, 0.14), (0, 0, self.w, 96 * S))
        pygame.draw.circle(surf, self.color, (int(self.w * 0.12 - 30 * S), int(48 * S)), int(12 * S))
        surf.blit(self.f.text(self.title, self.f.big, TEXT), (self.w * 0.12, 20 * S))
        ts = self.f.text(self.sub, self.f.small, DIM)
        surf.blit(ts, (self.w * 0.88 - ts.get_width(), 60 * S))
        items = self.items()
        y = 140 * S
        if not items:
            self.f.center(surf, "No songs yet.", self.f.mid, TEXT, self.h * 0.42)
            self.f.center(surf, f"Drop .mid files into {self.app.args.songs or SONGS_DIR}", self.f.small, DIM, self.h * 0.42 + 36 * S)
            self.f.center(surf, "or pass them on the command line. Drums on MIDI channel 10 work best.", self.f.small, DIM, self.h * 0.42 + 58 * S)
        row_h = 44 * S
        max_rows = int((self.h - 230 * S) / row_h)
        first = max(0, min(self.sel - max_rows // 2, len(items) - max_rows))
        for i in range(first, min(len(items), first + max_rows)):
            name, sub = items[i]
            selected = i == self.sel
            x = self.w * 0.12
            if selected:
                pygame.draw.rect(surf, lerp(LANE_BG, self.color, 0.18), (x - 20 * S, y - 8 * S, self.w * 0.76 + 40 * S, row_h - 4 * S), border_radius=int(10 * S))
                pygame.draw.rect(surf, self.color, (x - 20 * S, y - 8 * S, 6 * S, row_h - 4 * S), border_radius=int(3 * S))
            shown = name if len(name) <= 22 else name[:21] + "…"
            surf.blit(self.f.text(shown, self.f.mid, self.color if selected else TEXT), (x, y))
            surf.blit(self.f.text(sub, self.f.small, DIM), (x + 370 * S, y + 4 * S))
            best = self.app.results.get(name)
            if best:
                s = f"best {best['accuracy'] * 100:.0f}%  mean {best['mean_ms']:+.0f} ms"
                ts = self.f.text(s, self.f.small, JUDGE_COLORS["PERFECT"] if best["accuracy"] >= 0.9 else JUDGE_COLORS["GOOD"])
                surf.blit(ts, (self.w * 0.88 - ts.get_width(), y + 4 * S))
            y += row_h
        self.legend(surf, [("hihat", "down"), ("crash", "up"), ("snare", "select"), ("kick", "back")],
                    keys="arrows or j k · Enter or l · Esc or h")


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

    def on_note(self, note, velocity):       # drums are being captured here, never navigation
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
            kit[k] = [n for n in C.expand_family(self.captured[k]) if n not in taken]
            taken.update(kit[k])
        self.app.set_kit(kit)
        save_kit(kit, self.app.args.kit) if self.app.args.kit else save_kit(kit)
        self.done_at = time.perf_counter()

    def on_key(self, key):
        if key == pygame.K_ESCAPE:
            self.app.go(HubScreen(self.app, 3))
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
            self.on_note(C.DEFAULT_KIT[self.key][0], 100)   # keyboard stands in for a pad
        return True

    def update(self):
        with self.lock:
            if self.done_at is None and self.capture_start is not None and \
                    time.perf_counter() - self.capture_start >= CAPTURE_S:
                self.advance()
        if self.done_at is not None and time.perf_counter() - self.done_at > 1.2:
            self.app.go(SoundcheckScreen(self.app, first_run=self.first_run))

    def draw(self, surf, fps):
        surf.fill(BG)
        with self.lock:
            step, key, captured = self.step, self.key, {k: list(v) for k, v in self.captured.items()}
            capture_start, flash, done_at = self.capture_start, self.flash, self.done_at
        now = time.perf_counter()
        S = self.s
        self.f.center(surf, "Set up your kit", self.f.large, TEXT, 60 * S)
        if not self.app.midi_in:
            self.f.center(surf, "No MIDI input connected. Start with --port NAME, or press Esc and play with keys 1-4.",
                          self.f.small, JUDGE_COLORS["MISS"], self.h - 76 * S)
            self.f.center(surf, "Keys 1-4 here stand in for a pad and assign the default General MIDI numbers.",
                          self.f.small, DIM, self.h - 54 * S)

        for i, k in enumerate(C.INSTRUMENTS):
            x = self.w / 2 + (i - 1.5) * 150 * S
            state_color = C.COLORS[k] if (i < step or done_at) else (ACCENT if i == step else DIM)
            pygame.draw.circle(surf, state_color, (int(x), int(120 * S)), int(10 * S), 0 if (i < step or done_at) else 2)
            self.f.center(surf, C.LABELS[k], self.f.small, state_color, 145 * S, x)
            got = captured[k]
            self.f.center(surf, "/".join(map(str, got)) if got else ("skipped" if i < step else ""), self.f.small, DIM, 165 * S, x)

        if done_at is not None:
            self.f.center(surf, "Kit saved", self.f.big, JUDGE_COLORS["PERFECT"], self.h * 0.45)
            self.f.center(surf, describe(self.app.kit), self.f.small, DIM, self.h * 0.45 + 60 * S)
            return

        color = C.COLORS[key]
        cx, cy = self.w / 2, self.h * 0.5
        r = 110 * S
        if flash and now - flash[0] < 0.25:
            k = 1 - (now - flash[0]) / 0.25
            pygame.draw.circle(surf, lerp(LANE_BG, color, k), (int(cx), int(cy)), int(r + 40 * S * (1 - k)))
        pygame.draw.circle(surf, color, (int(cx), int(cy)), int(r), max(2, int(4 * S)))
        self.f.center(surf, C.LABELS[key].upper(), self.f.big, color, cy)
        self.f.center(surf, WIZARD_PROMPTS[key], self.f.mid, TEXT, cy - r - 40 * S)

        if captured[key]:
            fam = [n for n in C.expand_family(captured[key]) if n not in captured[key]]
            got = "got note " + ", ".join(map(str, captured[key])) + (f"  (+ {', '.join(map(str, fam))} same pad)" if fam else "")
            self.f.center(surf, got, self.f.mid, JUDGE_COLORS["PERFECT"], cy + r + 40 * S)
            if flash:
                self.f.center(surf, f"last: note {flash[1]} · velocity {flash[2]}", self.f.small, DIM, cy + r + 70 * S)
            if capture_start is not None:
                frac = max(0.0, 1 - (now - capture_start) / CAPTURE_S)
                bw = 300 * S
                pygame.draw.rect(surf, LANE_BG, (cx - bw / 2, cy + r + 95 * S, bw, 6 * S))
                pygame.draw.rect(surf, color, (cx - bw / 2, cy + r + 95 * S, bw * frac, 6 * S))
                self.f.center(surf, "keep hitting, moving on...", self.f.small, DIM, cy + r + 118 * S)
        else:
            self.f.center(surf, "waiting...", self.f.mid, DIM, cy + r + 40 * S)
        self.f.center(surf, "Enter next · S skip this drum · Backspace redo previous · Esc cancel", self.f.small, DIM, self.h - 28 * S)


# ---------------------------------------------------------------------------
class SoundcheckScreen(Screen):
    """Hit every pad: it lights up, plays its sound and shows the note number and velocity.
    Once every assigned drum has been heard, the snare continues and the kick redoes the wizard."""

    def __init__(self, app, first_run=False):
        super().__init__(app)
        self.first_run = first_run
        self.lock = threading.Lock()
        self.heard = {k: None for k in C.INSTRUMENTS}     # instrument -> (wall_t, note, velocity)
        self.unknown = None                                # (wall_t, note, velocity) for unassigned pads

    def needed(self):
        return [k for k in C.INSTRUMENTS if self.app.kit.get(k)]

    def all_heard(self):
        return all(self.heard[k] for k in self.needed())

    def on_note(self, note, velocity):
        inst = self.app.instrument_for(note)
        now = time.perf_counter()
        with self.lock:
            if inst is None:
                self.unknown = (now, note, velocity)
                return
            navigate = self.all_heard() and NAV.get(inst) in ("accept", "back")
            self.heard[inst] = (now, note, velocity)
        if navigate:
            self.app.nav_hit(note, velocity)
        else:
            self.app.sounds.play(inst, velocity)

    def on_drum(self, inst):
        action = NAV.get(inst)
        if action == "accept":
            self.done()
        elif action == "back":
            self.app.go(SetupScreen(self.app, first_run=self.first_run))
        return True

    def on_key(self, key):
        if key in (pygame.K_RETURN, pygame.K_ESCAPE):
            self.done()
        elif key == pygame.K_BACKSPACE:
            self.app.go(SetupScreen(self.app, first_run=self.first_run))
        elif key in KEY_LANES and KEY_LANES[key] < 4:
            inst = C.INSTRUMENTS[KEY_LANES[key]]
            notes = self.app.kit.get(inst) or C.DEFAULT_KIT[inst]
            self.on_note(notes[0], 100)
        return True

    def done(self):
        self.app.go(HubScreen(self.app, 0 if self.first_run else 3))

    def draw(self, surf, fps):
        surf.fill(BG)
        with self.lock:
            heard = dict(self.heard)
            unknown = self.unknown
        now = time.perf_counter()
        S = self.s
        ready = self.all_heard()
        self.f.center(surf, "Soundcheck", self.f.large, TEXT, 60 * S)
        self.f.center(surf, "hit every pad: it should light up its drum and sound like it", self.f.small, DIM, 96 * S)

        n = len(C.INSTRUMENTS)
        cy, r = self.h * 0.47, 82 * S
        for i, inst in enumerate(C.INSTRUMENTS):
            cx = self.w / 2 + (i - (n - 1) / 2) * 250 * S
            color = C.COLORS[inst]
            assigned = bool(self.app.kit.get(inst))
            h = heard[inst]
            k = max(0.0, 1 - (now - h[0]) / 0.3) if h else 0.0
            vel = h[2] / 127 if h else 0
            if k > 0:
                pygame.draw.circle(surf, lerp(LANE_BG, color, 0.5 * k), (int(cx), int(cy)), int(r + (30 + 40 * vel) * S * (1 - k)))
            pygame.draw.circle(surf, lerp(LANE_BG, color, 0.25 + 0.75 * k) if assigned else LANE_BG, (int(cx), int(cy)), int(r))
            pygame.draw.circle(surf, color if assigned else (60, 60, 70), (int(cx), int(cy)), int(r), max(2, int(4 * S)))
            self.f.center(surf, C.LABELS[inst].upper(), self.f.mid, TEXT if assigned else DIM, cy, cx)
            notes = "/".join(map(str, sorted(self.app.kit.get(inst, [])))) or "not assigned"
            self.f.center(surf, notes, self.f.small, DIM, cy + r + 24 * S, cx)
            if h:
                self.f.center(surf, f"note {h[1]} · vel {h[2]}", self.f.small, color, cy + r + 46 * S, cx)
                self.f.center(surf, "✓", self.f.mid, JUDGE_COLORS["PERFECT"], cy - r - 24 * S, cx)
            elif assigned:
                self.f.center(surf, "waiting", self.f.small, DIM, cy + r + 46 * S, cx)

        if unknown and now - unknown[0] < 2.5:
            self.f.center(surf, f"note {unknown[1]} is not assigned to any drum (vel {unknown[2]})",
                          self.f.mid, JUDGE_COLORS["MISS"], self.h * 0.78)
            self.f.center(surf, "if that pad should count, redo the setup and hit it during its drum", self.f.small, DIM, self.h * 0.78 + 30 * S)

        if ready:
            self.f.center(surf, "All pads heard.", self.f.mid, JUDGE_COLORS["PERFECT"], self.h * 0.78 - 30 * S if not unknown or now - unknown[0] >= 2.5 else self.h * 0.70)
            self.legend(surf, [("snare", "continue"), ("kick", "redo setup")], keys="Enter continue · Backspace redo setup")
        else:
            self.f.center(surf, "Enter skip · Backspace redo setup · keys 1-4 stand in for the pads", self.f.small, DIM, self.h - 28 * S)


# ---------------------------------------------------------------------------
class PlayScreen(Screen):
    def __init__(self, app, cat, index):
        super().__init__(app)
        self.cat, self.index = cat, index
        self.chart = app.items_for(cat)[index]
        self.lanes, self.by_note = build_lanes(self.chart, app.kit)
        self.game = Game(self.chart, self.lanes, self.by_note, offset_ms=app.offset_ms, speed=app.speed,
                         sounds=app.sounds, guide=app.guide)
        self.game.metronome_mode = app.metronome_mode
        prog = None if cat == "hihat" else index + (0 if cat == "kick" else 2)   # songs bring their own music
        for name, track in app.tracks_for(self.chart, prog).items():
            self.game.set_track(name, track, enabled=(app.backing_on if name == "backing" else True))
        self.renderer = Renderer(self.game, app.size, app.fonts)
        self.recorded = False
        self.finished_at = None
        self.game.reset()

    def on_resize(self):
        self.renderer = Renderer(self.game, self.app.size, self.app.fonts)

    def nav_ready(self):
        return self.finished_at is not None and time.perf_counter() - self.finished_at > RESULTS_GRACE_S

    def on_note(self, note, velocity):        # MIDI thread: judge immediately while playing
        if self.nav_ready():
            self.app.nav_hit(note, velocity)
        else:
            self.game.hit(note, velocity)

    def on_drum(self, inst):
        if not self.nav_ready():
            return True
        action = NAV.get(inst)
        if action == "accept":
            self.next_level()
        elif action == "next":
            self.retry()
        elif action == "back":
            self.to_list()
        return True

    def on_key(self, key):
        g = self.game
        if key == pygame.K_ESCAPE:
            self.to_list()
        elif key == pygame.K_SPACE:
            if not g.finished:
                g.toggle_pause()
        elif key == pygame.K_r:
            self.retry()
        elif key == pygame.K_RETURN and g.finished:
            self.next_level()
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
        elif key == pygame.K_b:
            self.app.backing_on = not self.app.backing_on
            g.enable_track("backing", self.app.backing_on)
        elif key == pygame.K_m:
            modes = ["full", "beats", "off"]
            self.app.metronome_mode = modes[(modes.index(self.app.metronome_mode) + 1) % 3]
            g.metronome_mode = self.app.metronome_mode
            if self.app.metronome_mode == "off":
                g.enable_track("metronome", False)
            else:
                prog = None if self.cat == "hihat" else self.index + (0 if self.cat == "kick" else 2)
                g.set_track("metronome", self.app.tracks_for(self.chart, prog)["metronome"], True)
        elif key in KEY_LANES and KEY_LANES[key] < len(self.lanes) and not g.finished:
            g.hit_lane(KEY_LANES[key], 100)
        return True

    def to_list(self):
        self.leave()
        self.app.go(ListScreen(self.app, self.cat, self.index))

    def retry(self):
        self.leave()
        self.app.go(PlayScreen(self.app, self.cat, self.index))

    def next_level(self):
        self.leave()
        nxt = self.index + 1
        if nxt < len(self.app.items_for(self.cat)):
            self.app.go(PlayScreen(self.app, self.cat, nxt))
        else:
            self.app.go(ListScreen(self.app, self.cat, self.index))

    def leave(self):
        self.game.stop_tracks()
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
            if self.finished_at is None:
                self.finished_at = time.perf_counter()

    def draw(self, surf, fps):
        self.renderer.draw(surf, fps)
        if self.game.finished:
            self.legend(surf, [("snare", "next"), ("hihat", "retry"), ("kick", "back")], y=self.h - 30 * self.s,
                        keys=None)


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="drumhero", description="Guitar Hero style drum trainer driven by MIDI.")
    ap.add_argument("midi", nargs="*", help="MIDI files to add to the Songs section")
    ap.add_argument("--songs", help=f"folder scanned for .mid songs (default: {SONGS_DIR})")
    ap.add_argument("--channel", type=int, help="only use chart notes on this MIDI channel (1-16)")
    ap.add_argument("--port", help="MIDI input port (substring). Default: first port that looks like a drum module")
    ap.add_argument("--kit", help="kit file to load/save instead of ~/.config/drumhero/kit.json")
    ap.add_argument("--offset", type=float, default=0.0, help="input latency compensation in ms (positive = treat hits as earlier)")
    ap.add_argument("--speed", type=float, default=1.0, help="scroll speed multiplier")
    ap.add_argument("--size", default="1280x720", help="window size WxH")
    ap.add_argument("--fullscreen", action="store_true", help="start in fullscreen (the saved default is on)")
    ap.add_argument("--windowed", action="store_true", help="start in a window, ignoring the saved setting")
    ap.add_argument("--audio-device", help="audio output name (substring), e.g. XR18; saved setting otherwise")
    ap.add_argument("--no-sound", action="store_true", help="disable all audio")
    ap.add_argument("--no-guide", action="store_true", help="start with the guide track off")
    ap.add_argument("--no-backing", action="store_true", help="start with the backing loop off")
    ap.add_argument("--no-metronome", action="store_true", help="start with the metronome off")
    ap.add_argument("--no-menu-music", action="store_true", help="no ambient music in the menus")
    ap.add_argument("--log", help="write every judged hit of the last run to this CSV")
    args = ap.parse_args(argv)
    App(args).run()


if __name__ == "__main__":
    main()
