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
from .chart import BEATS, EXERCISES, build_lanes, load_midi_chart, load_song_folder
from .game import Game
from .kit import (default_kit, describe, describe_pads, load_kit, load_progress, load_settings, save_kit,
                  save_progress, save_settings)
from .runlog import RunLog
from .capture import Recorder
from . import capture as CP
from . import edit as E
from . import stats as ST
from .coach import Coach
from .devices import DeviceWatcher, Toasts, TOAST_S
from .render import ACCENT, BG, DIM, JUDGE_COLORS, LANE_BG, TEXT, Fonts, Renderer, draw_hihat_state, draw_stars, lerp
from .game import TAIL_S, lead_in_for
from . import ghost as GH
from .ghost import GhostFilter
from .sounds import (BACKING_GAIN, METRONOME_GAIN, PROGRESSIONS, SoundBank, Track, load_audio_track,
                     menu_music_sound, output_devices, render_backing_track, render_metronome, MENU_CHANNEL)

TARGET_FPS = 240
CAPTURE_S = 1.5           # wizard: keep collecting note numbers this long after the first hit
NAV_DEBOUNCE_S = 0.22     # one drum hit = one menu action, at most this often per drum
NAV_MIN_VELOCITY = 45     # softer hits (sticks resting on the snare) never navigate
RESULTS_GRACE_S = 1.0     # after a level ends, ignore drum hits this long before they navigate
KEY_LANES = {pygame.K_1: 0, pygame.K_2: 1, pygame.K_3: 2, pygame.K_4: 3, pygame.K_5: 4,
             pygame.K_6: 5, pygame.K_7: 6, pygame.K_8: 7, pygame.K_9: 8, pygame.K_0: 9}
MODULE_HINTS = ("td-", "td1", "td2", "td5", "alesis", "nitro", "strike", "dtx", "roland", "drum")

