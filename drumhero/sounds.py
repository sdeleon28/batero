"""Synthesized drum sounds. No sample files: everything is generated at startup."""
import math

import numpy as np
import pygame

SR = 44100
MIXER_BUFFER = 256          # small buffer for low latency; raise it if audio crackles


def _t(seconds):
    return np.arange(int(seconds * SR)) / SR


def _noise(n, seed):
    return np.random.default_rng(seed).uniform(-1.0, 1.0, n)


def kick():
    t = _t(0.4)
    freq = 45 + 140 * np.exp(-t * 28)                 # fast pitch drop
    phase = 2 * math.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * 7)
    click = _noise(len(t), 1) * np.exp(-t * 400) * 0.5
    return body + click


def snare():
    t = _t(0.3)
    tone = (np.sin(2 * math.pi * 185 * t) + 0.5 * np.sin(2 * math.pi * 330 * t)) * np.exp(-t * 22) * 0.5
    rattle = _noise(len(t), 2) * np.exp(-t * 16)
    rattle = np.diff(rattle, prepend=0.0) * 0.9 + rattle * 0.5   # brighten
    return tone + rattle


def hihat():
    t = _t(0.09)
    x = _noise(len(t), 3)
    for _ in range(2):                                  # crude high-pass
        x = np.diff(x, prepend=0.0)
    return x * np.exp(-t * 55)


def crash(seed=4, dur=1.6):
    t = _t(dur)
    x = _noise(len(t), seed)
    x = np.diff(x, prepend=0.0)
    shimmer = sum(np.sin(2 * math.pi * f * t) for f in (3150, 4270, 5590, 7330)) * 0.05
    return (x + shimmer) * np.exp(-t * 2.4) * (1 - np.exp(-t * 200))


def tom(f0=170.0, dur=0.45, seed=5):
    t = _t(dur)
    freq = f0 * (1 + 0.6 * np.exp(-t * 30))              # short pitch bend down
    phase = 2 * math.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * 7)
    attack = _noise(len(t), seed) * np.exp(-t * 60) * 0.3
    return body + attack


def floor_tom():
    return tom(105.0, 0.6, seed=8)


def ride():
    t = _t(1.2)
    x = np.diff(_noise(len(t), 6), prepend=0.0)          # bright wash
    ping = np.sin(2 * math.pi * 3200 * t) * np.exp(-t * 12) * 0.5 + np.sin(2 * math.pi * 4700 * t) * np.exp(-t * 9) * 0.3
    return x * np.exp(-t * 4) * 0.5 + ping


def click(high=False):
    t = _t(0.03)
    return np.sin(2 * math.pi * (1600 if high else 1000) * t) * np.exp(-t * 120)


def tone(midi_note):
    t = _t(0.18)
    f = 440 * 2 ** ((midi_note - 69) / 12)
    return np.sin(2 * math.pi * f * t) * np.exp(-t * 18)


def _to_sound(x):
    x = x / (np.max(np.abs(x)) or 1.0) * 0.85
    pcm = (x * 32767).astype(np.int16)
    stereo = np.column_stack([pcm, pcm])
    return pygame.sndarray.make_sound(np.ascontiguousarray(stereo))


def output_devices():
    """Names of the audio output devices SDL can open."""
    try:
        from pygame._sdl2 import audio
        return list(audio.get_audio_device_names(False))
    except Exception:
        return []


