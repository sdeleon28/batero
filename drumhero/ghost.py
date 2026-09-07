"""Hi-hat ghost filter, ported from hhmapper's measurements on a Roland TD-17.

Working the pedal shakes the hat and the module sends stick notes that nobody
played. Measured on 2026-09-06 while stomping:
  - ~30 ms BEFORE the chick: note 46 at velocity 9..22 with the pedal still up
  - 3..5 ms AFTER the chick: note 46 at velocity 60..78 with the pedal moving fast
  - up to ~250 ms after: note 42 at velocity 30..36 while the pedal settles
The softest real hi-hat stroke seen was velocity 23.
"""
import time
from collections import deque

HIHAT_STICK_NOTES = {22, 26, 42, 46}   # bow and edge, closed and open
CHICK_NOTES = {44}
PEDAL_CC = 4

HIHAT_MIN_VELOCITY = 25      # softer hi-hat notes are ghosts (also covers most pre-chick ghosts)
CHICK_SPLASH_MS = 60         # hi-hat notes this soon after a chick are ghosts
PEDAL_MOTION_CC = 20         # hi-hat notes are ghosts if the pedal moved at least this much...
PEDAL_MOTION_MS = 50         # ...within this many milliseconds before the note
ANY_MIN_VELOCITY = 8         # below this nothing counts, on any pad

PEDAL_CLOSED_CC = 90         # fully closed on this pedal (0 = fully open)
TIGHT_MIN = 80               # closedness >= this -> tight
OPEN_MAX = 10                # closedness <= this -> open; between -> mid
EDGE_NOTES = {22, 26}


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
        if self.last_chick_t is not None and (t - self.last_chick_t) * 1000 <= CHICK_SPLASH_MS:
            return self._flag("chick splash", t, note, velocity)
        if self.pedal_motion(t) >= PEDAL_MOTION_CC:
            return self._flag("pedal moving", t, note, velocity)
        self.last_stroke = (t, note, velocity, "edge" if note in EDGE_NOTES else "bow", openness_label(self.pedal_cc))
        return None

    def _flag(self, why, t=None, note=None, velocity=None):
        self.filtered += 1
        self.last_reason = why
        self.last_ghost = (time.perf_counter() if t is None else t, note, velocity, why)
        return why