# What each drum does inside a list. The hub uses the drums as section buttons instead.
NAV = {"snare": "accept", "kick": "back", "hihat": "next", "crash": "prev", "crash2": "prev"}
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
        self.offset_ms = args.offset if args.offset else float(self.settings.get("offset_ms") or 0.0)
        self.rate = args.speed          # tempo multiplier for levels, 1.0 = as written
        self.results = load_progress()   # chart name -> best stats so far (stars, grade...), saved
        self.songs = None          # loaded lazily
        self.midi_name = None
        self.midi_in = None
        self.screen_obj = None
        self.fullscreen = False
        self.drum_queue = deque()  # navigation hits, handed to the screen on the main thread
        self.ghosts = GhostFilter()  # drops the hi-hat notes the pedal produces on its own
        self.runlog = RunLog()       # every level is written to ~/Library/Logs/drumhero/runs when it ends
        self.recorder = Recorder(self.settings)   # V: take of the game, the interface's mix and the camera
        self.editor = E.Editor(self.settings.get("claude_bin"))   # Edit with Claude
        self.coach = Coach(self.settings.get("claude_bin"), self.settings.get("coach_language", "es"),
                           self.settings.get("coach_model"))
        self.session = None          # {"name", "items": [(cat, index, rate, reps, why)], "pos", "rep"} while a playlist runs
        self.toasts = Toasts()
        self.camera_name = None
        self.watcher = DeviceWatcher(args.port, self.settings.get("audio_device"), self.settings.get("capture_camera", "iPhone"),
                                     midi_hints=MODULE_HINTS)
        self.midi_trace = None       # one line per note-on, for latency measurements (--midi-trace or settings)
        trace = getattr(args, "midi_trace", None) or self.settings.get("midi_trace")
        if trace:
            self.midi_trace = open(os.path.expanduser(trace), "a")
        self.last_nav = {}
        self.legend_flash = {}     # instrument -> wall time of its last navigation hit

        # display and fonts only: pygame.init() would open the mixer here, and opening an audio
        # device can block on macOS's microphone prompt (seen 2026-09-08: the window never came
        # up). The mixer opens in a thread instead and swaps in when ready.
        pygame.display.init()
        pygame.font.init()
        self.sounds = SoundBank(enabled=False)
        self.sounds_ready = threading.Event()
        self._sounds_pending = None
        if not args.no_sound:
            threading.Thread(target=self._open_sounds, daemon=True).start()
        else:
            self.sounds_ready.set()
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
        self.watcher.state["midi"] = self.midi_name
        self.watcher.state["audio"] = self.sounds.device or ("system default" if not self.settings.get("audio_device") else None)
        if not (args.port == "__none__"):
            self.watcher.start()

    # --- audio output ------------------------------------------------------------
    def _open_sounds(self):
        """Background: build the mixer and the kit; the main loop adopts it (adopt_sounds)."""
        try:
            bank = SoundBank(enabled=True, device=self.settings.get("audio_device"), drums=self.settings.get("drum_sounds", True))
        except Exception as e:                                  # noqa: BLE001
            print(f"audio failed to open: {e}")
            bank = SoundBank(enabled=False)
        self._sounds_pending = bank

    def adopt_sounds(self):
        """Main thread: swap in the mixer opened in the background, once."""
        bank = self._sounds_pending
        if bank is None:
            return
        self._sounds_pending = None
        self.sounds = bank
        if isinstance(self.screen_obj, PlayScreen):
            self.screen_obj.game.sounds = bank
        self.sounds_ready.set()
        if bank.ok:
            self.toasts.add(f"audio ready: {bank.device or 'system default'}", DIM)
        self.update_menu_music()

    def wait_sounds(self, timeout=15.0):
        """Block until the mixer is open (tests and scripts); the game never waits."""
        t0 = time.perf_counter()
        while not self.sounds_ready.is_set() and time.perf_counter() - t0 < timeout:
            self.adopt_sounds()
            time.sleep(0.01)
        self.adopt_sounds()
        return self.sounds.ok

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

    def set_drum_sounds(self, on):
        """The kit's own hit, guide and navigation sounds. Off when the module or a DAW
        (Bitwig with GetGood Drums through hhmapper) makes the drum sound; the metronome,
        backing and menu music stay."""
        self.sounds.drums = on
        self.settings["drum_sounds"] = on
        save_settings(self.settings)

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
        """Desktop fullscreen (macOS Spaces, the green button), never the exclusive mode that
        switches the monitor to the window's resolution and swallows trackpad gestures."""
        try:
            win = pygame.Window.from_display_module()
            if self.fullscreen:
                win.set_windowed()
                self.fullscreen = False
            else:
                win.set_fullscreen(desktop=True)
                self.fullscreen = True
        except (pygame.error, AttributeError) as e:
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
        """kit: {zone key: [note numbers]}. A number in two zones goes to the earlier zone."""
        self.kit = kit
        self.note_to_zone = {}
        for zk in reversed(C.ZONE_KEYS):
            for n in kit.get(zk, []):
                self.note_to_zone[n] = zk

    def zone_for(self, note):
        return self.note_to_zone.get(note)

    def instrument_for(self, note):
        zk = self.note_to_zone.get(note)
        return C.ZONE[zk].instrument if zk else None

    def has_drum(self, inst):
        return bool(self.midi_in) and bool(C.kit_notes(self.kit, inst))

    def load_songs(self):
        if self.songs is None:
            self.songs = []
            paths = list(self.args.midi or [])
            d = self.args.songs or SONGS_DIR
            paths += sorted(glob.glob(os.path.join(d, "*", "song.json")))          # ingested songs
            paths += sorted(glob.glob(os.path.join(d, "*.mid")) + glob.glob(os.path.join(d, "*.midi")))
            seen = set()
            for p in paths:
                if p in seen:
                    continue
                seen.add(p)
                try:
                    if p.endswith("song.json"):
                        folder = os.path.dirname(p)
                        if not os.path.exists(os.path.join(folder, "chart.mid")):
                            print(f"{folder}: no chart.mid yet, run: python -m drumhero.ingest {folder}")
                            continue
                        ch = load_song_folder(folder)
                    else:
                        ch = load_midi_chart(p, None if self.args.channel is None else self.args.channel - 1)
                except (SystemExit, OSError, ValueError) as e:
                    print(f"{p}: {e}")
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
        if chart.audio:
            key = ("music", chart.audio, round(chart.rate, 3))
            if key not in self.track_cache:
                try:
                    self.track_cache[key] = load_audio_track(chart.audio, -chart.audio_offset, rate=chart.rate)
                except (pygame.error, OSError) as e:
                    print(f"{chart.audio}: {e}")
                    self.track_cache[key] = None
            if self.track_cache[key] is not None:
                out["music"] = self.track_cache[key]
        return out

    # --- menu music ------------------------------------------------------------------
    def update_menu_music(self):
        """Ambient loop in every screen except play. Called on each screen change."""
        if not self.sounds.ok:
            return
        want = self.menu_music_on and not isinstance(self.screen_obj, PlayScreen)
        if want and self.menu_music is None:
            self.menu_music = menu_music_sound()
        ch = pygame.mixer.Channel(MENU_CHANNEL)
        if want:
            if not ch.get_busy():
                ch.play(self.menu_music, loops=-1, fade_ms=600)
        elif self.menu_music is not None and ch.get_busy():
            ch.fadeout(300)

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
        t_cb = time.perf_counter()
        if msg.type == "control_change":
            self.ghosts.control_change(msg.control, msg.value)
            if msg.control == 4:
                self.runlog.add("cc", control=msg.control, value=msg.value)
        elif msg.type == "note_on" and msg.velocity > 0:
            scr = self.screen_obj
            if scr is None:
                return
            why = self.ghosts.reason(msg.note, msg.velocity)
            if why is not None:
                scr.on_ghost(msg.note, msg.velocity, why)
                self.runlog.add("ghost", note=msg.note, velocity=msg.velocity, why=why)
                result = why
            else:
                self.runlog.add("note", note=msg.note, velocity=msg.velocity, cc=self.ghosts.pedal_cc)
                result = scr.on_note(msg.note, msg.velocity)
            if self.midi_trace is not None:
                # wall clock at the callback, note, velocity, outcome, microseconds spent judging
                self.midi_trace.write(f"{time.time():.6f} {msg.note} {msg.velocity} {result or '-'} "
                                      f"{(time.perf_counter() - t_cb) * 1e6:.0f}\n")
                self.midi_trace.flush()

    def nav_hit(self, note, velocity):
        """A drum hit used as a button. Debounced, sounded, queued for the main thread."""
        inst = self.instrument_for(note)
        if inst is None or velocity < NAV_MIN_VELOCITY:
            return
        if inst == "crash2":
            inst = "crash"                     # either crash is the "up" / Setup button
        now = time.perf_counter()
        if now - self.last_nav.get(inst, 0) < NAV_DEBOUNCE_S:
            return
        self.last_nav[inst] = now
        self.legend_flash[inst] = now
        self.sounds.play(inst, velocity, 0.8)
        self.drum_queue.append(inst)

    # --- screens ---------------------------------------------------------------
    # --- devices -------------------------------------------------------------------
    def handle_device_events(self):
        while not self.watcher.events.empty():
            kind, name, connected = self.watcher.events.get_nowait()
            self.on_device(kind, name, connected)

    def on_device(self, kind, name, connected):
        """A device appeared or went away: toast it and react."""
        ok, bad = JUDGE_COLORS["PERFECT"], JUDGE_COLORS["MISS"]
        if kind == "midi":
            if connected:
                if self.midi_in is None or self.midi_name != name:
                    try:
                        if self.midi_in is not None:
                            self.midi_in.close()
                        self.midi_in = mido.open_input(name, callback=self.on_midi)
                        self.midi_name = name
                    except (OSError, IOError) as e:
                        self.toasts.add(f"{name}: could not open ({e})", bad)
                        return
                self.toasts.add(f"{name} connected: drums are live", ok)
            else:
                if self.midi_in is not None:
                    try:
                        self.midi_in.close()
                    except Exception:                       # noqa: BLE001
                        pass
                self.midi_in, self.midi_name = None, None
                self.toasts.add(f"{name} disconnected: keyboard only", bad)
        elif kind == "midi-other":
            self.toasts.add(f"MIDI device connected: {name}", DIM)
        elif kind == "audio":
            if connected:
                self.toasts.add(f"audio output {name} connected", ok)
                if self.settings.get("audio_device") and (self.sounds.device or "") != name:
                    self.set_audio_device(self.settings["audio_device"])
            else:
                self.toasts.add(f"audio output {name} disconnected: sound falls back to the system default", bad)
                if self.sounds.device:
                    self.reopen_sounds(None)
        elif kind == "camera":
            self.camera_name = name if connected else None
            self.toasts.add(f"camera {name} {'available for takes' if connected else 'gone'}", ok if connected else DIM)

    def reopen_sounds(self, device):
        """Rebuild the mixer on `device` (None = system default) without touching the saved setting."""
        if self.menu_music is not None:
            self.menu_music.stop()
        if isinstance(self.screen_obj, PlayScreen):
            self.screen_obj.game.stop_tracks()
        self.sounds = SoundBank(enabled=not self.args.no_sound, device=device, drums=self.settings.get("drum_sounds", True))
        self.track_cache = {}
        self.menu_music = None
        if isinstance(self.screen_obj, PlayScreen):
            self.screen_obj.game.sounds = self.sounds
        self.update_menu_music()

    def draw_toasts(self):
        live = self.toasts.live()
        if not live:
            return
        S = self.scale
        f = self.fonts
        y = 108 * S                                              # under the titles and the hub strip
        for age, text, color in reversed(live):
            k = 1.0 if age < TOAST_S - 0.8 else max(0.0, (TOAST_S - age) / 0.8)
            slide = 0 if age > 0.25 else (1 - age / 0.25) * 30 * S
            ts = f.text(text, f.small, TEXT)
            w, h = ts.get_width() + 44 * S, ts.get_height() + 14 * S
            x = self.size[0] - w - 16 * S + slide
            box = pygame.Surface((int(w), int(h)), pygame.SRCALPHA)
            pygame.draw.rect(box, (*LANE_BG, int(235 * k)), box.get_rect(), border_radius=int(9 * S))
            pygame.draw.rect(box, (*color, int(255 * k)), box.get_rect(), max(1, int(2 * S)), border_radius=int(9 * S))
            pygame.draw.circle(box, (*color, int(255 * k)), (int(16 * S), int(h / 2)), int(5 * S))
            ts.set_alpha(int(255 * k))
            box.blit(ts, (30 * S, 7 * S))
            self.surface.blit(box, (x, y))
            y += h + 8 * S

    def device_line(self):
        """One line for the hub: the three devices with their state."""
        w = self.watcher.state
        midi = f"MIDI {self.midi_name}" if self.midi_in else "TD-17 not connected"
        audio = f"audio {self.sounds.device}" if self.sounds.device else ("audio system default" if self.sounds.ok else "no audio")
        cam = f"camera {w['camera']}" if w.get("camera") else "no camera"
        return midi, audio, cam

    def level_by_name(self, name):
        for cat in ("kick", "snare", "hihat"):
            for i, ch in enumerate(self.items_for(cat)):
                if ch.name == name:
                    return cat, i
        return None

    def level_names(self):
        return {ch.name for cat in ("kick", "snare", "hihat") for ch in self.items_for(cat)}

    def stats_summary(self):
        """Cached for a second: the hub draws it every frame."""
        now = time.perf_counter()
        if getattr(self, "_summary_at", 0) < now - 1.0:
            self._summary = ST.summary()
            self._summary_at = now
        return self._summary

    def coach_report(self):
        return ST.report({"exercises": self.items_for("kick"), "beats": self.items_for("snare"), "songs": self.items_for("hihat")})

    def start_session(self, playlist):
        items = []
        for it in playlist.get("items", []):
            found = self.level_by_name(it.get("level"))
            if found:
                items.append((found[0], found[1], float(it.get("rate", 1.0)), max(1, int(it.get("reps", 1))), it.get("why", "")))
        if not items:
            return False
        self.session = {"name": playlist.get("name", "session"), "items": items, "pos": 0, "rep": 1}
        self.session_go()
        return True

    def session_go(self):
        cat, index, rate, reps, why = self.session["items"][self.session["pos"]]
        self.rate = rate
        self.go(PlayScreen(self, cat, index))

    def session_advance(self):
        """Called when a level of a session ends and the player continues. True if it moved on."""
        ss = self.session
        if ss is None:
            return False
        cat, index, rate, reps, why = ss["items"][ss["pos"]]
        if ss["rep"] < reps:
            ss["rep"] += 1
        elif ss["pos"] + 1 < len(ss["items"]):
            ss["pos"] += 1; ss["rep"] = 1
        else:
            self.session = None
            self.rate = 1.0
            self.go(CoachScreen(self, done=ss["name"]))
            return True
        self.session_go()
        return True

    def toggle_recording(self):
        if self.recorder.active:
            path = self.recorder.stop()
            self.watcher.paused = False
            self.toasts.add(f"take stopped, rendering {os.path.basename(path)}", DIM)
            print(f"recording stopped, composing {path}")
        else:
            name = self.screen_obj.chart.name if isinstance(self.screen_obj, PlayScreen) else "take"
            self.watcher.paused = True                     # the camera is ffmpeg's now
            if self.recorder.start(self.size, name):
                self.toasts.add("recording" + (f" with camera {self.camera_name}" if self.camera_name else ", no camera"), (235, 70, 70))
                print("recording started")
            else:
                self.watcher.paused = False
                self.toasts.add(f"recording could not start: {self.recorder.error}", JUDGE_COLORS["MISS"])
                print(f"recording could not start: {self.recorder.error}")

    def draw_recording_status(self):
        status = self.recorder.status
        rec = self.recorder.active
        if not status and (self.editor.busy or self.editor.status):
            status = self.editor.status
            if self.editor.busy:
                status += " ." * (int(time.perf_counter()) % 4)
        if not status and self.coach.busy:
            status = self.coach.status + " ." * (int(time.perf_counter()) % 4)
        if not status:
            return
        S = self.scale
        f = self.fonts
        color = (235, 70, 70) if rec else (JUDGE_COLORS["MISS"] if (self.recorder.error or self.editor.error) and not (self.editor.busy or self.coach.busy) else DIM)
        ts = f.text(status, f.small, color)
        x = self.size[0] - ts.get_width() - 16 * S
        y = self.size[1] - ts.get_height() - 8 * S
        if rec and int(time.perf_counter() * 2) % 2 == 0:
            pygame.draw.circle(self.surface, color, (int(x - 12 * S), int(y + ts.get_height() / 2)), int(5 * S))
        self.surface.blit(ts, (x, y))

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
                    elif ev.key == pygame.K_v:
                        self.toggle_recording()
                    elif self.screen_obj.on_key(ev.key) is False:
                        running = False
            while self.drum_queue:
                if self.screen_obj.on_drum(self.drum_queue.popleft()) is False:
                    running = False
            self.handle_device_events()
            self.adopt_sounds()
            self.screen_obj.update()
            self.screen_obj.draw(self.surface, clock.get_fps())
            self.recorder.push(self.surface)              # a copy 30 times a second while recording
            self.draw_recording_status()
            self.draw_toasts()
            pygame.display.flip()
            clock.tick(TARGET_FPS)
        if self.midi_in:
            self.midi_in.close()
        self.watcher.stop()
        if self.recorder.active:
            self.recorder.stop()
        if self.recorder.composing is not None:
            print("finishing the take...")
            self.recorder.composing[0].join(timeout=300)
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

    def on_ghost(self, note, velocity, why):
        """MIDI thread. A note the ghost filter dropped; screens may show it."""
        pass

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
        elif key == pygame.K_s:
            self.app.go(StatsScreen(self.app))
        elif key == pygame.K_c:
            self.app.go(CoachScreen(self.app))
        return True

    def draw(self, surf, fps):
        surf.fill(BG)
        S = self.s
        self.f.center(surf, "drumhero", self.f.big, TEXT, 52 * S)
        sm = self.app.stats_summary()
        strip = (f"streak {sm['streak']} day{'s' if sm['streak'] != 1 else ''}  ·  today {sm['today_minutes']:.0f} min"
                 f"  ·  {sm['total_minutes']:.0f} min in {sm['days']} days  ·  S progress  ·  C coach")
        self.f.center(surf, strip, self.f.small, ACCENT if sm["streak"] else DIM, 92 * S)
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
                items = self.app.items_for(cat)
                got = sum(self.app.results.get(ch.name, {}).get("stars", 0) for ch in items)
                self.f.center(surf, f"{len(items)} levels  ·  ★ {got} / {5 * len(items)}", self.f.small, color, y + ph - 26 * S, x + pw / 2)
            elif cat == "hihat":
                n = len(self.app.songs) if self.app.songs is not None else None
                self.f.center(surf, f"{n} songs" if n is not None else "songs/ folder", self.f.small, color, y + ph - 26 * S, x + pw / 2)
            else:
                self.f.center(surf, describe(self.app.kit), self.f.small, color, y + ph - 26 * S, x + pw / 2)
            if not self.app.has_drum(cat) and self.app.midi_in:
                self.f.center(surf, "no pad assigned", self.f.small, JUDGE_COLORS["MISS"], y + 60 * S, x + pw / 2)
        midi, audio, cam = self.app.device_line()
        chips = [(midi, bool(self.app.midi_in)), (audio, bool(self.app.sounds.ok)), (cam, bool(self.app.watcher.state.get("camera")))]
        total = sum(self.f.text(t, self.f.small, DIM).get_width() + 34 * S for t, _ in chips)
        x = self.w / 2 - total / 2
        for text, on in chips:
            pygame.draw.circle(surf, JUDGE_COLORS["PERFECT"] if on else (80, 80, 90), (int(x + 6 * S), int(self.h - 44 * S)), int(5 * S))
            ts = self.f.text(text, self.f.small, TEXT if on else DIM)
            surf.blit(ts, (x + 18 * S, self.h - 44 * S - ts.get_height() / 2))
            x += ts.get_width() + 34 * S
        self.f.center(surf, "keys 1-4, arrows or hjkl + Enter   ·   F11 fullscreen   ·   Esc quit", self.f.small, DIM, self.h - 20 * S)


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
                    (f"Drum sounds: {'on' if self.app.sounds.drums else 'off'}", "off: the kit is silent here, the module or Bitwig makes the sound"),
                    (f"Guide sounds: {'on' if self.app.guide else 'off'}", "hear the chart as it crosses the line"),
                    (f"Backing loop: {'on' if self.app.backing_on else 'off'}", "bass, chords and arpeggio under the built-in levels"),
                    (f"Metronome: {self.app.metronome_mode}", "congas: full follows the subdivision, beats only marks the beats"),
                    (f"Menu music: {'on' if self.app.menu_music_on else 'off'}", "ambient texture outside the game"),
                    (f"Audio output: {self.app.sounds.device or 'system default'}", "select cycles through the outputs, saved"),
                    (f"Start fullscreen: {'on' if self.app.settings.get('fullscreen', True) else 'off'}", "F11 or Cmd+F toggles any time, saved"),
                    ("Recording (V)", f"audio {self.app.recorder.settings['capture_audio_device']} ch {self.app.recorder.settings['capture_audio_channels']}"
                                      f" · camera '{self.app.recorder.settings['capture_camera']}' · ~/Movies/drumhero"),
                    ("Camera & take check", "the iPhone next to the game picture, the PiP layout, the take's audio meter, a test take"),
                    ("Edit a take with Claude", "pick a take and a style; Claude Code cuts it with ffmpeg"),
                    ("Progress (S)", "streak, minutes, trends, records"),
                    ("Coach (C)", "Claude reads your stats: strengths, weaknesses, focus, playlists"),
                    ("Quit", "")]
        return [(ch.name, f"{ch.bpm:.0f} bpm · {len(ch.notes):3d} notes · {ch.desc}" + ("  ♪ audio" if ch.audio else "")) for ch in self.app.items_for(self.cat)]

    def fit(self, text, max_w):
        """text clipped with an ellipsis to max_w pixels in the small font."""
        if self.f.text(text, self.f.small, DIM).get_width() <= max_w:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.f.text(text[:mid] + "…", self.f.small, DIM).get_width() <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "…"

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
                self.app.set_drum_sounds(not self.app.sounds.drums)
            elif self.sel == 3:
                self.app.guide = not self.app.guide
            elif self.sel == 4:
                self.app.backing_on = not self.app.backing_on
            elif self.sel == 5:
                modes = ["full", "beats", "off"]
                self.app.metronome_mode = modes[(modes.index(self.app.metronome_mode) + 1) % 3]
            elif self.sel == 6:
                self.app.menu_music_on = not self.app.menu_music_on
                self.app.update_menu_music()
            elif self.sel == 7:
                self.app.cycle_audio_device()
            elif self.sel == 8:
                self.app.settings["fullscreen"] = not self.app.settings.get("fullscreen", True)
                save_settings(self.app.settings)
            elif self.sel == 9:
                self.app.toggle_recording()
            elif self.sel == 10:
                self.app.go(CameraCheckScreen(self.app, self.app.surface))
            elif self.sel == 11:
                self.app.go(EditScreen(self.app))
            elif self.sel == 12:
                self.app.go(StatsScreen(self.app))
            elif self.sel == 13:
                self.app.go(CoachScreen(self.app))
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
            best = self.app.results.get(name)
            right = self.w * 0.88
            if best:
                ts = self.f.text(f"{best['accuracy'] * 100:.0f}%", self.f.small, DIM)
                surf.blit(ts, (right - ts.get_width(), y + 4 * S))
                right -= ts.get_width() + 12 * S
                right -= draw_stars(surf, self.f, best.get("stars", 0), right, y + 2 * S, S, size="small") + 16 * S
            surf.blit(self.f.text(self.fit(sub, right - (x + 370 * S)), self.f.small, DIM), (x + 370 * S, y + 4 * S))
            y += row_h
        if items:
            self.f.center(surf, items[self.sel][1], self.f.small, TEXT, self.h - 84 * S)   # the selected one in full
        self.legend(surf, [("hihat", "down"), ("crash", "up"), ("snare", "select"), ("kick", "back")],
                    keys="arrows or j k · Enter or l · Esc or h")


