"""Charts: the notes to play, built-in levels, MIDI file loading, and lane layout."""
import json
import os
import statistics
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

# Notes that come from the same pad depending on pedal position or zone. When the kit
# wizard hears one of them, it assigns the whole family, so a hi-hat captured with the
# pedal up still counts when it is closed.
NOTE_FAMILIES = [
    {42, 46, 22, 26},    # hi-hat with a stick: bow closed/open, edge closed/open (pedal chick 44 stays separate)
    {49, 55},            # Roland crash 1: bow, edge
    {57, 52},            # Roland crash 2 / china: bow, edge
    {51, 53, 59},        # ride: bow, bell, edge
]


def expand_family(notes):
    out = set(notes)
    for fam in NOTE_FAMILIES:
        if out & fam:
            out |= fam
    return sorted(out)


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
    hand: str = None        # "R" / "L" sticking hint for rudiments, shown on the note


@dataclass
class Lane:
    index: int
    key: str
    label: str
    color: tuple
    notes: set = field(default_factory=set)   # input note numbers that hit this lane


# Counting syllables per subdivision; the first one is replaced by the beat number.
COUNT_LABELS = {1: ["1"], 2: ["1", "&"], 3: ["1", "&", "a"], 4: ["1", "e", "&", "a"]}
GRIDS = [1, 2, 3, 4]            # subdivisions per beat we recognise, coarsest first
GRID_TOLERANCE = 0.12           # of a grid step
GRID_COVERAGE = 0.95            # fraction of onsets that must sit on the grid
PHRASE_BARS = 4                 # subdivision is decided per phrase of this many bars


