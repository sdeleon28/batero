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


class SoundBank:
    """Builds the sounds once and plays them by chart key ("kick", "n45", ...)."""

    def __init__(self, enabled=True):
        self.ok = False
        self.sounds = {}
        if not enabled:
            return
        try:
            pygame.mixer.pre_init(SR, -16, 2, MIXER_BUFFER)
            pygame.mixer.init()
            pygame.mixer.set_num_channels(24)
        except pygame.error as e:
            print(f"audio disabled: {e}")
            return
        self.ok = True
        self.sounds = {
            "kick": _to_sound(kick()), "snare": _to_sound(snare()),
            "hihat": _to_sound(hihat()), "crash": _to_sound(crash()),
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