def wrap(fonts, text, font, max_w):
    """Word-wrap text into lines that fit max_w pixels."""
    lines = []
    for para in str(text).split("\n"):
        words = para.split()
        cur = ""
        for w in words:
            trial = (cur + " " + w).strip()
            if fonts.text(trial, font, TEXT).get_width() <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur); cur = w
        lines.append(cur)
    return lines


def sparkline(surf, values, x, y, w, h, color, lo=None, hi=None, S=1.0):
    if len(values) < 2:
        return
    lo = min(values) if lo is None else lo
    hi = max(values) if hi is None else hi
    span = (hi - lo) or 1.0
    pts = [(x + i * w / (len(values) - 1), y + h - (v - lo) / span * h) for i, v in enumerate(values)]
    pygame.draw.lines(surf, color, False, pts, max(1, int(2 * S)))
    pygame.draw.circle(surf, color, (int(pts[-1][0]), int(pts[-1][1])), int(4 * S))


# ---------------------------------------------------------------------------
class StatsScreen(Screen):
    """Progress at a glance: streak, minutes, this week, trends, records, last runs."""

    def on_drum(self, inst):
        if NAV.get(inst) == "back":
            self.app.go(HubScreen(self.app))
        elif NAV.get(inst) == "accept":
            self.app.go(CoachScreen(self.app))
        return True

    def on_key(self, key):
        if key in (pygame.K_ESCAPE, pygame.K_h, pygame.K_s):
            self.app.go(HubScreen(self.app))
        elif key in (pygame.K_c, pygame.K_RETURN, pygame.K_l):
            self.app.go(CoachScreen(self.app))
        return True

    def draw(self, surf, fps):
        surf.fill(BG)
        S = self.s; f = self.f
        sm = self.app.stats_summary()
        self.f.center(surf, "Progress", f.large, TEXT, 44 * S)
        if not sm["runs"]:
            f.center(surf, "Nothing played yet. Every level you finish lands here.", f.mid, DIM, self.h * 0.42)
            self.legend(surf, [("snare", "coach"), ("kick", "back")], keys="C coach · Esc back")
            return
        # headline numbers
        cols = [(f"{sm['streak']}", "day streak" + (f" (best {sm['best_streak']})" if sm["best_streak"] > sm["streak"] else "")),
                (f"{sm['today_minutes']:.0f}", "minutes today"),
                (f"{sm['total_minutes']:.0f}", f"minutes in {sm['days']} days"),
                (f"{sm['total_notes']:,}", "notes hit"),
                (f"{sum(self.app.results.get(n, {}).get('stars', 0) for n in self.app.results)}", "stars")]
        cw = (self.w - 120 * S) / len(cols)
        for i, (big, label) in enumerate(cols):
            cx = 60 * S + (i + 0.5) * cw
            f.center(surf, big, f.big, ACCENT, 112 * S, cx)
            f.center(surf, label, f.small, DIM, 152 * S, cx)
        # this week
        x0, y0, bw, bh = 60 * S, 200 * S, (self.w * 0.42 - 60 * S), 130 * S
        surf.blit(f.text("this week", f.small, TEXT), (x0, y0 - 24 * S))
        top = max([m for _, m in sm["week"]] + [10.0])
        for i, (d, m) in enumerate(sm["week"]):
            bx = x0 + i * bw / 7
            hgt = m / top * bh
            col = ACCENT if d == sm["today"] else lerp(ACCENT, LANE_BG, 0.45)
            pygame.draw.rect(surf, LANE_BG, (bx + 6 * S, y0, bw / 7 - 12 * S, bh), border_radius=int(5 * S))
            if hgt > 0:
                pygame.draw.rect(surf, col, (bx + 6 * S, y0 + bh - hgt, bw / 7 - 12 * S, hgt), border_radius=int(5 * S))
            f.center(surf, time.strftime("%a", time.strptime(d, "%Y-%m-%d"))[:2], f.small, DIM, y0 + bh + 14 * S, bx + bw / 14)
            if m >= 1:
                f.center(surf, f"{m:.0f}", f.small, TEXT, y0 + bh - hgt - 12 * S, bx + bw / 14)
        gw, gp = sm["grade_week"], sm["grade_prev_week"]
        if gw is not None:
            delta = f"  ({gw - gp:+.0f} vs last week)" if gp is not None else ""
            surf.blit(f.text(f"mean grade this week {gw:.0f}{delta}", f.small, DIM), (x0, y0 + bh + 34 * S))
        # trends
        tx, tw = self.w * 0.5, self.w * 0.5 - 60 * S
        for j, (label, vals, col, lo, hi, fmt) in enumerate([
                ("accuracy, last runs", [v * 100 for v in sm["trend_accuracy"]], JUDGE_COLORS["PERFECT"], 0, 100, "{:.0f}%"),
                ("timing std ms, last runs (lower is tighter)", sm["trend_std_ms"], JUDGE_COLORS["GOOD"], 0, None, "{:.0f} ms"),
                ("grade, last runs", sm["trend_grade"], ACCENT, 0, 100, "{:.0f}")]):
            ty = y0 - 24 * S + j * 70 * S
            surf.blit(f.text(label, f.small, TEXT), (tx, ty))
            if vals:
                sparkline(surf, vals, tx, ty + 22 * S, tw - 70 * S, 34 * S, col, lo, hi, S)
                surf.blit(f.text(fmt.format(vals[-1]), f.small, col), (tx + tw - 60 * S, ty + 30 * S))
        # records and last runs
        ry = 410 * S
        recs = []
        if sm["best_run"]:
            b = sm["best_run"]; recs.append(f"best run  {b['chart']['name']}  grade {b['stats']['grade']:.0f}, {b['stats']['stars']} stars")
        if sm["tightest"]:
            t = sm["tightest"]; recs.append(f"tightest timing  {t['chart']['name']}  std {t['stats']['std_ms']:.1f} ms")
        if sm["longest_combo"] and sm["longest_combo"]["stats"].get("max_combo"):
            c = sm["longest_combo"]; recs.append(f"longest combo  {c['stats']['max_combo']} on {c['chart']['name']}")
        surf.blit(f.text("records", f.small, TEXT), (x0, ry))
        for i, r in enumerate(recs):
            surf.blit(f.text(r, f.small, DIM), (x0, ry + (22 + i * 20) * S))
        surf.blit(f.text("last runs", f.small, TEXT), (tx, ry))
        for i, r in enumerate(sm["last"][:6]):
            when = time.strftime("%a %H:%M", time.localtime(r["started"]))
            line = f"{when}  {r['chart']['name'][:22]:22}  {r['stats']['accuracy'] * 100:3.0f}%  {r['stats'].get('std_ms', 0):4.0f} ms"
            surf.blit(f.text(line, f.small, DIM), (tx, ry + (22 + i * 20) * S))
            draw_stars(surf, f, r["stats"].get("stars", 0), self.w - 60 * S, ry + (20 + i * 20) * S, S, size="small")
        self.legend(surf, [("snare", "coach"), ("kick", "back")], keys="C coach · Esc back")


