"""Screens (hub, category lists, kit wizard, play) and the main loop.

Navigation is drum-driven: on the hub each drum opens its own colored section;
inside a section the hi-hat moves down, the crash moves up, the snare accepts
and the kick goes back. The keyboard always works too.
"""
import argparse
import glob
import os
import sys
import threading
import time
from collections import deque

import mido
import pygame

from . import chart as C
from .chart import BEATS, EXERCISES, build_lanes, load_midi_chart, load_song_folder
from . import game as GM
from .game import Game
from .kit import (default_kit, describe, describe_pads, load_kit, load_progress, load_settings, save_kit,
                  save_progress, save_settings)
from .runlog import RunLog
from .capture import Recorder
from .twitch import Chat, StreamLink
from . import twitch as TW
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
from . import sounds as SND

TARGET_FPS = 240
CAPTURE_S = 1.5           # wizard: keep collecting note numbers this long after the first hit
NAV_MIN_VELOCITY = 25     # softer hits never navigate (sticks resting on the snare read 4..14)
NAV_SOUND_MIN_VELOCITY = 15  # ...but every hit above this is heard, undebounced, so rolls sound whole
RESULTS_GRACE_S = 1.0     # after a level ends, ignore drum hits this long before they navigate
VOLUME_STEP = 0.05        # { and } move the game's output level by this much
DEBUG_HITS = 200          # hits the ` pane remembers
STALL_S = 2.0             # a frame this long is logged with the main thread's stack (App._watchdog)
CAM_RETRY_S = 5.0         # the ! layer looks for a missing camera this often
DEBUG_BARS = 48           # of which it draws as bars
NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")


def note_name(note):
    """MIDI note -> name in the Bitwig / Kontakt convention (60 = C3): 42 -> C#1."""
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 2}"
KEY_LANES = {pygame.K_1: 0, pygame.K_2: 1, pygame.K_3: 2, pygame.K_4: 3, pygame.K_5: 4,
             pygame.K_6: 5, pygame.K_7: 6, pygame.K_8: 7, pygame.K_9: 8, pygame.K_0: 9}
MODULE_HINTS = ("td-", "td1", "td2", "td5", "alesis", "nitro", "strike", "dtx", "roland", "drum")

# What each drum does inside a list. The hub uses the drums as section buttons instead.
NAV = {"snare": "accept", "kick": "back", "hihat": "next", "crash": "prev", "crash2": "prev",
       "tom1": "lead", "floor": "lead"}      # lead: swap the leading hand (lists of exercises)