@dataclass
class Chart:
    name: str
    notes: list
    bpm: float = 120.0
    desc: str = ""
    segments: list = None       # [(start_bar, subdivision), ...] sorted; inferred when None
    beats: list = None          # beat times in chart seconds when the tempo is not constant (songs)
    audio: str = None           # audio file played along (songs)
    audio_offset: float = 0.0   # audio time of chart time 0 (the first charted downbeat)
    sticking: list = None       # ["R", "L", ...] pattern shown as a strip (rudiments)

    @property
    def length(self):
        return self.notes[-1].t if self.notes else 0.0

    @property
    def beat(self):
        return 60 / self.bpm

    @property
    def bars(self):
        return int(self.beat_pos(self.length) // 4) + 1

    # --- beat grid: constant tempo, or the song's own beat times ------------------------
    def beat_pos(self, t: float) -> float:
        """Chart time -> fractional beat index (0 = first downbeat). Extrapolates outside."""
        b = self.beats
        if not b or len(b) < 2:
            return t / self.beat
        if t <= b[0]:
            return (t - b[0]) / (b[1] - b[0])
        if t >= b[-1]:
            return (len(b) - 1) + (t - b[-1]) / (b[-1] - b[-2])
        lo, hi = 0, len(b) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if b[mid] <= t:
                lo = mid
            else:
                hi = mid
        return lo + (t - b[lo]) / (b[hi] - b[lo])

    def beat_time(self, i: float) -> float:
        """Fractional beat index -> chart time. Inverse of beat_pos."""
        b = self.beats
        if not b or len(b) < 2:
            return i * self.beat
        if i <= 0:
            return b[0] + i * (b[1] - b[0])
        if i >= len(b) - 1:
            return b[-1] + (i - (len(b) - 1)) * (b[-1] - b[-2])
        k = int(i)
        return b[k] + (i - k) * (b[k + 1] - b[k])

    def segment_list(self):
        if self.segments is None:
            self.segments = infer_segments(self.notes, self.bpm, beat_pos=self.beat_pos)
        return self.segments

    def subdivision_at(self, t: float) -> int:
        """Subdivisions per beat for chart time t (negative t = count-in uses the first)."""
        bar = int(self.beat_pos(t) // 4)
        sub = self.segment_list()[0][1]
        for start, n in self.segment_list():
            if start <= bar:
                sub = n
            else:
                break
        return sub


def _grid_fits(positions, n):
    """positions: onsets in beats within a phrase. True if nearly all sit on the 1/n grid."""
    if not positions:
        return True
    ok = 0
    for x in positions:
        k = round(x * n)
        if abs(x * n - k) <= GRID_TOLERANCE:
            ok += 1
    return ok / len(positions) >= GRID_COVERAGE


def infer_segments(notes, bpm, phrase_bars=PHRASE_BARS, beat_pos=None):
    """Per phrase, the coarsest grid that covers the onsets. Phrases without notes keep
    the previous subdivision. Consecutive equal phrases merge into one segment.
    beat_pos: chart time -> beat index; default assumes a constant bpm."""
    if beat_pos is None:
        beat_pos = lambda t: t / (60 / bpm)
    if not notes:
        return [(0, 1)]
    phrase_beats = phrase_bars * 4
    positions = [beat_pos(n.t) for n in notes]
    phrases = int(max(positions) / phrase_beats) + 1
    buckets = [[] for _ in range(phrases)]
    for pos in positions:
        i = min(phrases - 1, max(0, int(pos / phrase_beats)))
        buckets[i].append(pos)
    segments, current = [], None
    for i, pos in enumerate(buckets):
        if pos:
            sub = next((g for g in GRIDS if _grid_fits(pos, g)), GRIDS[-1])
        else:
            sub = current if current is not None else 1
        if sub != current:
            segments.append((i * phrase_bars, sub))
            current = sub
    return segments


def key_for_note(num: int) -> str:
    return GM_TO_INSTRUMENT.get(num, f"n{num}")


def load_midi_chart(path: str, channel: int = None, name: str = None, align="first_note") -> Chart:
    """Chart from a MIDI file. Honors tempo changes. The beat grid comes from the tempo
    map, so songs with a tracked tempo keep their real beats. align: "first_note" puts
    chart time 0 on the first note; "zero" keeps the file's own time 0 (songs)."""
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat
    # absolute tick -> seconds through the tempo map (merged tracks)
    tempo_map = []           # (tick, tempo)
    merged = mido.merge_tracks(mid.tracks)
    tick = 0
    for msg in merged:
        tick += msg.time
        if msg.type == "set_tempo":
            tempo_map.append((tick, msg.tempo))
    if not tempo_map or tempo_map[0][0] > 0:
        tempo_map.insert(0, (0, 500000))

    def tick_to_s(x):
        s_acc, last_tick, tempo = 0.0, 0, tempo_map[0][1]
        for tk, tp in tempo_map:
            if tk >= x:
                break
            s_acc += (tk - last_tick) * tempo / 1e6 / tpb
            last_tick, tempo = tk, tp
        return s_acc + (x - last_tick) * tempo / 1e6 / tpb

    notes, tick, end_tick = [], 0, 0
    for msg in merged:
        tick += msg.time
        end_tick = tick
        if msg.type == "note_on" and msg.velocity > 0 and (channel is None or msg.channel == channel):
            notes.append(ChartNote(tick_to_s(tick), key_for_note(msg.note), msg.velocity))
    if not notes:
        sys.exit(f"No note_on events found in {path}" + (f" on channel {channel + 1}" if channel is not None else ""))
    origin = notes[0].t if align == "first_note" else 0.0
    for n in notes:
        n.t -= origin
    beats = [tick_to_s(b * tpb) - origin for b in range(end_tick // tpb + 2)]
    constant = len(tempo_map) == 1
    bpm = mido.tempo2bpm(tempo_map[0][1]) if constant else 60.0 / statistics.median(
        [beats[i + 1] - beats[i] for i in range(len(beats) - 1)] or [0.5])
    return Chart(name or path.rsplit("/", 1)[-1], notes, bpm, "MIDI file", beats=None if constant else beats)


def load_song_folder(folder: str) -> Chart:
    """A song is a folder with song.json (title, audio, offset) and chart.mid, made by ingest."""
    meta = json.load(open(os.path.join(folder, "song.json")))
    chart = load_midi_chart(os.path.join(folder, "chart.mid"), 9, meta.get("title") or os.path.basename(folder), align="zero")
    chart.desc = meta.get("artist", "song")
    audio = meta.get("audio")
    if audio:
        chart.audio = os.path.join(folder, audio)
        chart.audio_offset = float(meta.get("offset", 0.0))
    if meta.get("bpm"):
        chart.bpm = float(meta["bpm"])
    return chart


# ---------------------------------------------------------------------------
# Built-in levels. A pattern is a function bar -> [(beat, key, velocity), ...]
# with beat counted from 0 within a 4/4 bar.
# ---------------------------------------------------------------------------
def _build(name, desc, bpm, bars, pattern, subdivision=None) -> Chart:
    """subdivision: force the metronome grid (per beat); None = infer from the notes."""
    beat = 60 / bpm
    notes = []
    for bar in range(bars):
        for b, key, vel in pattern(bar):
            notes.append(ChartNote((bar * 4 + b) * beat, key, vel))
    notes.sort(key=lambda n: (n.t, n.key))
    return Chart(name, notes, bpm, desc, [(0, subdivision)] if subdivision else None)


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


def _snares_on_beats(bar):
    return [(b, "snare", 110) for b in range(4)]


def _kick_snare_alternating(bar):
    return [(0, "kick", 110), (1, "snare", 110), (2, "kick", 110), (3, "snare", 110)]


def _hats_eighths(bar):
    return [(e / 2, "hihat", 85 if e % 2 == 0 else 70) for e in range(8)]


def _crash_quarters(bar):
    return [(b, "crash", 110) for b in range(4)]


def _rudiment(name, desc, bpm, bars, sticking, sub, accents=(0,), lanes=None):
    """A practice-pad rudiment: `sticking` repeats over the bar at `sub` notes per beat.
    accents: indices within the sticking pattern that get the accent velocity.
    lanes: optional map hand -> instrument key (default: everything on the snare)."""
    beat = 60 / bpm
    lanes = lanes or {"R": "snare", "L": "snare"}
    notes = []
    per_bar = 4 * sub
    for bar in range(bars):
        for i in range(per_bar):
            hand = sticking[i % len(sticking)]
            accent = (i % len(sticking)) in accents
            notes.append(ChartNote((bar * 4 + i / sub) * beat, lanes[hand], 115 if accent else 78, hand=hand))
    ch = Chart(name, notes, bpm, desc, [(0, sub)])
    ch.sticking = list(sticking)
    return ch


RUDIMENTS = [
    _rudiment("Single strokes 8ths", "Alternate hands on the eighths.", 80, 8, "RL", 2),
    _rudiment("Single strokes 16ths", "Alternate hands on the sixteenths, accent on the beat.", 70, 8, "RLRL", 4),
    _rudiment("Paradiddle", "R L R R  L R L L, accent on the first of each group.", 70, 8, "RLRRLRLL", 4, accents=(0, 4)),
    _rudiment("Paradiddle faster", "The same paradiddle at 90.", 90, 12, "RLRRLRLL", 4, accents=(0, 4)),
    _rudiment("Paradiddle hat / snare", "Right hand on the hi-hat, left on the snare.", 75, 8, "RLRRLRLL", 4, accents=(0, 4),
              lanes={"R": "hihat", "L": "snare"}),
    _rudiment("Triplets", "Eighth-note triplets, alternating, accent on the beat.", 70, 8, "RLRLRL", 3, accents=(0, 3)),
    _rudiment("Triplets R L L", "R L L on every beat: the shuffle hand pattern.", 70, 8, "RLL", 3),
    _rudiment("Double paradiddle", "R L R L R R  L R L R L L in triplets.", 70, 8, "RLRLRRLRLRLL", 3, accents=(0, 6)),
    _rudiment("Paradiddle-diddle", "R L R R L L in triplets, accent on the first.", 75, 8, "RLRRLL", 3, accents=(0,)),
    _rudiment("Doubles 16ths", "R R L L on the sixteenths.", 70, 8, "RRLL", 4, accents=(0,)),
]


# Exercises: one or two drums, slow. Beats: full grooves.
EXERCISES = [
    _build("Kick on the beat", "Kick on every beat. Get a feel for the line.", 70, 8, _kicks_on_beats),
    _build("Snare on the beat", "Snare on every beat.", 70, 8, _snares_on_beats),
    _build("Snare on 2 and 4", "Only the backbeat.", 70, 8, _snare_2_4),
    _build("Hi-hat on the beat", "Hi-hat on every beat.", 80, 8, _hats_quarters),
    _build("Crash on the beat", "Crash on every beat, let it ring.", 70, 4, _crash_quarters),
    _build("Kick and snare", "Kick on 1 and 3, snare on 2 and 4.", 80, 8, _kick_snare),
    _build("Alternating", "Kick, snare, kick, snare.", 85, 8, _kick_snare_alternating),
    _build("Hi-hat eighths", "Hi-hat on every eighth note.", 90, 8, _hats_eighths),
]
BEATS = [
    _build("Basic beat", "Kick, snare and quarter-note hats together.", 85, 8, _basic_beat),
    _build("Eighth-note hats", "Same beat, hats on the eighths.", 95, 12, _eighth_beat),
    _build("Crash on the one", "Crash at the start of every four bars.", 100, 16, _crash_beat),
    _build("Rock beat", "Kick and snare variations every few bars.", 110, 16, _rock_beat),
    _build("Rock beat, faster", "The same beat at 130.", 130, 16, _rock_beat),
]
EXERCISES = EXERCISES + RUDIMENTS
LEVELS = EXERCISES + BEATS


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