# ---------------------------------------------------------------------------
class CoachScreen(Screen):
    """What Claude says about your playing, and the playlists it wrote as sessions."""

    def __init__(self, app, done=None):
        super().__init__(app)
        self.sel = 0
        self.done = done

    def playlists(self):
        r = self.app.coach.result
        return r.get("playlists", []) if r else []

    def on_drum(self, inst):
        action = NAV.get(inst)
        if action == "back":
            self.app.go(HubScreen(self.app))
        elif action == "next":
            self.sel = (self.sel + 1) % max(1, len(self.playlists()))
        elif action == "prev":
            self.sel = (self.sel - 1) % max(1, len(self.playlists()))
        elif action == "accept":
            self.accept()
        return True

    def on_key(self, key):
        if key in (pygame.K_ESCAPE, pygame.K_h):
            self.app.go(HubScreen(self.app))
        elif key in (pygame.K_DOWN, pygame.K_j):
            self.sel = (self.sel + 1) % max(1, len(self.playlists()))
        elif key in (pygame.K_UP, pygame.K_k):
            self.sel = (self.sel - 1) % max(1, len(self.playlists()))
        elif key in (pygame.K_RETURN, pygame.K_l):
            self.accept()
        elif key == pygame.K_a:
            self.ask()
        elif key == pygame.K_s:
            self.app.go(StatsScreen(self.app))
        return True

    def ask(self):
        if not self.app.coach.busy:
            self.app.coach.start(self.app.coach_report(), self.app.level_names())

    def accept(self):
        pls = self.playlists()
        if pls:
            self.app.start_session(pls[self.sel])
        else:
            self.ask()

    def draw(self, surf, fps):
        surf.fill(BG)
        S = self.s; f = self.f
        f.center(surf, "Coach", f.large, TEXT, 44 * S)
        r = self.app.coach.result
        if self.done:
            f.center(surf, f"session '{self.done}' done", f.mid, JUDGE_COLORS["PERFECT"], 84 * S)
        if self.app.coach.busy:
            f.center(surf, self.app.coach.status, f.mid, JUDGE_COLORS["GOOD"], self.h * 0.42)
            f.center(surf, "Claude is reading your run logs; this takes a minute or two", f.small, DIM, self.h * 0.42 + 36 * S)
        elif not r:
            f.center(surf, "No analysis yet.", f.mid, TEXT, self.h * 0.40)
            f.center(surf, "Snare or Enter: ask Claude for strengths, weaknesses, the focus for next session and playlists.",
                     f.small, DIM, self.h * 0.40 + 36 * S)
            if self.app.coach.error:
                f.center(surf, self.app.coach.error, f.small, JUDGE_COLORS["MISS"], self.h * 0.40 + 64 * S)
        else:
            x0, colw = 60 * S, self.w * 0.5 - 80 * S
            y = 84 * S if not self.done else 108 * S
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("generated", 0)))
            surf.blit(f.text(f"from {when}  ·  A ask again", f.small, DIM), (x0, y)); y += 26 * S
            surf.blit(f.text("focus next session", f.mid, ACCENT), (x0, y)); y += 30 * S
            for line in wrap(f, r.get("focus_next_session", ""), f.small, colw)[:5]:
                surf.blit(f.text(line, f.small, TEXT), (x0, y)); y += 20 * S
            y += 10 * S
            for title, key, col in (("strengths", "strengths", JUDGE_COLORS["PERFECT"]), ("weaknesses", "weaknesses", JUDGE_COLORS["OK"])):
                surf.blit(f.text(title, f.mid, col), (x0, y)); y += 30 * S
                for item in r.get(key, [])[:5]:
                    lines = wrap(f, "· " + item, f.small, colw)[:2]
                    for line in lines:
                        surf.blit(f.text(line, f.small, TEXT), (x0, y)); y += 20 * S
                y += 8 * S
            # right: diet and playlists
            rx = self.w * 0.5 + 20 * S
            y = 84 * S if not self.done else 108 * S
            surf.blit(f.text("diet", f.mid, ACCENT), (rx, y)); y += 30 * S
            for line in wrap(f, r.get("diet", ""), f.small, colw)[:7]:
                surf.blit(f.text(line, f.small, TEXT), (rx, y)); y += 20 * S
            y += 12 * S
            surf.blit(f.text("sessions  ·  snare or Enter to start", f.mid, ACCENT), (rx, y)); y += 32 * S
            for i, pl in enumerate(self.playlists()):
                selected = i == self.sel
                if selected:
                    pygame.draw.rect(surf, lerp(LANE_BG, ACCENT, 0.18), (rx - 12 * S, y - 6 * S, colw + 24 * S, 46 * S), border_radius=int(8 * S))
                n = len(pl.get("items", []))
                surf.blit(f.text(f"{pl.get('name', 'session')}  ·  {n} levels · {pl.get('minutes', '?')} min", f.small, ACCENT if selected else TEXT), (rx, y))
                surf.blit(f.text(wrap(f, pl.get("goal", ""), f.small, colw)[0] if pl.get("goal") else "", f.small, DIM), (rx, y + 20 * S))
                y += 50 * S
            if self.playlists():
                pl = self.playlists()[self.sel]
                items = ", ".join(f"{it['level']} @{it.get('rate', 1.0):.2g}x" + (f" x{it['reps']}" if it.get('reps', 1) > 1 else "") for it in pl["items"])
                for line in wrap(f, items, f.small, colw)[:3]:
                    surf.blit(f.text(line, f.small, DIM), (rx, y)); y += 20 * S
        self.legend(surf, [("hihat", "down"), ("crash", "up"), ("snare", "start session" if r else "ask Claude"), ("kick", "back")],
                    keys="A ask Claude · S progress · Esc back")