class SoundBank:
    """Builds the sounds once and plays them by chart key ("kick", "n45", ...).
    device: output device name (substring match), None = system default."""

    def __init__(self, enabled=True, device=None, drums=True):
        self.ok = False
        self.sounds = {}
        self.device = None
        self.drums = drums          # False: the kit is silent (the module or a DAW makes the drum sound)
        if not enabled:
            return
        name = None
        if device:
            names = output_devices()            # needs SDL audio up: list before quitting the mixer
            name = next((n for n in names if device.lower() in n.lower()), None)
            if name is None:
                print(f"audio device '{device}' not found, using the default. Available: {', '.join(names) or 'none'}")
        if pygame.mixer.get_init():
            pygame.mixer.quit()
        try:
            pygame.mixer.pre_init(SR, -16, 2, MIXER_BUFFER)
            if name:
                pygame.mixer.init(devicename=name)
            else:
                pygame.mixer.init()
            pygame.mixer.set_num_channels(24)
        except pygame.error as e:
            print(f"audio disabled: {e}")
            return
        self.ok = True
        self.device = name
        print(f"audio output: {name or 'system default'}")
        self.sounds = {
            "kick": _to_sound(kick()), "snare": _to_sound(snare()),
            "hihat": _to_sound(hihat()), "crash": _to_sound(crash()),
            "tom1": _to_sound(tom()), "floor": _to_sound(floor_tom()), "ride": _to_sound(ride()),
            "crash2": _to_sound(crash(seed=9, dur=1.3)),
            "click": _to_sound(click()), "click_hi": _to_sound(click(high=True)),
        }

    def _get(self, key):
        s = self.sounds.get(key)
        if s is None and key.startswith("n"):
            s = self.sounds[key] = _to_sound(tone(int(key[1:])))
        return s

    def play(self, key, velocity=100, gain=1.0):
        if not self.ok or (not self.drums and key not in ("click", "click_hi")):
            return
        s = self._get(key)
        if s is None:
            return
        ch = s.play()
        if ch is not None:
            ch.set_volume(max(0.05, min(1.0, gain * (0.3 + 0.7 * velocity / 127))))


# ---------------------------------------------------------------------------
# Backing: a small arrangement rendered once for the whole level. A chord
# progression (picked per level), a pad, a bass line, a plucked arpeggio and a
# generated lead, arranged in 4-bar sections that add, drop and vary parts so the
# music moves while you drill.
# ---------------------------------------------------------------------------
# (root semitone relative to C, chord quality) per bar. Picked per level.
PROGRESSIONS = [
    [(9, "m"), (5, "M"), (0, "M"), (7, "M")],      # Am F C G
    [(4, "m"), (0, "M"), (7, "M"), (2, "M")],      # Em C G D
    [(2, "m"), (10, "M"), (5, "M"), (0, "M")],     # Dm Bb F C
    [(7, "M"), (2, "M"), (4, "m"), (0, "M")],      # G D Em C
    [(0, "M"), (7, "M"), (9, "m"), (5, "M")],      # C G Am F
    [(9, "m"), (7, "M"), (5, "M"), (7, "M")],      # Am G F G
    [(2, "m"), (7, "M"), (0, "M"), (9, "m")],      # Dm G C Am
    [(5, "M"), (7, "M"), (9, "m"), (9, "m")],      # F G Am Am
    [(4, "m"), (2, "M"), (7, "M"), (9, "M")],      # Em D G A
    [(0, "m"), (8, "M"), (3, "M"), (10, "M")],     # Cm Ab Eb Bb
]
BACKING_GAIN = 0.55
SECTION_BARS = 4
# What plays in each 4-bar section, cycling. (bass pattern, arp pattern, lead, pad)
SECTIONS = [
    ("eighths", None, False, True),        # pad + bass
    ("eighths", "up", False, True),        # + arpeggio
    ("root_fifth", "up", True, True),      # + lead
    ("sync", "updown", True, True),        # busier bass, arp turns around
    ("sparse", None, False, True),         # breakdown: pad and a few bass notes
    ("octave", "sixteenths", True, True),  # build: bouncing bass, fast arp, lead
    ("root_fifth", "broken", False, True), # settle
    ("sync", "up", True, False),           # no pad: bass, arp and lead carry it
]
BASS_PATTERNS = {                          # (eighth index, degree, gain) per bar; degree 0 = root, 7 = fifth
    "eighths": [(e, 0, 1.0 if e % 2 == 0 else 0.7) for e in range(8)],
    "root_fifth": [(0, 0, 1.0), (2, 7, 0.8), (4, 0, 1.0), (6, 7, 0.8), (7, 0, 0.6)],
    "sync": [(0, 0, 1.0), (3, 0, 0.9), (4, 7, 0.8), (6, 0, 0.9), (7, 12, 0.6)],
    "sparse": [(0, 0, 1.0), (6, 0, 0.6)],
    "octave": [(e, 0 if e % 2 == 0 else 12, 1.0 if e % 2 == 0 else 0.75) for e in range(8)],
}


