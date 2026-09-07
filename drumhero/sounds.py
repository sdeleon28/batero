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


def crash():
    t = _t(1.6)
    x = _noise(len(t), 4)
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

    def __init__(self, enabled=True, device=None):
        self.ok = False
        self.sounds = {}
        self.device = None
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
            "click": _to_sound(click()), "click_hi": _to_sound(click(high=True)),
        }

    def _get(self, key):
        s = self.sounds.get(key)
        if s is None and key.startswith("n"):
            s = self.sounds[key] = _to_sound(tone(int(key[1:])))
        return s

    def play(self, key, velocity=100, gain=1.0):
        if not self.ok:
            return
        s = self._get(key)
        if s is None:
            return
        ch = s.play()
        if ch is not None:
            ch.set_volume(max(0.05, min(1.0, gain * (0.3 + 0.7 * velocity / 127))))


# ---------------------------------------------------------------------------
# Backing loop: bass + pad + plucked arpeggio over a 4-bar chord progression,
# rendered once at the level's tempo into a seamless loop.
# ---------------------------------------------------------------------------
# (root semitone relative to C, chord quality) per bar. Rotated per level for variety.
PROGRESSIONS = [
    [(9, "m"), (5, "M"), (0, "M"), (7, "M")],      # Am F C G
    [(4, "m"), (0, "M"), (7, "M"), (2, "M")],      # Em C G D
    [(2, "m"), (10, "M"), (5, "M"), (0, "M")],     # Dm Bb F C
    [(7, "M"), (2, "M"), (4, "m"), (0, "M")],      # G D Em C
]
BACKING_GAIN = 0.55


def _midi_hz(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def _saw(phase):
    return 2 * (phase % 1.0) - 1


def _chord_notes(root, quality, octave_base=57):
    """Three chord tones as MIDI numbers around octave_base (A3 = 57)."""
    third = 3 if quality == "m" else 4
    r = octave_base + ((root - octave_base) % 12)
    return [r, r + third, r + 7]


def make_backing(bpm, prog_index=0, bars=4, sr=SR):
    """Return (float32 mono array of exactly `bars` bars, loop length in seconds)."""
    prog = PROGRESSIONS[prog_index % len(PROGRESSIONS)]
    beat = 60 / bpm
    bar = 4 * beat
    total = bars * bar
    n = int(round(total * sr))
    tail = int(0.6 * sr)
    out = np.zeros(n + tail)

    def add(start_s, sig):
        i = int(round(start_s * sr))
        j = min(i + len(sig), len(out))
        out[i:j] += sig[: j - i]

    for b in range(bars):
        root, quality = prog[b % len(prog)]
        chord = _chord_notes(root, quality)
        bass_note = chord[0] - 24                                  # two octaves down
        t0 = b * bar

        # pad: two detuned saws per chord tone, soft attack, lowpassed by a moving average
        tp = np.arange(int(bar * sr) + int(0.08 * sr)) / sr
        pad = np.zeros_like(tp)
        for m in chord:
            f = _midi_hz(m)
            pad += _saw(tp * f * 1.003) + _saw(tp * f * 0.997)
        env = np.minimum(1.0, tp / 0.06) * np.minimum(1.0, np.maximum(0.0, (bar + 0.08 - tp) / 0.08))
        pad = np.convolve(pad * env, np.ones(48) / 48, mode="same")
        add(t0, pad * 0.16)

        # bass: root on every eighth, accented on the beat
        for e in range(8):
            f = _midi_hz(bass_note)
            dur = beat / 2
            tb = np.arange(int(dur * sr)) / sr
            envb = np.exp(-tb * 6) * np.minimum(1.0, tb / 0.004) * (1 if e % 2 == 0 else 0.7)
            sig = (np.sin(2 * np.pi * f * tb) * 0.8 + np.sin(4 * np.pi * f * tb) * 0.25 + _saw(tb * f) * 0.15) * envb
            add(t0 + e * dur, sig * 0.55)

        # pluck: arpeggio through the chord an octave up, on the eighths
        arp = [m + 12 for m in chord] + [chord[1] + 12]
        for e in range(8):
            f = _midi_hz(arp[e % len(arp)])
            ta = np.arange(int(0.25 * sr)) / sr
            enva = np.exp(-ta * 14) * np.minimum(1.0, ta / 0.002)
            sig = (np.sin(2 * np.pi * f * ta) + 0.3 * np.sin(6 * np.pi * f * ta)) * enva
            add(t0 + e * beat / 2, sig * 0.22)

    out[:tail] += out[n:n + tail]          # fold the tail onto the start: seamless loop
    out = out[:n]
    out = out / (np.max(np.abs(out)) or 1.0) * 0.8
    return out.astype(np.float32), n / sr


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
    """The 4-bar loop tiled from the count-in to total_s, chord 1 landing on chart time 0."""
    loop, length = make_backing(bpm, prog_index)
    loop = np.roll(loop, int(round(lead_in_s * SR)))
    n = int((lead_in_s + total_s) * SR) + SR
    reps = n // len(loop) + 1
    return np.tile(loop, reps)[:n]


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