# ---------------------------------------------------------------------------
class CameraCheckScreen(Screen):
    """Soundcheck for takes: the game picture next to the camera, the composed preview with
    the picture-in-picture, the take's audio channels metered, and a 3-second test take."""

    def __init__(self, app, sample=None):
        super().__init__(app)
        self.sample = sample.copy() if sample is not None else None   # what the take captures
        self.settings = {**CP.DEFAULTS, **{k: v for k, v in app.settings.items() if k in CP.DEFAULTS}}
        self.camera = CP.find_camera(self.settings["capture_camera"])
        self.preview = CP.CameraPreview(self.camera) if self.camera and CP.ffmpeg_path() else None
        try:
            self.meter = CP.AudioMeter(app.settings)
        except Exception as e:                                   # noqa: BLE001
            self.meter = None
            self.meter_error = str(e)
        else:
            self.meter_error = self.meter.error
        self.test_at = None
        self.test_result = None

    def close(self):
        if self.preview:
            self.preview.stop()
        if self.meter:
            self.meter.stop()
        self.preview = self.meter = None

    def leave(self):
        self.close()
        self.app.go(ListScreen(self.app, "crash", 10))

    def on_drum(self, inst):
        action = NAV.get(inst)
        if action == "back":
            self.leave()
        elif action == "next":
            self.cycle_pip(1)
        elif action == "prev":
            self.cycle_pip(-1)
        elif action == "accept":
            self.test_take()
        return True

    def on_key(self, key):
        if key in (pygame.K_ESCAPE, pygame.K_h):
            self.leave()
        elif key in (pygame.K_DOWN, pygame.K_j):
            self.cycle_pip(1)
        elif key in (pygame.K_UP, pygame.K_k):
            self.cycle_pip(-1)
        elif key in (pygame.K_LEFTBRACKET, pygame.K_RIGHTBRACKET, pygame.K_LEFT, pygame.K_RIGHT):
            corners = CP.CORNERS
            i = corners.index(self.settings["capture_corner"]) if self.settings["capture_corner"] in corners else 0
            self.settings["capture_corner"] = corners[(i + (1 if key in (pygame.K_RIGHTBRACKET, pygame.K_RIGHT) else -1)) % 4]
            self.save()
        elif key in (pygame.K_RETURN, pygame.K_l):
            self.test_take()
        elif key == pygame.K_r:
            self.close()
            self.__init__(self.app, self.sample)
        return True

    def cycle_pip(self, d):
        sizes = [0.2, 0.28, 0.36, 0.45]
        cur = min(range(4), key=lambda i: abs(sizes[i] - self.settings["capture_pip"]))
        self.settings["capture_pip"] = sizes[(cur + d) % 4]
        self.save()

    def save(self):
        self.app.settings.update({k: self.settings[k] for k in ("capture_pip", "capture_corner")})
        save_settings(self.app.settings)
        self.app.recorder.settings.update(self.settings)

    def test_take(self):
        if self.app.recorder.active or self.app.recorder.composing:
            return
        self.close()                                             # the camera and the device go to the recorder
        self.app.recorder.settings.update(self.settings)
        if self.app.recorder.start(self.app.size, "camera check"):
            self.test_at = time.perf_counter()
            self.test_result = None
        else:
            self.test_result = f"could not start: {self.app.recorder.error}"

    def update(self):
        # no camera yet: look again every few seconds (Continuity Camera comes and goes with the phone)
        if self.camera is None and self.test_at is None and time.perf_counter() - getattr(self, "_scan_at", 0) > 5.0:
            self._scan_at = time.perf_counter()
            cam = CP.find_camera(self.settings["capture_camera"])
            if cam:
                self.camera = cam
                self.preview = CP.CameraPreview(cam) if CP.ffmpeg_path() else None
                print(f"camera check: {cam[1]} appeared")
        if self.preview is not None and self.preview.error and not getattr(self, "_logged_error", False):
            self._logged_error = True
            print(f"camera check: preview error: {self.preview.error}")
        if self.test_at is not None and self.app.recorder.active and time.perf_counter() - self.test_at > 3.0:
            path = self.app.recorder.stop()
            self.test_result = f"test take: {os.path.basename(path)}"
        if self.test_at is not None and not self.app.recorder.active and self.app.recorder.composing is None and self.test_result and self.preview is None:
            self.test_result = (f"saved {self.test_result[11:]}" if not self.app.recorder.error else self.app.recorder.error)
            self.test_at = None
            self.preview = CP.CameraPreview(self.camera) if self.camera and CP.ffmpeg_path() else None   # back to live
            try:
                self.meter = CP.AudioMeter(self.app.settings)
            except Exception:                                  # noqa: BLE001
                self.meter = None

    def draw(self, surf, fps):
        surf.fill(BG)
        S = self.s; f = self.f
        f.center(surf, "Camera & take check", f.large, TEXT, 44 * S)
        avail = self.h - 210 * S                                   # between the title and the legend
        ph = int(avail * 0.42); pw = int(ph * 16 / 9)
        gap = int(40 * S)
        lx = int(self.w / 2 - pw - gap / 2); rx = int(self.w / 2 + gap / 2); y = int(96 * S)
        # game picture
        surf.blit(f.text("game picture (what the take records)", f.small, TEXT), (lx, y - 22 * S))
        if self.sample is not None:
            surf.blit(pygame.transform.smoothscale(self.sample, (pw, ph)), (lx, y))
        pygame.draw.rect(surf, (60, 60, 70), (lx, y, pw, ph), 1)
        # camera
        surf.blit(f.text(f"camera: {self.camera[1] if self.camera else 'none found'}", f.small, TEXT if self.camera else JUDGE_COLORS["MISS"]), (rx, y - 22 * S))
        cam = self.preview.surface() if self.preview else None
        if cam is not None:
            surf.blit(pygame.transform.smoothscale(cam, (pw, ph)), (rx, y))
        else:
            pygame.draw.rect(surf, LANE_BG, (rx, y, pw, ph))
            msg = (self.preview.error or "waiting for frames... (allow camera access if macOS asks)") if self.preview else \
                  (f"no video device matching '{self.settings['capture_camera']}'. Looking every 5 s. iPhone: same Apple ID, "
                   f"Wi-Fi and Bluetooth on, near the Mac, locked and still in landscape (or open Camo).")
            for i, line in enumerate(wrap(f, msg, f.small, pw - 20 * S)[:4]):
                f.center(surf, line, f.small, DIM, y + ph / 2 - 20 * S + i * 20 * S, rx + pw / 2)
        pygame.draw.rect(surf, (60, 60, 70), (rx, y, pw, ph), 1)
        # composed preview
        cy = int(y + ph + 46 * S)
        f.center(surf, f"composed take  ·  camera {self.settings['capture_pip']:.0%} high, corner {self.settings['capture_corner']}  ·  hats/crash size, [ ] corner",
                 f.small, TEXT, cy - 18 * S)
        ch = int(avail * 0.44); cw = int(ch * 16 / 9)
        cx = int(self.w / 2 - cw / 2 - 120 * S)
        if self.sample is not None:
            surf.blit(pygame.transform.smoothscale(self.sample, (cw, ch)), (cx, cy))
        else:
            pygame.draw.rect(surf, LANE_BG, (cx, cy, cw, ch))
        pip_h = int(ch * self.settings["capture_pip"]); pip_w = int(pip_h * 16 / 9); m = int(24 * cw / 1920)
        corner = self.settings["capture_corner"]
        px = cx + (cw - pip_w - m if corner in ("br", "tr") else m)
        py = cy + (ch - pip_h - m if corner in ("br", "bl") else m)
        if cam is not None:
            surf.blit(pygame.transform.smoothscale(cam, (pip_w, pip_h)), (px, py))
        else:
            pygame.draw.rect(surf, lerp(LANE_BG, ACCENT, 0.3), (px, py, pip_w, pip_h))
        pygame.draw.rect(surf, ACCENT, (px, py, pip_w, pip_h), 2)
        pygame.draw.rect(surf, (60, 60, 70), (cx, cy, cw, ch), 1)
        # audio meter, to the right of the composed preview
        mx = cx + cw + 36 * S
        surf.blit(f.text(f"take audio: {self.settings['capture_audio_device']} ch {self.settings['capture_audio_channels']}", f.small, TEXT), (mx, cy))
        levels = self.meter.levels if self.meter else None
        if levels:
            for i, db in enumerate(levels):
                bar_w = 200 * S
                k = max(0.0, min(1.0, (db + 60) / 60))
                pygame.draw.rect(surf, LANE_BG, (mx, cy + (30 + i * 26) * S, bar_w, 14 * S), border_radius=int(4 * S))
                pygame.draw.rect(surf, JUDGE_COLORS["PERFECT"] if db > -40 else JUDGE_COLORS["OK"], (mx, cy + (30 + i * 26) * S, bar_w * k, 14 * S), border_radius=int(4 * S))
                surf.blit(f.text(f"{db:5.0f} dB", f.small, DIM), (mx + bar_w + 10 * S, cy + (27 + i * 26) * S))
            if max(levels) < -70:
                for i, line in enumerate(wrap(f, "silent: on the XR18 set USB sends 17/18 to Main L/R (X-AIR Edit, Setup, Audio/MIDI)", f.small, 300 * S)[:3]):
                    surf.blit(f.text(line, f.small, JUDGE_COLORS["OK"]), (mx, cy + (90 + i * 18) * S))
        else:
            surf.blit(f.text(self.meter_error or "no signal yet", f.small, JUDGE_COLORS["MISS"] if self.meter_error else DIM), (mx, cy + 30 * S))
        # test take state
        if self.app.recorder.active and self.test_at is not None:
            f.center(surf, f"test take recording {3 - int(time.perf_counter() - self.test_at)}...", f.mid, (235, 70, 70), self.h - 92 * S)
        elif self.app.recorder.composing is not None and self.test_at is not None:
            f.center(surf, "rendering the test take...", f.mid, JUDGE_COLORS["GOOD"], self.h - 92 * S)
        elif self.test_result:
            f.center(surf, self.test_result, f.mid, JUDGE_COLORS["PERFECT"] if "saved" in self.test_result else JUDGE_COLORS["MISS"], self.h - 92 * S)
        self.legend(surf, [("hihat", "size"), ("crash", "size"), ("snare", "test take 3 s"), ("kick", "back")],
                    keys="[ ] corner · R rescan · Enter test take · Esc back · results in ~/Movies/drumhero")