def _midi_hz(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def _saw(phase):
    return 2 * (phase % 1.0) - 1


def _chord_notes(root, quality, octave_base=57, inversion=0, seventh=False):
    """Chord tones as MIDI numbers around octave_base (A3 = 57)."""
    third = 3 if quality == "m" else 4
    r = octave_base + ((root - octave_base) % 12)
    tones = [r, r + third, r + 7] + ([r + (10 if quality == "m" else 11)] if seventh else [])
    for _ in range(inversion):
        tones = tones[1:] + [tones[0] + 12]
    return tones


def _scale(root, quality):
    """Seven scale degrees (semitones from root): natural minor or major."""
    return [0, 2, 3, 5, 7, 8, 10] if quality == "m" else [0, 2, 4, 5, 7, 9, 11]


def _arp_order(pattern, chord):
    top = [m + 12 for m in chord[:3]]
    if pattern == "up":
        return top + [top[1]]
    if pattern == "updown":
        return top + [top[1]]
    if pattern == "broken":
        return [top[0], top[2], top[1], top[2]]
    if pattern == "sixteenths":
        return top + [top[2] + 12]
    return top


def make_arrangement(bpm, prog_index=0, bars=8, intro_bars=0, sr=SR, seed=None):
    """Mono float32 of (intro_bars + bars) bars at bpm: intro (pad and sparse bass) then the
    arrangement, bar 0 of the level at intro_bars * bar seconds. Deterministic per
    prog_index unless seed is given."""
    rng = np.random.default_rng(prog_index if seed is None else seed)
    prog = PROGRESSIONS[prog_index % len(PROGRESSIONS)]
    transpose = int(rng.integers(-3, 4))                    # a different key per level
    lead_tone = int(rng.integers(0, 3))                     # sine / triangle-ish / soft square
    beat = 60 / bpm
    bar = 4 * beat
    total_bars = intro_bars + bars
    n = int(round(total_bars * bar * sr))
    out = np.zeros(n + int(1.0 * sr))

    def add(start_s, sig):
        i = int(round(start_s * sr))
        if i >= len(out):
            return
        j = min(i + len(sig), len(out))
        out[i:j] += sig[: j - i]

    def pad(t0, chord):
        tp = np.arange(int(bar * sr) + int(0.08 * sr)) / sr
        sig = np.zeros_like(tp)
        for m in chord:
            f = _midi_hz(m)
            sig += _saw(tp * f * 1.003) + _saw(tp * f * 0.997)
        env = np.minimum(1.0, tp / 0.06) * np.minimum(1.0, np.maximum(0.0, (bar + 0.08 - tp) / 0.08))
        sig = np.convolve(sig * env, np.ones(48) / 48, mode="same")
        add(t0, sig * 0.16 / max(1, len(chord) / 3))

    def bass(t0, root_note, pattern):
        for e, degree, gain in BASS_PATTERNS[pattern]:
            f = _midi_hz(root_note + degree)
            dur = beat / 2 if pattern != "sparse" else beat
            tb = np.arange(int(dur * sr)) / sr
            envb = np.exp(-tb * (6 if pattern != "sparse" else 3)) * np.minimum(1.0, tb / 0.004)
            sig = (np.sin(2 * np.pi * f * tb) * 0.8 + np.sin(4 * np.pi * f * tb) * 0.25 + _saw(tb * f) * 0.15) * envb
            add(t0 + e * beat / 2, sig * 0.55 * gain)

    def arp(t0, chord, pattern):
        order = _arp_order(pattern, chord)
        steps = 16 if pattern == "sixteenths" else 8
        step = bar / steps
        for e in range(steps):
            if pattern == "updown":
                seq = order + order[-2:0:-1]                 # up then back down
                m = seq[e % len(seq)]
            else:
                m = order[e % len(order)]
            f = _midi_hz(m)
            ta = np.arange(int(0.25 * sr)) / sr
            enva = np.exp(-ta * (14 if steps == 8 else 22)) * np.minimum(1.0, ta / 0.002)
            sig = (np.sin(2 * np.pi * f * ta) + 0.3 * np.sin(6 * np.pi * f * ta)) * enva
            add(t0 + e * step, sig * (0.22 if steps == 8 else 0.17) * (1.0 if e % 4 == 0 else 0.8))

    def lead(t0, root, quality, chord, bar_in_section):
        """A phrase of chord tones and scale steps on a simple rhythm, answered every other bar."""
        scale = [root + 60 + d for d in _scale(root, quality)] + [root + 72]
        tones = {m % 12 for m in chord}
        rhythm = [(0, 1.0), (1.5, 0.5), (2, 1.0), (3, 0.5), (3.5, 0.5)] if bar_in_section % 2 == 0 else [(0.5, 1.5), (2, 2.0)]
        pos = int(rng.integers(2, 6))
        for k, (start, length) in enumerate(rhythm):
            pos = max(0, min(len(scale) - 1, pos + int(rng.integers(-2, 3))))
            if k == 0 or start in (0, 2):                      # strong beats land on a chord tone
                cands = [i for i, m in enumerate(scale) if m % 12 in tones]
                pos = min(cands, key=lambda i: abs(i - pos))
            f = _midi_hz(scale[pos])
            dur = length * beat
            tl = np.arange(int(dur * sr)) / sr
            env = np.minimum(1.0, tl / 0.02) * np.exp(-tl * 2.5) * np.minimum(1.0, np.maximum(0.0, (dur - tl) / 0.05))
            ph = 2 * np.pi * f * tl
            if lead_tone == 0:
                sig = np.sin(ph) + 0.2 * np.sin(2 * ph)
            elif lead_tone == 1:
                sig = 2 / np.pi * np.arcsin(np.sin(ph))       # triangle
            else:
                sig = np.tanh(2.5 * np.sin(ph)) * 0.8         # soft square
            vib = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * tl) * np.minimum(1.0, tl / 0.3)
            sig = np.interp(tl * vib, tl, sig) if len(tl) > 1 else sig
            add(t0 + start * beat, sig * env * 0.13)

    for i in range(total_bars):
        b = i - intro_bars                                    # level bar, negative in the intro
        t0 = i * bar
        root, quality = prog[b % len(prog)]
        root = (root + transpose) % 12
        section = SECTIONS[(b // SECTION_BARS) % len(SECTIONS)] if b >= 0 else ("sparse", None, False, True)
        bass_pat, arp_pat, lead_on, pad_on = section
        inversion = (b // len(prog)) % 3 if b >= 0 else 0
        chord = _chord_notes(root, quality, inversion=inversion, seventh=(b >= 0 and (b // SECTION_BARS) % 2 == 1))
        bass_root = _chord_notes(root, quality)[0] - 24
        if pad_on:
            pad(t0, chord)
        bass(t0, bass_root, bass_pat)
        if arp_pat:
            arp(t0, chord, arp_pat)
        if lead_on:
            lead(t0, root, quality, chord, b % SECTION_BARS)

    out = out[:n]
    out = out / (np.max(np.abs(out)) or 1.0) * 0.8
    return out.astype(np.float32)


def make_backing(bpm, prog_index=0, bars=4, sr=SR):
    """Backwards-compatible: (float32 mono array of `bars` bars, length in seconds)."""
    data = make_arrangement(bpm, prog_index, bars, 0, sr)
    return data, len(data) / sr


def backing_sound(bpm, prog_index=0, bars=4, lead_in_s=0.0):
    """pygame Sound of the loop plus its length in seconds. Needs the mixer initialised.
    lead_in_s: playback starts that long before chart time 0; the loop is rotated so
    the first chord still lands on chart time 0."""
    data, length = make_backing(bpm, prog_index, bars)
    data = np.roll(data, int(round(lead_in_s * SR)))
    pcm = (data * 32767).astype(np.int16)
    stereo = np.ascontiguousarray(np.column_stack([pcm, pcm]))
    snd = pygame.sndarray.make_sound(stereo)
    snd.set_volume(BACKING_GAIN)
    return snd, length


# ---------------------------------------------------------------------------
# Metronome: round, conga-like hits, pre-rendered for the whole level so they
# are sample-accurate and sit inside the mix instead of on top of it.
# ---------------------------------------------------------------------------
METRONOME_GAIN = 0.6
METRO_LEVELS = {"low": 1.0, "mid": 0.72, "tap": 0.34}


def conga(f=180.0, dur=0.35, drop=1.35, decay=9.0, noise=0.12, seed=7):
    """A tuned hand-drum: sine with a quick pitch drop, soft saturation, a breath of noise."""
    t = _t(dur)
    freq = f * (1 + (drop - 1) * np.exp(-t * 90))
    phase = 2 * math.pi * np.cumsum(freq) / SR
    body = np.sin(phase) * np.exp(-t * decay)
    attack = _noise(len(t), seed) * np.exp(-t * 350) * noise
    x = np.tanh(1.6 * (body + attack)) * np.minimum(1.0, t / 0.001)
    return x / (np.max(np.abs(x)) or 1.0)


METRO_HITS = {
    "low": lambda: conga(150.0, 0.40, 1.4, 8.0, 0.10, 11),   # downbeat: low conga
    "mid": lambda: conga(215.0, 0.30, 1.3, 11.0, 0.12, 12),  # beats 2, 3, 4
    "tap": lambda: conga(330.0, 0.12, 1.2, 32.0, 0.25, 13),  # subdivisions: muted tap
}


def _mix_events(events, total_s):
    """events: [(time_s, mono array, gain)] -> mono float32 of total_s seconds, peak 0.9."""
    out = np.zeros(int(total_s * SR) + SR)
    for t0, sig, gain in events:
        i = int(round(t0 * SR))
        if i < 0 or i >= len(out):
            continue
        j = min(i + len(sig), len(out))
        out[i:j] += sig[: j - i] * gain
    peak = np.max(np.abs(out))
    if peak > 0.9:
        out *= 0.9 / peak
    return out.astype(np.float32)


def render_metronome(chart, lead_in_s, total_s, mode="full"):
    """Congas for every bar from the count-in to total_s (chart time), on the chart's own
    beat grid (constant tempo or a song's tracked beats). mode: full / beats."""
    hits = {k: f() for k, f in METRO_HITS.items()}
    events = []
    first_beat = int(round(chart.beat_pos(-lead_in_s)))
    last_beat = int(chart.beat_pos(total_s)) + 1
    for i in range(first_beat, last_beat):
        t0 = chart.beat_time(i)
        b = i % 4
        sub = chart.subdivision_at(max(0.0, t0))
        events.append((t0 + lead_in_s, hits["low" if b == 0 else "mid"], METRO_LEVELS["low" if b == 0 else "mid"]))
        if mode == "full":
            for k in range(1, sub):
                events.append((chart.beat_time(i + k / sub) + lead_in_s, hits["tap"], METRO_LEVELS["tap"]))
    return _mix_events(events, lead_in_s + total_s)


def render_backing_track(bpm, prog_index, lead_in_s, total_s):
    """The arrangement from the count-in to total_s, bar 0 landing on chart time 0."""
    bar = 240 / bpm
    intro_bars = int(round(lead_in_s / bar))
    bars = int(np.ceil(total_s / bar)) + 1
    data = make_arrangement(bpm, prog_index, bars, intro_bars)
    n = int((lead_in_s + total_s) * SR) + SR
    if len(data) < n:
        data = np.concatenate([data, np.zeros(n - len(data), dtype=np.float32)])
    return data[:n]


class Track:
    """A pre-rendered track on the chart timeline, starting at t0 (usually -lead_in).
    Playback can start from any point, which makes pause/resume and late joins exact.
    data: mono float32 in -1..1, or stereo int16 (n, 2) straight from a decoded file."""

    def __init__(self, data, t0, gain=1.0):
        if data.dtype == np.int16 and data.ndim == 2:
            self.pcm = np.ascontiguousarray(data)
        else:
            pcm = (np.clip(data, -1, 1) * 32767).astype(np.int16)
            self.pcm = np.ascontiguousarray(np.column_stack([pcm, pcm]))
        self.t0 = t0
        self.gain = gain
        self.sound = None
        self.playing = False

    @property
    def end(self):
        return self.t0 + len(self.pcm) / SR

    def start_at(self, t):
        self.stop()
        i = int(max(0.0, t - self.t0) * SR)
        if i >= len(self.pcm):
            return
        self.sound = pygame.sndarray.make_sound(np.ascontiguousarray(self.pcm[i:]))
        self.sound.set_volume(self.gain)
        self.sound.play()
        self.playing = True

    def stop(self):
        if self.sound is not None:
            self.sound.stop()
        self.sound = None
        self.playing = False


# ---------------------------------------------------------------------------
# Menu music: a slow ambient texture for the hub and lists. Soft chords that
# drift, a sparse pentatonic pluck with a delay tail, a low hum underneath.
# ---------------------------------------------------------------------------
MENU_MUSIC_GAIN = 0.32
MENU_CHORDS = [[57, 60, 64, 67], [55, 59, 62, 66], [53, 57, 60, 64], [52, 55, 59, 62]]   # Am7 Gmaj7 Fmaj7 Em7
MENU_PLUCKS = [69, 72, 74, 76, 79, 81, 84]                                             # A minor pentatonic


def make_menu_music(bar_s=4.0, bars=8, sr=SR):
    """Loop of `bars` bars of `bar_s` seconds. Chords change every two bars."""
    total = bars * bar_s
    n = int(total * sr)
    tail = int(2.0 * sr)
    out = np.zeros(n + tail)

    def add(start_s, sig, gain):
        i = int(round(start_s * sr))
        j = min(i + len(sig), len(out))
        if i < j:
            out[i:j] += sig[: j - i] * gain

    rng = np.random.default_rng(21)
    for b in range(bars):
        chord = MENU_CHORDS[(b // 2) % len(MENU_CHORDS)]
        t0 = b * bar_s
        # pad, with a slow swell so chords breathe
        tp = np.arange(int(bar_s * sr) + int(1.0 * sr)) / sr
        pad = np.zeros_like(tp)
        for m in chord:
            f = _midi_hz(m)
            pad += _saw(tp * f * 1.004) + _saw(tp * f * 0.996) + 0.6 * np.sin(2 * np.pi * f / 2 * tp)
        env = np.minimum(1.0, tp / 0.9) * np.minimum(1.0, np.maximum(0.0, (bar_s + 1.0 - tp) / 1.0))
        env *= 0.85 + 0.15 * np.sin(2 * np.pi * tp / bar_s * 0.5)
        pad = np.convolve(pad * env, np.ones(160) / 160, mode="same")
        add(t0, pad, 0.10)
        # sparse plucks: a few per bar, mostly on the eighths, with two soft echoes
        for e in range(8):
            if rng.random() > 0.4:
                continue
            note = MENU_PLUCKS[rng.integers(len(MENU_PLUCKS))] + (12 if rng.random() < 0.2 else 0)
            f = _midi_hz(note)
            ta = np.arange(int(1.2 * sr)) / sr
            sig = (np.sin(2 * np.pi * f * ta) + 0.25 * np.sin(4 * np.pi * f * ta)) * np.exp(-ta * 3.0) * np.minimum(1.0, ta / 0.003)
            when = t0 + e * bar_s / 8
            add(when, sig, 0.16)
            add(when + 0.375, sig, 0.08)
            add(when + 0.75, sig, 0.04)
    # low hum on the chord root
    t_all = np.arange(n + tail) / sr
    for b in range(0, bars, 2):
        f = _midi_hz(MENU_CHORDS[(b // 2) % len(MENU_CHORDS)][0] - 24)
        seg = slice(int(b * bar_s * sr), int((b + 2) * bar_s * sr))
        tt = t_all[seg] - b * bar_s
        env = np.minimum(1.0, tt / 0.5) * np.minimum(1.0, np.maximum(0.0, (2 * bar_s - tt) / 0.5))
        out[seg] += np.sin(2 * np.pi * f * tt) * env * 0.12
    out[:tail] += out[n:n + tail]
    out = out[:n]
    out = out / (np.max(np.abs(out)) or 1.0) * 0.8
    return out.astype(np.float32)


def menu_music_sound():
    data = make_menu_music()
    pcm = (data * 32767).astype(np.int16)
    snd = pygame.sndarray.make_sound(np.ascontiguousarray(np.column_stack([pcm, pcm])))
    snd.set_volume(MENU_MUSIC_GAIN)
    return snd


MUSIC_GAIN = 0.85


def load_audio_track(path, t0, gain=MUSIC_GAIN, rate=1.0):
    """Decode an audio file (mp3/ogg/wav/flac via SDL_mixer) into a Track on the chart
    timeline: audio time 0 happens at chart time t0. Needs the mixer initialised.
    rate != 1 time-stretches the recording (rate 0.5 = half speed, pitch kept) with
    librosa's phase vocoder; a few seconds of work for a full song."""
    if rate != 1.0:
        import librosa
        y, _ = librosa.load(path, sr=SR, mono=False)
        if y.ndim == 1:
            y = np.stack([y, y])
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)         # librosa 1.0 deprecation chatter
            y = librosa.effects.time_stretch(y, rate=rate)
        pcm = (np.clip(y, -1, 1) * 32767).astype(np.int16).T
        return Track(np.ascontiguousarray(pcm), t0, gain)
    snd = pygame.mixer.Sound(path)
    arr = pygame.sndarray.array(snd)
    if arr.ndim == 1:
        arr = np.column_stack([arr, arr])
    if arr.dtype != np.int16:
        arr = (np.clip(arr.astype(np.float32) / max(1.0, float(np.max(np.abs(arr)))), -1, 1) * 32767).astype(np.int16)
    return Track(arr, t0, gain)
