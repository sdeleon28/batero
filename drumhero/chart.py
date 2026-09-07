"""Charts: the notes to play, built-in levels, MIDI file loading, and lane layout."""
import sys
from dataclasses import dataclass, field

import mido

# Instruments the onboarding wizard asks for, in order.
INSTRUMENTS = ["kick", "snare", "hihat", "crash"]
LABELS = {"kick": "Kick", "snare": "Snare", "hihat": "Hi-Hat", "crash": "Crash"}
COLORS = {
    "kick": (245, 90, 90),
    "snare": (250, 170, 60),
    "hihat": (245, 230, 80),
    "crash": (110, 220, 110),
}
# Fallback kit, General MIDI numbers, used until the wizard has run.
DEFAULT_KIT = {"kick": [36, 35], "snare": [38, 40], "hihat": [42, 46, 44], "crash": [49, 57]}

# Chart notes from MIDI files are folded into instruments when they are the GM drum numbers.
GM_TO_INSTRUMENT = {35: "kick", 36: "kick", 38: "snare", 40: "snare",
                    42: "hihat", 44: "hihat", 46: "hihat", 49: "crash", 57: "crash"}
GM_DRUM_NAMES = {
    37: "Side Stick", 39: "Clap", 41: "Floor Tom 2", 43: "Floor Tom", 45: "Low Tom", 47: "Mid Tom",
    48: "High Tom 2", 50: "High Tom", 51: "Ride", 52: "China", 53: "Ride Bell", 54: "Tambourine",
    55: "Splash", 56: "Cowbell", 58: "Vibraslap", 59: "Ride 2",
}
EXTRA_PALETTE = [
    (80, 200, 230), (100, 130, 250), (190, 110, 240), (240, 120, 190), (140, 200, 160), (200, 200, 200),
]


@dataclass
class ChartNote:
    t: float                # seconds from chart start
    key: str                # "kick" / "snare" / "hihat" / "crash" or "n<midi number>"
    velocity: int
    lane: int = -1
    state: str = "pending"  # pending / hit / miss
    judge: str = None       # PERFECT / GOOD / OK / MISS
    error_ms: float = None  # hit time - note time (negative = early)
    sounded: bool = False   # guide sound already played


@dataclass
class Lane:
    index: int
    key: str
    label: str
    color: tuple
    notes: set = field(default_factory=set)   # input note numbers that hit this lane


@dataclass
class Chart:
    name: str
    notes: list
    bpm: float = 120.0
    desc: str = ""

    @property
    def length(self):
        return self.notes[-1].t if self.notes else 0.0


def key_for_note(num: int) -> str:
    return GM_TO_INSTRUMENT.get(num, f"n{num}")


def load_midi_chart(path: str, channel: int = None) -> Chart:
    """Chart from a MIDI file. Honors tempo changes; bpm is the first tempo found."""
    mid = mido.MidiFile(path)
    notes, t, bpm = [], 0.0, None
    for msg in mid:               # iterating a MidiFile yields real-time deltas
        t += msg.time
        if msg.type == "set_tempo" and bpm is None:
            bpm = mido.tempo2bpm(msg.tempo)
        if msg.type == "note_on" and msg.velocity > 0:
            if channel is None or msg.channel == channel:
                notes.append(ChartNote(t, key_for_note(msg.note), msg.velocity))
    if not notes:
        sys.exit(f"No note_on events found in {path}" + (f" on channel {channel + 1}" if channel is not None else ""))
    first = notes[0].t
    for n in notes:               # chart time 0 = first note
        n.t -= first
    name = path.rsplit("/", 1)[-1]
    return Chart(name, notes, bpm or 120.0, "MIDI file")


# ---------------------------------------------------------------------------
# Built-in levels. A pattern is a function bar -> [(beat, key, velocity), ...]
# with beat counted from 0 within a 4/4 bar.
# ---------------------------------------------------------------------------
def _build(name, desc, bpm, bars, pattern) -> Chart:
    beat = 60 / bpm
    notes = []
    for bar in range(bars):
        for b, key, vel in pattern(bar):
            notes.append(ChartNote((bar * 4 + b) * beat, key, vel))
    notes.sort(key=lambda n: (n.t, n.key))
    return Chart(name, notes, bpm, desc)


def _kicks_on_beats(bar):
    return [(b, "kick", 110) for b in range(4)]


def _snare_2_4(bar):
    return [(1, "snare", 115), (3, "snare", 115)]


def _kick_snare(bar):
    return [(0, "kick", 110), (2, "kick", 110), (1, "snare", 115), (3, "snare", 115)]


def _hats_quarters(bar):
    return [(b, "hihat", 85) for b in range(4)]


def _basic_beat(bar):
    return _kick_snare(bar) + _hats_quarters(bar)


def _eighth_beat(bar):
    return _kick_snare(bar) + [(e / 2, "hihat", 85 if e % 2 == 0 else 70) for e in range(8)]


def _crash_beat(bar):
    out = _eighth_beat(bar)
    if bar % 4 == 0:
        out.append((0, "crash", 120))
    return out


def _rock_beat(bar):
    out = _crash_beat(bar)
    if bar % 4 == 3:
        out.append((2.5, "kick", 100))
    if bar % 8 == 7:
        out.append((3.5, "snare", 90))
    return out


LEVELS = [
    _build("1. Kick", "Kick on every beat. Get a feel for the line.", 70, 8, _kicks_on_beats),
    _build("2. Snare", "Snare on 2 and 4.", 70, 8, _snare_2_4),
    _build("3. Kick and snare", "Kick on 1 and 3, snare on 2 and 4.", 80, 8, _kick_snare),
    _build("4. Hi-hat", "Hi-hat on every beat.", 80, 8, _hats_quarters),
    _build("5. Basic beat", "Kick, snare and quarter-note hats together.", 85, 8, _basic_beat),
    _build("6. Eighth-note hats", "Same beat, hats on the eighths.", 95, 12, _eighth_beat),
    _build("7. Crash on the one", "Crash at the start of every four bars.", 100, 16, _crash_beat),
    _build("8. Rock beat", "Kick and snare variations every few bars.", 110, 16, _rock_beat),
    _build("9. Rock beat, faster", "The same beat at 130.", 130, 16, _rock_beat),
]


# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------
def build_lanes(chart: Chart, kit: dict):
    """One lane per distinct key in the chart: instruments first (wizard order), then
    other MIDI numbers ascending. Returns (lanes, input note -> lane index)."""
    keys = {n.key for n in chart.notes}
    ordered = [k for k in INSTRUMENTS if k in keys]
    ordered += sorted((k for k in keys if k not in INSTRUMENTS), key=lambda k: int(k[1:]))
    lanes, by_note, extra = [], {}, 0
    for i, key in enumerate(ordered):
        if key in INSTRUMENTS:
            lane = Lane(i, key, LABELS[key], COLORS[key], set(kit.get(key, [])))
        else:
            num = int(key[1:])
            lane = Lane(i, key, GM_DRUM_NAMES.get(num, f"note {num}"), EXTRA_PALETTE[extra % len(EXTRA_PALETTE)], {num})
            extra += 1
        lanes.append(lane)
        for num in lane.notes:
            by_note.setdefault(num, i)
    index = {k: i for i, k in enumerate(ordered)}
    for n in chart.notes:
        n.lane = index[n.key]
    return lanes, by_note