# ---------------------------------------------------------------------------
class EditScreen(Screen):
    """Pick a take (newest first) and a style, and hand it to Claude Code. Hi-hat / crash
    move through the styles, snare starts, kick goes back; [ and ] change the take."""

    def __init__(self, app):
        super().__init__(app)
        self.takes = E.takes()
        self.take_i = 0
        self.sel = 0

    def on_drum(self, inst):
        action = NAV.get(inst)
        if action == "next":
            self.sel = (self.sel + 1) % len(E.STYLES)
        elif action == "prev":
            self.sel = (self.sel - 1) % len(E.STYLES)
        elif action == "accept":
            self.accept()
        elif action == "back":
            self.app.go(ListScreen(self.app, "crash", 11))
        return True

    def on_key(self, key):
        if key in (pygame.K_ESCAPE, pygame.K_h):
            self.app.go(ListScreen(self.app, "crash", 11))
        elif key in (pygame.K_DOWN, pygame.K_j):
            self.sel = (self.sel + 1) % len(E.STYLES)
        elif key in (pygame.K_UP, pygame.K_k):
            self.sel = (self.sel - 1) % len(E.STYLES)
        elif key in (pygame.K_LEFTBRACKET, pygame.K_LEFT) and self.takes:
            self.take_i = (self.take_i + 1) % len(self.takes)
        elif key in (pygame.K_RIGHTBRACKET, pygame.K_RIGHT) and self.takes:
            self.take_i = (self.take_i - 1) % len(self.takes)
        elif key in (pygame.K_RETURN, pygame.K_l):
            self.accept()
        return True

    def accept(self):
        if not self.takes or self.app.editor.busy:
            return
        if self.app.editor.start(self.takes[self.take_i], E.STYLES[self.sel][0]):
            self.app.go(HubScreen(self.app, 3))

    def draw(self, surf, fps):
        surf.fill(BG)
        S = self.s
        self.f.center(surf, "Edit with Claude", self.f.large, TEXT, 60 * S)
        if not self.takes:
            self.f.center(surf, "No takes yet. Press V during play to record one.", self.f.mid, DIM, self.h * 0.42)
        else:
            take = self.takes[self.take_i]
            meta = E.sidecar_for(take) or {}
            levels = ", ".join(l["chart"] for l in meta.get("run_logs", [])) or "no levels logged"
            self.f.center(surf, os.path.basename(take)[:-4], self.f.mid, ACCENT, 110 * S)
            self.f.center(surf, f"{meta.get('duration', 0):.0f} s · {levels}" + ("  · camera" if meta.get("camera") else ""),
                          self.f.small, DIM, 138 * S)
            self.f.center(surf, f"take {self.take_i + 1} of {len(self.takes)}  ·  [ ] or ← → to change", self.f.small, DIM, 160 * S)
        y = 210 * S
        for i, (key, label, blurb, _) in enumerate(E.STYLES):
            selected = i == self.sel
            x = self.w * 0.18
            if selected:
                pygame.draw.rect(surf, lerp(LANE_BG, ACCENT, 0.18), (x - 20 * S, y - 8 * S, self.w * 0.64 + 40 * S, 52 * S), border_radius=int(10 * S))
                pygame.draw.rect(surf, ACCENT, (x - 20 * S, y - 8 * S, 6 * S, 52 * S), border_radius=int(3 * S))
            surf.blit(self.f.text(label, self.f.mid, ACCENT if selected else TEXT), (x, y))
            surf.blit(self.f.text(blurb, self.f.small, DIM), (x + 300 * S, y + 6 * S))
            y += 60 * S
        if self.app.editor.busy:
            self.f.center(surf, self.app.editor.status, self.f.small, JUDGE_COLORS["GOOD"], self.h - 84 * S)
        elif self.app.editor.error:
            self.f.center(surf, self.app.editor.error, self.f.small, JUDGE_COLORS["MISS"], self.h - 84 * S)
        self.legend(surf, [("hihat", "down"), ("crash", "up"), ("snare", "edit"), ("kick", "back")],
                    keys="arrows or j k · Enter or l · Esc or h · edits land in ~/Movies/drumhero/edits")


