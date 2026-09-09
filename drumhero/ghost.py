"""Hi-hat ghost filter, ported from hhmapper's measurements on a Roland TD-17.

Working the pedal shakes the hat and the module sends stick notes that nobody
played. Measured on 2026-09-06 while stomping:
  - ~30 ms BEFORE the chick: note 46 at velocity 7..22 with the pedal still up
  - 3..8 ms AFTER the chick: note 46 at velocity 48..94 with the pedal moving fast
  - up to ~250 ms after: note 42 at velocity 22..36 while the pedal settles
Measured 2026-09-07 hitting the edge with the pedal closed: nearly every hard stroke is
followed by one or two bow notes (42, sometimes 46), 42 ms later at 70..76 % of the
stroke's velocity and/or 73..93 ms later at 35..56 %; soft strokes get them rarely.
Re-tuned 2026-09-09 on real playing (paradiddles, fast bow/edge alternation, chick and
stroke together, open hats): the softest real tap read 29; real bow taps land as close
as 44 ms after an edge accent at 63..85 % of it; a stick landing with the chick reads
113..126 within 5 ms of it, or 56..127 as a 42 up to 52 ms later; real strokes while
the pedal is opening read 116..127. Every rule below keeps those.
"""
import time
from collections import deque

HIHAT_STICK_NOTES = {22, 26, 42, 46}   # bow and edge, closed and open
CHICK_NOTES = {44}
PEDAL_CC = 4

HIHAT_MIN_VELOCITY = 25      # softer hi-hat notes are ghosts (also covers the pre-chick ghosts)
CHICK_SPLASH_MS = 10         # hi-hat notes this soon after a chick are ghosts...
CHICK_SPLASH_VELOCITY_MIN = 100  # ...unless this loud: a stick landing with the chick reads 113..126
PEDAL_MOTION_CC = 20         # hi-hat notes are ghosts if the pedal moved at least this much...
PEDAL_MOTION_MS = 50         # ...within this many milliseconds before the note...
PEDAL_MOTION_VELOCITY_MIN = 50   # ...unless this loud: real strokes while opening read 116..127
PEDAL_SETTLE_MS = 250        # closed-hat notes (42/22) this long after a chick...
PEDAL_SETTLE_VELOCITY_MAX = 40   # ...at or below this velocity are the pedal settling (real: >= 56)
ANY_MIN_VELOCITY = 8         # below this nothing counts, on any pad
# A hard stroke on one zone makes the other zone fire late: measured 42 ms after the stroke at
# 70..76 % of its velocity, and 73..93 ms after at 35..56 %. Real bow taps after an edge accent
# come as close as 44 ms at 63..85 %, so only the soft tier is separable; the 42 ms one is
# accepted. (window ms, max velocity ratio) tiers, checked in order.
ZONE_CROSSTALK = [(95, 0.58)]

PEDAL_CLOSED_CC = 90         # fully closed on this pedal (0 = fully open)
TIGHT_MIN = 80               # closedness >= this -> tight
OPEN_MAX = 10                # closedness <= this -> open; between -> mid
EDGE_NOTES = {22, 26}
CLOSED_NOTES = {42, 22}      # what the module sends when its own threshold says closed


def openness_label(cc):
    if cc >= TIGHT_MIN:
        return "tight"
    if cc <= OPEN_MAX:
        return "open"
    return "mid"


class GhostFilter:
    """Feed every MIDI message; ask reason(note, velocity) for note-ons. Thread-safe enough
    for a single MIDI callback thread."""

    def __init__(self):
        self.cc_trail = deque()          # (t, value) samples within PEDAL_MOTION_MS
        self.last_chick_t = None
        self.filtered = 0
        self.last_reason = None
        self.pedal_cc = PEDAL_CLOSED_CC  # assume closed until the pedal speaks
        self.last_stroke = None          # (t, note, velocity, zone, openness) of the last real hi-hat stroke
        self.last_ghost = None           # (t, note, velocity, why)

    def control_change(self, control, value, t=None):
        if control != PEDAL_CC:
            return
        t = time.perf_counter() if t is None else t
        self.pedal_cc = value
        self.cc_trail.append((t, value))

    def pedal_motion(self, t):
        cutoff = t - PEDAL_MOTION_MS / 1000
        while self.cc_trail and self.cc_trail[0][0] < cutoff:
            self.cc_trail.popleft()
        if len(self.cc_trail) < 2:
            return 0
        values = [v for _, v in self.cc_trail]
        return max(values) - min(values)

    def reason(self, note, velocity, t=None):
        """Why this note-on should be ignored, or None if it looks like a real stroke."""
        t = time.perf_counter() if t is None else t
        if note in CHICK_NOTES:
            self.last_chick_t = t
            return None
        if velocity < ANY_MIN_VELOCITY:
            return self._flag("too soft", t, note, velocity)
        if note not in HIHAT_STICK_NOTES:
            return None
        if velocity < HIHAT_MIN_VELOCITY:
            return self._flag("soft hi-hat", t, note, velocity)
        if self.last_chick_t is not None:
            since_chick = (t - self.last_chick_t) * 1000
            if since_chick <= CHICK_SPLASH_MS and velocity < CHICK_SPLASH_VELOCITY_MIN:
                return self._flag("chick splash", t, note, velocity)
            if (since_chick <= PEDAL_SETTLE_MS and velocity <= PEDAL_SETTLE_VELOCITY_MAX
                    and note in CLOSED_NOTES):
                return self._flag("pedal settling", t, note, velocity)
        if self.pedal_motion(t) >= PEDAL_MOTION_CC and velocity < PEDAL_MOTION_VELOCITY_MIN:
            return self._flag("pedal moving", t, note, velocity)
        zone = "edge" if note in EDGE_NOTES else "bow"
        ls = self.last_stroke
        if ls and ls[3] != zone:
            dt = (t - ls[0]) * 1000
            for window_ms, ratio in ZONE_CROSSTALK:
                if dt <= window_ms:
                    if velocity <= ratio * ls[2]:
                        return self._flag("zone crosstalk", t, note, velocity)
                    break
        self.last_stroke = (t, note, velocity, zone, openness_label(self.pedal_cc))
        return None

    def articulation_for(self, note):
        """hhmapper's label for the stroke just accepted: 'tight edge', 'open body', 'pedal chick'...
        None for other pads."""
        if note in CHICK_NOTES:
            return "pedal chick"
        ls = self.last_stroke
        if ls and ls[1] == note:
            return f"{ls[4]} {'edge' if ls[3] == 'edge' else 'body'}"
        return None

    def _flag(self, why, t=None, note=None, velocity=None):
        self.filtered += 1
        self.last_reason = why
        self.last_ghost = (time.perf_counter() if t is None else t, note, velocity, why)
        return why