HAND_COLORS = {"R": (245, 90, 90), "L": (80, 200, 230)}     # as on the sticking strip
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
        SND.set_master(self.settings.get("volume", 1.0))
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
        self.streamer = StreamLink(self.settings, log=print)   # T: the stream, its own process (attaches to a live one)
        self.stream_phase = "off"                 # what the badge showed last frame; transitions become toasts
        self.chat = None                          # the channel's chat while the ! layer is on
        self.editor = E.Editor(self.settings.get("claude_bin"))   # Edit with Claude
        self.coach = Coach(self.settings.get("claude_bin"), self.settings.get("coach_language", "es"),
                           self.settings.get("coach_model"))
        self.session = None          # {"name", "items": [(cat, index, rate, reps, why, lead)], "pos", "rep"} while a playlist runs
        self.lead = "R"              # the hand leading the exercises that have a lead (Chart.lead); toms swap it
        self.toasts = Toasts()
        self.camera_name = None
        self.watcher = DeviceWatcher(args.port, self.settings.get("audio_device"), self.settings.get("capture_camera", "iPhone"),
                                     midi_hints=MODULE_HINTS)
        self.midi_trace = None       # one line per note-on, for latency measurements (--midi-trace or settings)
        trace = getattr(args, "midi_trace", None) or self.settings.get("midi_trace")
        if trace:
            self.midi_trace = open(os.path.expanduser(trace), "a")
        self.legend_flash = {}     # instrument -> wall time of its last navigation hit
        self.debug_on = False      # the ` key: velocity viewer over any screen
        self.layer_on = False      # the ! key: the streamer's layer over any screen (the camera as the PiP, the chat)
        self.layer_auto = False    # the stream turned it on (so its end turns it off and frees the camera); ! makes it the user's
        self.cam_preview = None    # our own CameraPreview while the recorder does not hold the camera
        self._cam_opening = None   # the thread opening it (open_cam_preview)
        self._cam_tried = -1e9     # perf_counter of the last attempt
        self._cam_wanted = False
        self._cam_cache = (None, None)   # (source frame counter, scaled surface)
        self.debug_hits = deque(maxlen=DEBUG_HITS)   # (wall t, note, velocity, instrument, outcome, is ghost)

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

    @property
    def volume(self):
        return SND.master()

    def set_volume(self, v):
        """The game's own output level ({ and }), independent of the mixer's fader. Applies to
        the sounds already playing (menu music, a level's tracks) and to every new one; saved."""
        SND.set_master(v)
        self.settings["volume"] = SND.master()
        save_settings(self.settings)
        if self.menu_music is not None:
            self.menu_music.set_volume(SND.MENU_MUSIC_GAIN * SND.master())
        if isinstance(self.screen_obj, PlayScreen):
            for track, _ in self.screen_obj.game.tracks.values():
                track.apply_gain()
        self.toasts.add(f"volume {SND.master():.0%}", ACCENT, key="volume")

    def nudge_volume(self, d):
        self.set_volume(round(SND.master() + d * VOLUME_STEP, 2))

    @property
    def dyn_scale(self):
        return self.settings.get("dyn_scale", 1.0)

    def set_dyn_scale(self, v):
        """Accent sensitivity (; and '): scales the accent / tap thresholds of every level,
        100 % = the measured ones. Applies to the level being played; saved."""
        v = round(max(GM.DYN_SCALE_MIN, min(GM.DYN_SCALE_MAX, float(v))), 2)
        self.settings["dyn_scale"] = v
        save_settings(self.settings)
        if isinstance(self.screen_obj, PlayScreen):
            self.screen_obj.game.dyn_scale = v
            if self.runlog is not None:
                self.runlog.add("dyn_scale", thresholds=self.screen_obj.game.dyn_thresholds())
        self.toasts.add(f"accent sensitivity {v:.0%}", ACCENT, key="dyn_scale")

    def nudge_dyn_scale(self, d):
        self.set_dyn_scale(self.dyn_scale + d * GM.DYN_SCALE_STEP)

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
            # triplet and sextuplet levels get the shuffle arrangement: the music must confirm the subdivision;
            # a level with a style of its own (the cumbias) names it
            sub = chart.subdivision_at(0)
            feel = chart.backing or ("sextuplet" if sub % 6 == 0 else "triplet" if sub % 3 == 0 else "straight")
            key = ("backing", chart.name, round(chart.bpm, 3), prog_index, feel)
            if key not in self.track_cache:
                self.track_cache[key] = Track(render_backing_track(chart.bpm, prog_index, lead_in, total, feel), -lead_in, BACKING_GAIN)
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
            self.debug_hits.append((time.perf_counter(), msg.note, msg.velocity, self.instrument_for(msg.note),
                                    result if isinstance(result, str) else None, why is not None))
            if self.midi_trace is not None:
                # wall clock at the callback, note, velocity, outcome, microseconds spent judging
                self.midi_trace.write(f"{time.time():.6f} {msg.note} {msg.velocity} {result or '-'} "
                                      f"{(time.perf_counter() - t_cb) * 1e6:.0f}\n")
                self.midi_trace.flush()

    def nav_hit(self, note, velocity):
        """A drum hit used as a button. Every hit is sounded; every hit above the gate is
        one action, immediately (no debounce: the game must feel instant, and MIDI hits
        never bounce), queued for the main thread."""
        inst = self.instrument_for(note)
        if inst is None or velocity < NAV_SOUND_MIN_VELOCITY:
            return
        self.sounds.play(inst, velocity, 0.8)
        if velocity < NAV_MIN_VELOCITY:
            return
        if inst == "crash2":
            inst = "crash"                     # either crash is the "up" / Setup button
        self.legend_flash[inst] = time.perf_counter()
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
        elif kind == "audio-list":
            # CoreAudio's device set changed (a phone mic, a display...): SDL can lose its output
            # when that happens, so reopen the mixer on the device we want
            if self.sounds.ok and not self.recorder.active:
                self.reopen_sounds(self.settings.get("audio_device"))
                self.toasts.add(f"audio devices changed ({name}): mixer reopened", DIM)
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
            if connected and self.layer_on and self.cam_preview is None:
                self._cam_tried = -1e9                     # the layer was waiting for it: look now, not in CAM_RETRY_S
            self.toasts.add(f"camera {name} {'available for takes' if connected else 'gone'}", ok if connected else DIM)

    def reopen_sounds(self, device):
        """Rebuild the mixer on `device` (None = system default) without touching the saved
        setting. A running level keeps going: its tracks restart from the current time on the
        next update (Track keeps its samples and makes a fresh Sound for the new mixer)."""
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
        """(category, index, lead) for a level name or a left-hand-lead key ("Paradiddle (L)")."""
        for cat in ("kick", "snare", "hihat"):
            for i, ch in enumerate(self.items_for(cat)):
                if ch.name == name:
                    return cat, i, "R"
                if ch.lead and ch.mirrored().key == name:
                    return cat, i, "L"
        return None

    def chart_for(self, cat, index):
        """The level to play: the left-hand-lead mirror when that is the chosen lead."""
        ch = self.items_for(cat)[index]
        return ch.mirrored() if ch.lead and self.lead == "L" else ch

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
                items.append((found[0], found[1], float(it.get("rate", 1.0)), max(1, int(it.get("reps", 1))), it.get("why", ""), found[2]))
        if not items:
            return False
        self.session = {"name": playlist.get("name", "session"), "items": items, "pos": 0, "rep": 1}
        self.session_go()
        return True

    def session_go(self):
        cat, index, rate, reps, why, lead = self.session["items"][self.session["pos"]]
        self.rate, self.lead = rate, lead
        self.go(PlayScreen(self, cat, index))

    def session_advance(self):
        """Called when a level of a session ends and the player continues. True if it moved on."""
        ss = self.session
        if ss is None:
            return False
        cat, index, rate, reps, why, lead = ss["items"][ss["pos"]]
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
            self.toasts.add(f"take stopped: {os.path.basename(path)}, rendering both editions", DIM)
            print(f"recording stopped, finishing {path}")
        else:
            self.close_cam_monitor()                      # the camera goes to the recorder; ! reopens it after
            name = self.screen_obj.chart.key if isinstance(self.screen_obj, PlayScreen) else "take"
            self.watcher.paused = True                     # the camera is ffmpeg's now
            if self.recorder.start(self.size, name):
                self.toasts.add("recording" + (f" with camera {self.camera_name}" if self.camera_name else ", no camera"), (235, 70, 70))
                print("recording started")
            else:
                self.watcher.paused = False
                self.toasts.add(f"recording could not start: {self.recorder.error}", JUDGE_COLORS["MISS"])
                print(f"recording could not start: {self.recorder.error}")

    def draw_debug(self):
        """The ` pane: the last hits' velocities as bars against the accent / tap thresholds
        in force, the last few as text, the pedal CC. Drawn over any screen."""
        if not self.debug_on:
            return
        S, f = self.scale, self.fonts
        game = self.screen_obj.game if isinstance(self.screen_obj, PlayScreen) else None
        night = game.night if game is not None else GM.is_night()
        scale = game.dyn_scale if game is not None else self.dyn_scale
        hits = list(self.debug_hits)
        now = time.perf_counter()
        w, h = 470 * S, 232 * S
        x0, y0 = 16 * S, self.size[1] - h - 16 * S
        pane = pygame.Surface((int(w), int(h)), pygame.SRCALPHA)
        pane.fill((*LANE_BG, 225))
        self.surface.blit(pane, (x0, y0))
        pygame.draw.rect(self.surface, DIM, (x0, y0, w, h), 1)
        a_min, t_max = GM.dyn_band(None, night, scale)
        head = f"velocity   pedal cc {self.ghosts.pedal_cc}   thresholds {a_min}/{t_max}" + ("  night" if night else "")
        self.surface.blit(f.text(head, f.small, DIM), (x0 + 12 * S, y0 + 8 * S))
        # bars: the last DEBUG_BARS hits, newest at the right, 0..127 tall; ghosts hollow
        gx, gy, gw, gh = x0 + 12 * S, y0 + 34 * S, w - 130 * S, 96 * S     # room for the big number at the right
        pygame.draw.rect(self.surface, BG, (gx, gy, gw, gh))
        for v, col in ((a_min, JUDGE_COLORS["PERFECT"]), (t_max, ACCENT)):
            yy = gy + gh * (1 - v / 127)
            pygame.draw.line(self.surface, lerp(col, BG, 0.5), (gx, yy), (gx + gw, yy), 1)
        bars = hits[-DEBUG_BARS:]
        bw = gw / DEBUG_BARS
        for i, (t, note, vel, inst, res, ghost) in enumerate(bars):
            bh = gh * vel / 127
            bx = gx + gw - (len(bars) - i) * bw
            band = GM.dyn_band(inst, night, scale)
            col = JUDGE_COLORS["MISS"] if ghost else JUDGE_COLORS["PERFECT"] if vel >= band[0] else ACCENT if vel <= band[1] else DIM
            rect = (bx + 1, gy + gh - bh, max(1, bw - 2), bh)
            pygame.draw.rect(self.surface, col, rect, 1 if ghost else 0)
        # the last hit, big, and the last few as text
        if hits:
            t, note, vel, inst, res, ghost = hits[-1]
            ts = f.text(str(vel), f.big, JUDGE_COLORS["MISS"] if ghost else TEXT)
            self.surface.blit(ts, (x0 + w - 12 * S - ts.get_width(), gy + gh / 2 - ts.get_height() / 2 - 10 * S))
            ns = f.text(f"{note} {note_name(note)}", f.small, DIM)   # the note: number and name
            self.surface.blit(ns, (x0 + w - 12 * S - ns.get_width(), gy + gh - ns.get_height() - 2 * S))
        y = y0 + 138 * S
        for t, note, vel, inst, res, ghost in reversed(hits[-4:]):
            age = now - t
            line = f"{age:5.1f}s  {note:3d} {note_name(note):<4} {inst or '?':7} {vel:3d}  {'ghost: ' + res if ghost else (res or '')}"
            self.surface.blit(f.text(line[:56], f.small, DIM if age > 2 else TEXT), (x0 + 12 * S, y))
            y += 22 * S

    # --- the ! layer: the camera as the picture-in-picture, the chat ---------------------
    def set_layer(self, on, auto=False):
        """! : the streamer's layer over any screen: the camera where the computer edition puts
        it (capture_pip of the height in capture_corner, 30 fps) and the channel's chat pane.
        Independent of the stream (it is how the picture is checked before going live), but
        going live turns it on (auto=True): that is how the iPhone gets into the stream, which
        is the display; and the stream's end turns an auto layer off again, so the camera is
        released (Continuity Camera stayed "connected" after a stream, 2026-09-13). A layer the
        user switched with ! is theirs: the stream leaves it alone."""
        if on == self.layer_on:
            if not auto:
                self.layer_auto = False
            return
        self.layer_on = on
        self.layer_auto = auto and on
        if on:
            if self.chat is None:
                self.chat = Chat(self.streamer.settings["twitch_channel"], log=print)
        else:
            self.close_cam_monitor()
            if self.chat is not None:
                self.chat.stop()
                self.chat = None
        print(f"layer {'on' if on else 'off'}")

    def close_cam_monitor(self):
        """Release our camera preview (the recorder or the camera check screen takes the camera)."""
        if self.cam_preview is not None:
            self.cam_preview.stop()
            self.cam_preview = None
        self._cam_wanted = False

    def open_cam_preview(self, fps):
        """Open our camera preview in a thread: finding the camera spawns `ffmpeg -list_devices`
        (0.7 s, 15 s when AVFoundation stalls) and used to run on the main thread, every frame
        while no camera was found (2026-09-12: the game froze at "waiting for frames" during a
        stream). A miss is retried after CAM_RETRY_S."""
        now = time.perf_counter()
        self._cam_wanted = True
        if self._cam_opening is not None or now - self._cam_tried < CAM_RETRY_S:
            return
        self._cam_tried = now

        def work():
            preview = None
            try:
                cam = CP.find_camera(self.recorder.settings["capture_camera"])
                if cam and CP.ffmpeg_path():
                    preview = CP.CameraPreview(cam, fps=fps)
            finally:
                if preview is not None and (not self._cam_wanted or self.recorder.active or self.cam_preview is not None):
                    preview.stop()                # nobody wants it any more (layer closed, a take took the camera)
                elif preview is not None:
                    self.cam_preview = preview
                    self._cam_cache = (None, None)
                self._cam_opening = None

        self._cam_opening = threading.Thread(target=work, daemon=True)
        self._cam_opening.start()

    def pip_rect(self):
        """Where the layer draws the camera: the computer edition's picture-in-picture."""
        return CP.pip_rect(self.size, self.recorder.settings["capture_pip"], self.recorder.settings["capture_corner"],
                           margin=int(16 * self.scale))

    def draw_cam_monitor(self):
        """The layer's camera: what the camera sees, as the computer edition's picture-in-picture,
        over any screen. While a take runs it shows the recorder's own frames (the ones going
        into the take); otherwise it opens a preview of its own. The camera check screen has its
        own picture."""
        if not self.layer_on or isinstance(self.screen_obj, CameraCheckScreen):
            self.close_cam_monitor()
            return
        S, f = self.scale, self.fonts
        x0, y0, pw, ph = self.pip_rect()
        live = self.streamer.active
        if self.recorder.active and self.recorder.feed is not None:
            self.close_cam_monitor()
            src, frame, size, count = self.recorder.feed, self.recorder.feed.frame, CP.CAMERA_SIZE, self.recorder.feed.frames
            error = self.recorder.feed.error
        else:
            if self.cam_preview is None and not self.recorder.active:
                self.open_cam_preview(CP.CAMERA_FPS)
            p = self.cam_preview
            src, frame, size, count = p, (p.frame if p else None), CP.PREVIEW_SIZE, (p.frames if p else 0)
            error = (p.error if p else f"no camera '{self.recorder.settings['capture_camera']}'")
        key = (id(src), count)
        if frame is not None and self._cam_cache[0] != key:
            pic = pygame.image.frombuffer(frame, size, "RGB")
            self._cam_cache = (key, pygame.transform.scale(pic, (int(pw), int(ph))))
        pic = self._cam_cache[1] if self._cam_cache[0] == key else None
        if pic is not None:
            self.surface.blit(pic, (x0, y0))
        else:
            pygame.draw.rect(self.surface, LANE_BG, (x0, y0, pw, ph))
            self.fonts.center(self.surface, error or "waiting for frames...", f.small, DIM, y0 + ph / 2 - 8 * S, x0 + pw / 2)
        pygame.draw.rect(self.surface, (235, 70, 70) if self.recorder.active else (145, 70, 255) if live else DIM, (x0, y0, pw, ph), 1)

    def draw_chat(self):
        """The layer's chat pane: the last messages of the channel that fit, name in the user's
        Twitch colour. Bottom left, the velocity viewer's place (above it when both are on);
        bottom right when the camera has the bottom left corner."""
        chat = self.chat
        if chat is None or not self.layer_on or isinstance(self.screen_obj, CameraCheckScreen):
            return
        S, f = self.scale, self.fonts
        w, h = 470 * S, 232 * S
        x0 = 16 * S if self.recorder.settings["capture_corner"] != "bl" else self.size[0] - w - 16 * S
        y0 = self.size[1] - h - 16 * S - ((232 + 12) * S if self.debug_on and x0 < self.size[0] / 2 else 0)
        pane = pygame.Surface((int(w), int(h)), pygame.SRCALPHA)
        pane.fill((*LANE_BG, 225))
        self.surface.blit(pane, (x0, y0))
        pygame.draw.rect(self.surface, DIM, (x0, y0, w, h), 1)
        head = f"chat  #{chat.channel}" + ("" if chat.connected else f"  ({chat.error or 'connecting'})")
        self.surface.blit(f.text(head, f.small, (145, 70, 255) if chat.connected else DIM), (x0 + 12 * S, y0 + 8 * S))
        with chat._lock:
            msgs = list(chat.messages)
        lines = []                                    # (name, colour, text) per wrapped line, newest last; name only on the first
        max_w = w - 24 * S
        for t, name, text, colour in msgs[-24:]:
            colour = colour or TEXT
            prefix = f"{name}: "
            pw = f.text(prefix, f.small, colour).get_width()
            words = text.split(" ")
            row, avail = "", max_w - pw
            first = True
            for word in words:
                trial = (row + " " + word).strip()
                if row and f.text(trial, f.small, TEXT).get_width() > avail:
                    lines.append((prefix if first else "", colour, row))
                    first, row, avail = False, word, max_w
                else:
                    row = trial
            lines.append((prefix if first else "", colour, row))
        lh = f.text("Ag", f.small, TEXT).get_height() + 2 * S
        y = y0 + h - 10 * S - lh
        top = y0 + 30 * S
        for prefix, colour, row in reversed(lines):
            if y < top:
                break
            x = x0 + 12 * S
            if prefix:
                ps = f.text(prefix, f.small, colour)
                self.surface.blit(ps, (x, y)); x += ps.get_width()
            self.surface.blit(f.text(row, f.small, TEXT), (x, y))
            y -= lh

    # --- the stream: T, the badge, the daemon ---------------------------------------------
    def toggle_stream(self):
        """T (and the badge's x): the stream starts and stops here and nowhere else (never on its own).
        Starting spawns the stream process; the badge says STARTING until ffmpeg reports, then LIVE."""
        st = self.streamer
        if st.phase == "stopping":
            return
        if st.active:
            self.stop_stream()
        elif st.start(self.size):
            self.set_layer(True, auto=True)            # the camera into the stream; ! hides it again
            what = (f"display {st.settings['stream_display']}" if st.source == "screen" else "%dx%d window" % st.out_size)
            self.toasts.add(f"starting the stream to twitch.tv/{st.settings['twitch_channel']}: {what}, {st.settings['stream_kbps']} kbps"
                            + (" (bandwidth test, not public)" if st.settings.get("stream_bandwidth_test") else ""), (145, 70, 255))
        else:
            self.toasts.add(f"stream could not start: {st.error}", JUDGE_COLORS["MISS"])
            print(f"stream could not start: {st.error}")

    def stop_stream(self):
        self.streamer.stop()
        if self.streamer.phase == "stopping":
            self.toasts.add("stopping the stream", DIM)

    def watch_stream(self):
        """Every frame: the stream process's state, and its transitions as toasts (live at last,
        stopped, ended on its own with the reason, found live when the game started)."""
        st = self.streamer
        st.refresh()
        phase = st.phase
        if phase == self.stream_phase:
            return
        was, self.stream_phase = self.stream_phase, phase
        if phase == "live":
            if was == "off" and st.attached:
                st.attached = False
                since = time.strftime("%H:%M", time.localtime(st.started_wall or time.time()))
                self.toasts.add(f"stream live since {since} on twitch.tv/{st.settings['twitch_channel']} (the stream runs on its own; T or the x stop it)", (145, 70, 255))
                self.set_layer(True, auto=True)
            elif was == "starting":
                self.toasts.add(f"live on twitch.tv/{st.settings['twitch_channel']}", (235, 70, 70))
        elif phase == "off":
            if st.ended_reason:
                self.toasts.add(f"stream ended: {st.ended_reason}", JUDGE_COLORS["MISS"])
                st.ended_reason = None
            else:
                self.toasts.add("stream stopped", DIM)
            if self.layer_auto:
                self.set_layer(False)                  # the camera and the chat came with the stream: they go with it

    def badge_height(self):
        """The room the play HUD leaves top right for the stream's LIVE badge (the daemon's own
        floating window, badge.py, sits over that corner while the stream lives), else 0."""
        return int(26 * self.scale) if self.streamer.phase != "off" else 0

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

    def _watchdog(self):
        """A frame that takes longer than STALL_S: the main thread's stack goes to the log, once per
        stall, so a frozen game explains itself (2026-09-12: froze during a stream, no trace)."""
        import traceback
        main_id = threading.main_thread().ident
        reported = None
        while True:
            time.sleep(0.5)
            at = self.frame_at
            stalled = time.perf_counter() - at
            if stalled < STALL_S or reported == at:
                continue
            reported = at
            frame = sys._current_frames().get(main_id)
            stack = "".join(traceback.format_stack(frame)) if frame is not None else "(no frame)"
            print(f"main loop stalled {stalled:.1f} s, main thread at:\n{stack}")

    def go(self, screen):
        self.screen_obj = screen
        self.update_menu_music()

    def run(self):
        self.go(SetupScreen(self, first_run=True) if self.first_run and self.midi_in else HubScreen(self))
        clock = pygame.time.Clock()
        running = True
        self.frame_at = time.perf_counter()
        threading.Thread(target=self._watchdog, daemon=True).start()
        while running:
            self.frame_at = time.perf_counter()
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
                    elif ev.key == pygame.K_t:
                        self.toggle_stream()
                    elif ev.unicode == "{" or (ev.key == pygame.K_LEFTBRACKET and ev.mod & pygame.KMOD_SHIFT):
                        self.nudge_volume(-1)              # the plain brackets belong to the screens (tempo, corner)
                    elif ev.unicode == "}" or (ev.key == pygame.K_RIGHTBRACKET and ev.mod & pygame.KMOD_SHIFT):
                        self.nudge_volume(+1)
                    elif ev.unicode == "`" or ev.key == pygame.K_BACKQUOTE:
                        self.debug_on = not self.debug_on
                    elif ev.unicode == "!" or (ev.key == pygame.K_1 and ev.mod & pygame.KMOD_SHIFT):
                        self.set_layer(not self.layer_on)
                    elif ev.unicode == ";" or ev.key == pygame.K_SEMICOLON:
                        self.nudge_dyn_scale(-1)           # softer accents count
                    elif ev.unicode == "'" or ev.key == pygame.K_QUOTE:
                        self.nudge_dyn_scale(+1)
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
            self.draw_debug()
            self.draw_chat()                              # the ! layer, after the recorder's push: never in the take
            self.draw_cam_monitor()
            self.streamer.push(self.surface)              # window source only: the stream is the window, every overlay included
            self.watch_stream()
            pygame.display.flip()
            clock.tick(TARGET_FPS)
        if self.midi_in:
            self.midi_in.close()
        self.watcher.stop()
        self.set_layer(False)
        if self.recorder.active:
            self.recorder.stop()
        self.streamer.close()                             # the stream process stays live; T in the next game stops it
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
                keys = [ch.key for ch in items] + [ch.mirrored().key for ch in items if ch.lead]   # both leads count
                got = sum(self.app.results.get(k, {}).get("stars", 0) for k in keys)
                self.f.center(surf, f"{len(items)} levels  ·  ★ {got} / {5 * len(keys)}", self.f.small, color, y + ph - 26 * S, x + pw / 2)
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
                    (f"Volume: {self.app.volume:.0%}", "{ and } lower / raise the game's own level anywhere, saved; select raises, wraps to 5 %"),
                    (f"Accent sensitivity: {self.app.dyn_scale:.0%}", "; and ' lower / raise the accent and tap thresholds anywhere, saved; "
                                                                      f"night (22:00-08:00) lowers them another 20 %; select raises, wraps"),
                    (f"Start fullscreen: {'on' if self.app.settings.get('fullscreen', True) else 'off'}", "F11 or Cmd+F toggles any time, saved"),
                    ("Recording (V)", f"audio {self.app.recorder.settings['capture_audio_device']} ch {self.app.recorder.settings['capture_audio_channels']}"
                                      f" · camera '{self.app.recorder.settings['capture_camera']}' · ~/Movies/drumhero"),
                    ("Stream (T)", f"twitch.tv/{self.app.streamer.settings['twitch_channel']} · {self.app.streamer.settings['stream_height']}p "
                                   f"{self.app.streamer.settings['stream_kbps']} kbps · "
                                   + (f"display {self.app.streamer.settings['stream_display']} and the interface, both by ffmpeg"
                                      if self.app.streamer.settings.get('stream_source', 'screen') != 'window' else "the window as you see it, audio as the takes")
                                   + " · its own process with the LIVE badge, survives the game · ! camera and chat layer · key in ~/.config/drumhero/twitch_key"),
                    ("Camera & take check", "the iPhone next to the game picture, both editions' layout, the take's audio meter, a test take"),
                    ("Takes: editions, Claude edits", "every take renders a computer (16:9) and a social (9:16) edition; render one again, or have Claude cut it"),
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
                self.app.set_volume(VOLUME_STEP if self.app.volume >= 1.0 else self.app.volume + VOLUME_STEP)
            elif self.sel == 9:
                self.app.set_dyn_scale(GM.DYN_SCALE_MIN if self.app.dyn_scale >= GM.DYN_SCALE_MAX else self.app.dyn_scale + GM.DYN_SCALE_STEP)
            elif self.sel == 10:
                self.app.settings["fullscreen"] = not self.app.settings.get("fullscreen", True)
                save_settings(self.app.settings)
            elif self.sel == 11:
                self.app.toggle_recording()
            elif self.sel == 12:
                self.app.toggle_stream()
            elif self.sel == 13:
                self.app.go(CameraCheckScreen(self.app, self.app.surface))
            elif self.sel == 14:
                self.app.go(EditScreen(self.app))
            elif self.sel == 15:
                self.app.go(StatsScreen(self.app))
            elif self.sel == 16:
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
        elif action == "lead":
            self.swap_lead()
        return True

    def has_leads(self):
        return self.cat == "kick"

    def swap_lead(self):
        if self.has_leads():
            self.app.lead = "L" if self.app.lead == "R" else "R"

    def on_key(self, key):
        if key in (pygame.K_DOWN, pygame.K_j):
            self.move(1)
        elif key in (pygame.K_UP, pygame.K_k):
            self.move(-1)
        elif key in (pygame.K_RETURN, pygame.K_SPACE, pygame.K_l):
            return self.accept()
        elif key in (pygame.K_ESCAPE, pygame.K_h):
            self.back()
        elif key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_TAB):
            self.swap_lead()
        return True

    def draw_lead_rows(self, surf, ch, right, y, selected):
        """A two-lead level's best results, one line per hand: R on top, L below, the chosen
        lead lit on the selected row. Returns the x where the sub text must stop."""
        S, f = self.s, self.f
        stop = right
        for k, (lead, key) in enumerate((("R", ch.key), ("L", ch.mirrored().key))):
            best = self.app.results.get(key)
            yy = y - 3 * S + k * 16 * S
            x = right
            hot = selected and lead == self.app.lead
            if best:                                             # a fixed column: the stars stay aligned
                ts = f.text(f"{best['accuracy'] * 100:.0f}%", f.tiny, TEXT if hot else DIM)
                surf.blit(ts, (x - ts.get_width(), yy + 1 * S))
            x -= f.text("100%", f.tiny, DIM).get_width() + 8 * S
            x -= draw_stars(surf, f, best.get("stars", 0) if best else 0, x, yy + 2 * S, S, size="tiny") + 8 * S
            hs = f.text(lead, f.tiny, HAND_COLORS[lead] if hot or best else DIM)
            surf.blit(hs, (x - hs.get_width(), yy + 1 * S)); x -= hs.get_width()
            if hot:
                pygame.draw.rect(surf, HAND_COLORS[lead], (x - 6 * S, yy + 2 * S, 2 * S, 11 * S))
            stop = min(stop, x - 10 * S)
        return stop

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
            shown = name if len(name) <= 34 else name[:33] + "…"
            surf.blit(self.f.text(shown, self.f.mid, self.color if selected else TEXT), (x, y))
            best = self.app.results.get(name)
            right = self.w * 0.88
            ch = self.app.items_for(self.cat)[i] if self.cat != "crash" else None
            if ch is not None and ch.lead:
                right = self.draw_lead_rows(surf, ch, right, y, selected)
            elif best:
                ts = self.f.text(f"{best['accuracy'] * 100:.0f}%", self.f.small, DIM)
                surf.blit(ts, (right - ts.get_width(), y + 4 * S))
                right -= ts.get_width() + 12 * S
                right -= draw_stars(surf, self.f, best.get("stars", 0), right, y + 2 * S, S, size="small") + 16 * S
            surf.blit(self.f.text(self.fit(sub, right - (x + 500 * S)), self.f.small, DIM), (x + 500 * S, y + 4 * S))
            y += row_h
        if items:
            self.f.center(surf, items[self.sel][1], self.f.small, TEXT, self.h - 84 * S)   # the selected one in full
        if self.has_leads():
            lead = self.app.lead
            ls = self.f.text(f"lead hand  {'right' if lead == 'R' else 'left'}", self.f.small, HAND_COLORS[lead])
            surf.blit(ls, (self.w * 0.88 - ls.get_width(), 28 * S))
            self.legend(surf, [("hihat", "down"), ("crash", "up"), ("snare", "select"), ("tom1", "lead R/L"), ("kick", "back")],
                        keys="arrows or j k · Enter or l · ← → lead · Esc or h")
        else:
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
        app.close_cam_monitor()                                  # one ffmpeg on the camera at a time
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
        self.app.go(ListScreen(self.app, "crash", 11))

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
        elif key == pygame.K_s:
            self.cycle_split()
        elif key == pygame.K_p:
            self.cycle_pip(1)
        elif key == pygame.K_r:
            self.close()
            self.__init__(self.app, self.sample)
        return True

    def cycle_pip(self, d=1):
        """The computer edition: the camera picture's height as a share of the frame (P, up/down, hats/crash)."""
        sizes = CP.PIPS
        cur = min(range(len(sizes)), key=lambda i: abs(sizes[i] - self.settings["capture_pip"]))
        self.settings["capture_pip"] = sizes[(cur + d) % len(sizes)]
        self.save()

    def cycle_split(self):
        """The social edition: the camera's share of the 9:16 height (0.32 = the whole camera picture)."""
        cur = min(range(len(CP.SPLITS)), key=lambda i: abs(CP.SPLITS[i] - self.settings["capture_split"]))
        self.settings["capture_split"] = CP.SPLITS[(cur + 1) % len(CP.SPLITS)]
        self.save()

    def save(self):
        self.app.settings.update({k: self.settings[k] for k in ("capture_pip", "capture_corner", "capture_split")})
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
        # the two editions: computer (16:9, the camera picture-in-picture) and social (9:16, the
        # screen as a thumbnail on top, the camera under it)
        cy = int(y + ph + 46 * S)
        f.center(surf, f"computer edition: camera {self.settings['capture_pip']:.0%} of the height (P), corner {self.settings['capture_corner']} ([ ])"
                       f"      social edition: camera {self.settings['capture_split']:.0%} of the height (S)",
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
        sx, sh = cx + cw + int(24 * S), ch
        sw = int(sh * 9 / 16)
        pygame.draw.rect(surf, (0, 0, 0), (sx, cy, sw, sh))
        (gx, gy, gw, gh), (bx, by, bw, bh) = CP.social_layout(self.sample.get_size() if self.sample is not None else (16, 9),
                                                              self.settings["capture_split"], size=(sw, sh))
        if self.sample is not None and gw > 0 and gh > 0:
            surf.blit(pygame.transform.smoothscale(self.sample, (gw, gh)), (sx + gx, cy + gy))
        if cam is not None and bw > 0 and bh > 0:
            surf.blit(CP.cover(cam, (bw, bh)), (sx + bx, cy + by))
        else:
            pygame.draw.rect(surf, lerp(LANE_BG, ACCENT, 0.3), (sx + bx, cy + by, bw, bh))
        pygame.draw.rect(surf, ACCENT, (sx + bx, cy + by, bw, bh), 2)
        pygame.draw.rect(surf, (60, 60, 70), (sx, cy, sw, sh), 1)
        # audio meter, to the right of the previews
        mx = sx + sw + 36 * S
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
            f.center(surf, "rendering the test take's editions...", f.mid, JUDGE_COLORS["GOOD"], self.h - 92 * S)
        elif self.test_result:
            f.center(surf, self.test_result, f.mid, JUDGE_COLORS["PERFECT"] if "saved" in self.test_result else JUDGE_COLORS["MISS"], self.h - 92 * S)
        self.legend(surf, [("hihat", "size"), ("crash", "size"), ("snare", "test take 3 s"), ("kick", "back")],
                    keys="P computer camera share · [ ] corner · S social camera share · R rescan · Enter test take · Esc back · takes in ~/Movies/drumhero")


# ---------------------------------------------------------------------------
EDITIONS = [("computer", "Computer edition (16:9)", "render again: the screen, the camera picture-in-picture"),
            ("social", "Social edition (9:16)", "render again: the screen on top, the iPhone under it")]


class EditScreen(Screen):
    """Pick a take (newest first) and what to make of it: an edition rendered again from its raws
    (both are made when a take stops), or a style for Claude Code to cut. Hi-hat / crash move
    through the options, snare starts, kick goes back; [ and ] change the take."""

    def __init__(self, app):
        super().__init__(app)
        self.takes = E.takes()
        self.take_i = 0
        self.sel = 0
        self.message = None

    def options(self):
        """[(key, label, blurb, kind)], kind "edition" or "claude"."""
        return [(k, l, b, "edition") for k, l, b in EDITIONS] + [(k, l, b, "claude") for k, l, b, _ in E.STYLES]

    def on_drum(self, inst):
        action = NAV.get(inst)
        if action == "next":
            self.sel = (self.sel + 1) % len(self.options())
        elif action == "prev":
            self.sel = (self.sel - 1) % len(self.options())
        elif action == "accept":
            self.accept()
        elif action == "back":
            self.app.go(ListScreen(self.app, "crash", 12))
        return True

    def on_key(self, key):
        if key in (pygame.K_ESCAPE, pygame.K_h):
            self.app.go(ListScreen(self.app, "crash", 12))
        elif key in (pygame.K_DOWN, pygame.K_j):
            self.sel = (self.sel + 1) % len(self.options())
        elif key in (pygame.K_UP, pygame.K_k):
            self.sel = (self.sel - 1) % len(self.options())
        elif key in (pygame.K_LEFTBRACKET, pygame.K_LEFT) and self.takes:
            self.take_i = (self.take_i + 1) % len(self.takes)
        elif key in (pygame.K_RIGHTBRACKET, pygame.K_RIGHT) and self.takes:
            self.take_i = (self.take_i - 1) % len(self.takes)
        elif key in (pygame.K_RETURN, pygame.K_l):
            self.accept()
        return True

    def accept(self):
        if not self.takes:
            return
        take = self.takes[self.take_i]
        key, label, blurb, kind = self.options()[self.sel]
        self.message = None
        if kind == "edition":
            if not E.has_raws(take):
                self.message = "this take has no raws (recorded before take folders): nothing to render from"
            elif not self.app.recorder.render(take, key):
                self.message = "the recorder is busy, try again in a moment"
        elif not self.app.editor.busy:
            if self.app.editor.start(take, key):
                self.app.go(HubScreen(self.app, 3))

    def draw(self, surf, fps):
        surf.fill(BG)
        S = self.s
        self.f.center(surf, "Takes: editions and Claude edits", self.f.large, TEXT, 60 * S)
        if not self.takes:
            self.f.center(surf, "No takes yet. Press V during play to record one.", self.f.mid, DIM, self.h * 0.42)
        else:
            take = self.takes[self.take_i]
            meta = E.sidecar_for(take) or {}
            levels = ", ".join(l["chart"] for l in meta.get("run_logs", [])) or "no levels logged"
            eds = ", ".join(sorted(E.editions(take))) or "no edition yet"
            self.f.center(surf, E.label(take), self.f.mid, ACCENT, 110 * S)
            self.f.center(surf, f"{meta.get('duration', 0):.0f} s · {levels}" + ("  · camera" if meta.get("camera") else ""),
                          self.f.small, DIM, 138 * S)
            self.f.center(surf, f"editions: {eds}  ·  take {self.take_i + 1} of {len(self.takes)}  ·  [ ] or ← → to change", self.f.small, DIM, 160 * S)
        opts = self.options()
        step = 60 * S if len(opts) * 60 * S <= self.h - 330 * S else (self.h - 330 * S) / len(opts)
        y = 210 * S
        for i, (key, label, blurb, kind) in enumerate(opts):
            selected = i == self.sel
            x = self.w * 0.18
            if selected:
                pygame.draw.rect(surf, lerp(LANE_BG, ACCENT, 0.18), (x - 20 * S, y - 8 * S, self.w * 0.64 + 40 * S, step - 8 * S), border_radius=int(10 * S))
                pygame.draw.rect(surf, ACCENT, (x - 20 * S, y - 8 * S, 6 * S, step - 8 * S), border_radius=int(3 * S))
            if i == len(EDITIONS):
                pygame.draw.line(surf, (50, 50, 60), (x, y - 12 * S), (self.w * 0.82, y - 12 * S))
            surf.blit(self.f.text(label, self.f.mid, ACCENT if selected else TEXT), (x, y))
            surf.blit(self.f.text(blurb, self.f.small, DIM), (x + 340 * S, y + 6 * S))
            y += step
        note = self.message or (self.app.editor.status if self.app.editor.busy else self.app.editor.error)
        if note:
            self.f.center(surf, note, self.f.small, JUDGE_COLORS["GOOD"] if self.app.editor.busy and not self.message else JUDGE_COLORS["MISS"],
                          self.h - 84 * S)
        self.legend(surf, [("hihat", "down"), ("crash", "up"), ("snare", "go"), ("kick", "back")],
                    keys="arrows or j k · Enter or l · Esc or h · everything lands in the take's folder in ~/Movies/drumhero")


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
        self.chart = app.chart_for(cat, index).at_rate(app.rate)
        self.lanes, self.by_note = build_lanes(self.chart, app.kit)
        # scroll speed follows the tempo so a beat is always the same distance on screen
        self.game = Game(self.chart, self.lanes, self.by_note, offset_ms=app.offset_ms, speed=app.rate,
                         sounds=app.sounds, guide=app.guide and not self.chart.audio,   # the record has its own drums
                         log=app.runlog, dyn_scale=app.dyn_scale)
        self.game.metronome_mode = app.metronome_mode
        app.runlog.start(self.chart, self.lanes, app.kit, app.settings, {
            "offset_ms": app.offset_ms, "guide": self.game.guide, "metronome": app.metronome_mode,
            "backing": app.backing_on, "drum_sounds": app.sounds.drums, "dyn_thresholds": self.game.dyn_thresholds(),
            "ghost_filter": {"hihat_min_velocity": GH.HIHAT_MIN_VELOCITY, "chick_splash_ms": GH.CHICK_SPLASH_MS,
                             "chick_splash_velocity_min": GH.CHICK_SPLASH_VELOCITY_MIN,
                             "pedal_motion_cc": GH.PEDAL_MOTION_CC, "pedal_motion_ms": GH.PEDAL_MOTION_MS,
                             "pedal_motion_velocity_min": GH.PEDAL_MOTION_VELOCITY_MIN,
                             "pedal_settle_ms": GH.PEDAL_SETTLE_MS, "pedal_settle_velocity_max": GH.PEDAL_SETTLE_VELOCITY_MAX,
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
        self.app.sounds.stop_jingle()                     # the jingle must not bleed into the next screen
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
        best = self.app.results.get(self.chart.key)
        if best is None or st["grade"] > best.get("grade", -1):
            keep = {k: st[k] for k in ("stars", "grade", "accuracy", "mean_ms", "std_ms", "hit", "notes")}
            keep["when"] = time.time()
            self.app.results[self.chart.key] = keep
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
        self.renderer.top_inset = self.app.badge_height()
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