# ---------------------------------------------------------------------------
class SetupScreen(Screen):
    """Onboarding wizard: one step per zone of the kit (snare head, snare rim, ride bell...).
    Every distinct note number heard during a step is assigned to that zone."""

    def __init__(self, app, first_run=False):
        super().__init__(app)
        self.first_run = first_run
        self.lock = threading.Lock()
        self.step = 0
        self.captured = {k: [] for k in C.ZONE_KEYS}
        self.capture_start = None
        self.flash = None              # (wall_t, note, velocity)
        self.done_at = None

    @property
    def key(self):
        return C.ZONE_KEYS[self.step] if self.step < len(C.ZONE_KEYS) else None

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
        self.app.sounds.play(C.ZONE[key].instrument, velocity)

    def advance(self):
        self.capture_start = None
        self.step += 1
        if self.step >= len(C.ZONE_KEYS):
            self.finish()

    def finish(self):
        kit = {}
        taken = set()
        for k in reversed(C.ZONE_KEYS):        # a number heard for two zones goes to the later one
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
        self.f.center(surf, "Set up your kit", self.f.large, TEXT, 44 * S)
        if not self.app.midi_in:
            self.f.center(surf, "No MIDI input connected. Start with --port NAME, or press Esc and play with keys 1-4.",
                          self.f.small, JUDGE_COLORS["MISS"], self.h - 76 * S)
            self.f.center(surf, "Any number key here stands in for the pad and assigns its factory note number.",
                          self.f.small, DIM, self.h - 54 * S)

        # left: every zone with its state
        x0, y0, row = 40 * S, 92 * S, 31 * S
        last_pad = None
        y = y0
        for i, zk in enumerate(C.ZONE_KEYS):
            z = C.ZONE[zk]
            if z.pad != last_pad:
                if last_pad is not None:
                    y += 6 * S
                last_pad = z.pad
            color = C.COLORS[z.instrument]
            got = captured[zk]
            if done_at is not None or i < step:
                dot, tcol = (color if got else DIM), (TEXT if got else DIM)
                note_txt = "/".join(map(str, got)) if got else "skipped"
            elif i == step:
                dot, tcol = ACCENT, ACCENT
                note_txt = "/".join(map(str, got)) if got else "..."
            else:
                dot, tcol, note_txt = DIM, DIM, ""
            cy = y + row / 2
            if i == step and done_at is None:
                pygame.draw.rect(surf, LANE_BG, (x0 - 14 * S, y, 360 * S, row), border_radius=int(8 * S))
            pygame.draw.circle(surf, dot, (int(x0), int(cy)), int(6 * S), 0 if (got or i == step) else 2)
            surf.blit(self.f.text(z.label, self.f.small, tcol), (x0 + 16 * S, cy - 10 * S))
            surf.blit(self.f.text(note_txt, self.f.small, DIM if i != step else ACCENT), (x0 + 200 * S, cy - 10 * S))
            y += row

        if done_at is not None:
            self.f.center(surf, "Kit saved", self.f.big, JUDGE_COLORS["PERFECT"], self.h * 0.42, self.w * 0.65)
            self.f.center(surf, describe(self.app.kit), self.f.small, DIM, self.h * 0.42 + 60 * S, self.w * 0.65)
            return

        z = C.ZONE[key]
        color = C.COLORS[z.instrument]
        cx, cy = self.w * 0.65, self.h * 0.47
        r = 96 * S
        k = 0.0
        if flash and now - flash[0] < 0.25:
            k = 1 - (now - flash[0]) / 0.25
            pygame.draw.circle(surf, lerp(LANE_BG, color, k), (int(cx), int(cy)), int(r + 40 * S * (1 - k)))
        pygame.draw.circle(surf, color, (int(cx), int(cy)), int(r), max(2, int(4 * S)))
        self.f.center(surf, z.pad.upper(), self.f.large, lerp(color, BG, k), cy - 16 * S, cx)   # dark on the flash
        self.f.center(surf, z.part.upper() if z.part else "", self.f.mid, lerp(TEXT, BG, k), cy + 24 * S, cx)
        self.f.center(surf, f"step {step + 1} of {len(C.ZONE_KEYS)}", self.f.small, DIM, cy - r - 62 * S, cx)
        self.f.center(surf, z.prompt, self.f.mid, TEXT, cy - r - 34 * S, cx)

        if captured[key]:
            fam = [n for n in C.expand_family(captured[key]) if n not in captured[key]]
            got = "got note " + ", ".join(map(str, captured[key])) + (f"  (+ {', '.join(map(str, fam))} same zone)" if fam else "")
            self.f.center(surf, got, self.f.mid, JUDGE_COLORS["PERFECT"], cy + r + 36 * S, cx)
            if flash:
                self.f.center(surf, f"last: note {flash[1]} · velocity {flash[2]}", self.f.small, DIM, cy + r + 66 * S, cx)
            if capture_start is not None:
                frac = max(0.0, 1 - (now - capture_start) / CAPTURE_S)
                bw = 300 * S
                pygame.draw.rect(surf, LANE_BG, (cx - bw / 2, cy + r + 91 * S, bw, 6 * S))
                pygame.draw.rect(surf, color, (cx - bw / 2, cy + r + 91 * S, bw * frac, 6 * S))
                self.f.center(surf, "keep hitting, moving on...", self.f.small, DIM, cy + r + 114 * S, cx)
        else:
            self.f.center(surf, "waiting...", self.f.mid, DIM, cy + r + 36 * S, cx)
        self.f.center(surf, "Enter next · S skip a zone you don't have · Backspace redo previous · Esc cancel",
                      self.f.small, DIM, self.h - 28 * S)


# ---------------------------------------------------------------------------
class SoundcheckScreen(Screen):
    """Hit every zone: its row lights up, plays the pad's sound and shows the note number
    and velocity. Once every assigned zone has been heard, the snare continues and the
    kick redoes the wizard."""

    def __init__(self, app, first_run=False):
        super().__init__(app)
        self.first_run = first_run
        self.lock = threading.Lock()
        self.heard = {k: None for k in C.ZONE_KEYS}       # zone -> (wall_t, note, velocity)
        self.unknown = None                                # (wall_t, note, velocity) for unassigned pads
        self.ghost = None                                  # (wall_t, note, velocity, why) for dropped notes

    def on_ghost(self, note, velocity, why):
        with self.lock:
            self.ghost = (time.perf_counter(), note, velocity, why)

    def needed(self):
        return [k for k in C.ZONE_KEYS if self.app.kit.get(k)]

    def all_heard(self):
        return all(self.heard[k] for k in self.needed())

    def on_note(self, note, velocity):
        zk = self.app.zone_for(note)
        now = time.perf_counter()
        with self.lock:
            if zk is None:
                self.unknown = (now, note, velocity)
                return
            inst = C.ZONE[zk].instrument
            navigate = self.all_heard() and NAV.get(inst) in ("accept", "back")
            self.heard[zk] = (now, note, velocity)
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
        elif key in KEY_LANES and KEY_LANES[key] < len(C.INSTRUMENTS):
            inst = C.INSTRUMENTS[KEY_LANES[key]]
            notes = C.kit_notes(self.app.kit, inst) or C.DEFAULT_KIT[C.INSTRUMENT_ZONES[inst][0]]
            self.on_note(notes[0], 100)
        return True

    def done(self):
        self.app.go(HubScreen(self.app, 0 if self.first_run else 3))

    def draw(self, surf, fps):
        surf.fill(BG)
        with self.lock:
            heard = dict(self.heard)
            unknown = self.unknown
            ghost = self.ghost
        now = time.perf_counter()
        S = self.s
        kit = self.app.kit
        ready = self.all_heard()
        self.f.center(surf, "Soundcheck", self.f.large, TEXT, 44 * S)
        self.f.center(surf, "hit every zone: it should light up and sound like it", self.f.small, DIM, 82 * S)

        cols, side, gap = 4, 40 * S, 14 * S
        cw = (self.w - 2 * side - (cols - 1) * gap) / cols
        row, head = 24 * S, 34 * S
        top = 104 * S
        card_h = head + 3 * row + 10 * S
        for i, (pad, zones) in enumerate(C.PADS):
            col, r_ = i % cols, i // cols
            x, y = side + col * (cw + gap), top + r_ * (card_h + gap)
            color = C.COLORS[C.ZONE[zones[0]].instrument]
            assigned = any(kit.get(zk) for zk in zones)
            hot = max((max(0.0, 1 - (now - heard[zk][0]) / 0.3) for zk in zones if heard[zk]), default=0.0)
            rect = pygame.Rect(int(x), int(y), int(cw), int(card_h))
            pygame.draw.rect(surf, lerp(lerp(LANE_BG, color, 0.10 if assigned else 0.0), color, 0.35 * hot), rect, border_radius=int(12 * S))
            pygame.draw.rect(surf, color if assigned else (60, 60, 70), rect, max(1, int(2 * S)), border_radius=int(12 * S))
            pygame.draw.circle(surf, color if assigned else (60, 60, 70), (int(x + 18 * S), int(y + head / 2)), int(7 * S))
            surf.blit(self.f.text(pad, self.f.mid, TEXT if assigned else DIM), (x + 32 * S, y + head / 2 - 13 * S))
            if all(heard[zk] for zk in zones if kit.get(zk)) and assigned:
                surf.blit(self.f.text("✓", self.f.mid, JUDGE_COLORS["PERFECT"]), (x + cw - 30 * S, y + head / 2 - 13 * S))
            for j, zk in enumerate(zones):
                z = C.ZONE[zk]
                ry = y + head + j * row
                notes = kit.get(zk, [])
                h = heard[zk]
                k = max(0.0, 1 - (now - h[0]) / 0.3) if h else 0.0
                if k > 0:
                    pygame.draw.rect(surf, lerp(LANE_BG, color, 0.6 * k), (x + 6 * S, ry, cw - 12 * S, row), border_radius=int(6 * S))
                label = f"{z.part or 'pad':<6}{'/'.join(map(str, notes)) if notes else '-'}"
                surf.blit(self.f.text(label, self.f.small, TEXT if notes else DIM), (x + 14 * S, ry + 3 * S))
                if h:
                    right = f"✓ {h[1]} v{h[2]}" if now - h[0] < 2.0 else "✓"
                    ts = self.f.text(right, self.f.small, color)
                else:
                    ts = self.f.text("waiting" if notes else "", self.f.small, DIM)
                surf.blit(ts, (x + cw - 14 * S - ts.get_width(), ry + 3 * S))

        bottom = top + 2 * card_h + gap
        if C.kit_notes(kit, "hihat"):
            draw_hihat_state(surf, self.f, self.app.ghosts, self.w / 2, bottom + 52 * S, S)

        my = self.h * 0.84
        if unknown and now - unknown[0] < 2.5:
            self.f.center(surf, f"note {unknown[1]} is not assigned to any zone (vel {unknown[2]})",
                          self.f.mid, JUDGE_COLORS["MISS"], my - 30 * S)
            self.f.center(surf, "if that pad should count, redo the setup and hit it during its zone", self.f.small, DIM, my)
        elif ghost and now - ghost[0] < 1.5:
            self.f.center(surf, f"ignored note {ghost[1]} vel {ghost[2]}: {ghost[3]}", self.f.small, DIM, my)
        elif ready:
            self.f.center(surf, "All zones heard.", self.f.mid, JUDGE_COLORS["PERFECT"], my - 30 * S)

        if ready:
            self.legend(surf, [("snare", "continue"), ("kick", "redo setup")], keys="Enter continue · Backspace redo setup")
        else:
            self.f.center(surf, "Enter skip · Backspace redo setup · keys 1-7 stand in for the pads", self.f.small, DIM, self.h - 28 * S)


