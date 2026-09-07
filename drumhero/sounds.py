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