# ---------------------------------------------------------------------------
class PlayScreen(Screen):
    def __init__(self, app, cat, index):
        super().__init__(app)
        self.cat, self.index = cat, index
        self.chart = app.items_for(cat)[index].at_rate(app.rate)
        self.lanes, self.by_note = build_lanes(self.chart, app.kit)
        # scroll speed follows the tempo so a beat is always the same distance on screen
        self.game = Game(self.chart, self.lanes, self.by_note, offset_ms=app.offset_ms, speed=app.rate,
                         sounds=app.sounds, guide=app.guide and not self.chart.audio,   # the record has its own drums
                         log=app.runlog)
        self.game.metronome_mode = app.metronome_mode
        app.runlog.start(self.chart, self.lanes, app.kit, app.settings, {
            "offset_ms": app.offset_ms, "guide": self.game.guide, "metronome": app.metronome_mode,
            "backing": app.backing_on, "drum_sounds": app.sounds.drums, "dyn_thresholds": self.game.dyn_thresholds(),
            "ghost_filter": {"hihat_min_velocity": GH.HIHAT_MIN_VELOCITY, "chick_splash_ms": GH.CHICK_SPLASH_MS,
                             "pedal_motion_cc": GH.PEDAL_MOTION_CC, "pedal_motion_ms": GH.PEDAL_MOTION_MS,
                             "zone_crosstalk": GH.ZONE_CROSSTALK, "any_min_velocity": GH.ANY_MIN_VELOCITY},
        })
        prog = None if cat == "hihat" else index + (0 if cat == "kick" else 2)   # songs bring their own music
        for name, track in app.tracks_for(self.chart, prog).items():
            self.game.set_track(name, track, enabled=(app.backing_on if name == "backing" else True))
        self.renderer = Renderer(self.game, app.size, app.fonts, app.ghosts)
        self.recorded = False
        self.finished_at = None
        self.game.reset()

    def on_resize(self):
        self.renderer = Renderer(self.game, self.app.size, self.app.fonts, self.app.ghosts)
        self.renderer.finished_at = self.finished_at

    def nav_ready(self):
        return self.finished_at is not None and time.perf_counter() - self.finished_at > RESULTS_GRACE_S

    def on_note(self, note, velocity):        # MIDI thread: judge immediately while playing
        if self.nav_ready():
            self.app.nav_hit(note, velocity)
            return "nav"
        j = self.game.hit(note, velocity, art=self.app.ghosts.articulation_for(note))
        return f"{j}" if j else "unmapped"

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
                self.app.runlog.add("pause", paused=g.paused)
        elif key == pygame.K_r:
            self.retry()
        elif key == pygame.K_RETURN and g.finished:
            self.next_level()
        elif key in (pygame.K_LEFTBRACKET, pygame.K_RIGHTBRACKET):
            # tempo: everything (notes, metronome, backing, the record) slows down or speeds up;
            # the level restarts at the new tempo
            step = 0.1 if key == pygame.K_RIGHTBRACKET else -0.1
            self.app.rate = round(min(2.0, max(0.3, self.app.rate + step)), 2)
            self.retry()
        elif key in (pygame.K_COMMA, pygame.K_PERIOD):
            g.offset_ms = self.app.offset_ms = g.offset_ms + (5 if key == pygame.K_PERIOD else -5)
            self.app.settings["offset_ms"] = self.app.offset_ms      # remembered across runs
            self.app.runlog.add("offset", offset_ms=self.app.offset_ms)
            save_settings(self.app.settings)
        elif key == pygame.K_g:
            g.guide = self.app.guide = not g.guide
        elif key == pygame.K_d:
            self.app.set_drum_sounds(not self.app.sounds.drums)
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
        if self.app.session is not None:
            self.app.session = None
            self.app.rate = 1.0
            self.app.go(CoachScreen(self.app))
            return
        self.app.go(ListScreen(self.app, self.cat, self.index))

    def retry(self):
        self.leave()
        self.app.go(PlayScreen(self.app, self.cat, self.index))

    def next_level(self):
        self.leave()
        if self.app.session_advance():
            return
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
        if self.game.hits:
            self.app.runlog.write(self.game.stats())     # once, off the hot path
        else:
            self.app.runlog.header = None

    def record(self):
        if self.recorded or not self.game.hits:
            return
        st = self.game.stats()
        best = self.app.results.get(self.chart.name)
        if best is None or st["grade"] > best.get("grade", -1):
            keep = {k: st[k] for k in ("stars", "grade", "accuracy", "mean_ms", "std_ms", "hit", "notes")}
            keep["when"] = time.time()
            self.app.results[self.chart.name] = keep
            save_progress(self.app.results)
        self.recorded = True

    def update(self):
        self.game.update()
        if self.game.finished:
            self.record()
            if self.finished_at is None:
                self.finished_at = time.perf_counter()
                self.renderer.finished_at = self.finished_at
                self.game.stop_tracks()                       # the jingle takes over from the backing
                self.app.sounds.play_jingle(self.game.stats()["stars"])

    def draw(self, surf, fps):
        self.renderer.draw(surf, fps)
        ss = self.app.session
        if ss is not None:
            cat, index, rate, reps, why = ss["items"][ss["pos"]]
            S = self.s
            label = f"session {ss['name']}  ·  {ss['pos'] + 1}/{len(ss['items'])}" + (f"  ·  rep {ss['rep']}/{reps}" if reps > 1 else "")
            if why and not self.game.finished:
                label += f"  ·  {why}"
            self.f.center(surf, label, self.f.small, ACCENT, self.h - (64 if self.game.finished else 24) * S)
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
    ap.add_argument("--offset", type=float, default=0.0,
                    help="latency compensation in ms: your mean error when uncalibrated (positive = hits are treated as earlier); 0 = the saved value")
    ap.add_argument("--speed", type=float, default=1.0, help="tempo multiplier for every level (0.5 = half speed); [ and ] change it in play")
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
    ap.add_argument("--midi-trace", metavar="FILE",
                    help="append one line per note-on (wall time, note, velocity, outcome, judge µs) for latency measurements")
    args = ap.parse_args(argv)
    App(args).run()


if __name__ == "__main__":
    main()
