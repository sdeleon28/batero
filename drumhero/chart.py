"""Charts: the notes to play, built-in levels, MIDI file loading, and lane layout."""
import json
import os
import re
import statistics
import sys
import dataclasses
from dataclasses import dataclass, field

import mido

# --- the kit -------------------------------------------------------------------------
# A zone is one strikeable part of the TD-17 (snare head, snare rim, ride bell, ...).
# The wizard asks for every zone in this order, so the saved kit knows the whole kit even
# though lessons only use a few instruments so far. Head/bow zones come before rim/edge
# zones on purpose: a note heard in two steps goes to the later one.
@dataclass(frozen=True)
class Zone:
    key: str
    label: str          # "Snare rim"
    pad: str            # the physical pad: "Snare"
    part: str           # "head" / "rim" / "bow" / "edge" / "bell" / "pedal"
    instrument: str     # chart key these hits count for
    prompt: str         # wizard instruction
    defaults: tuple     # General MIDI / TD-17 factory note numbers


ZONES = [
    Zone("kick", "Kick", "Kick", "", "kick", "Hit the KICK a few times", (36, 35)),
    Zone("snare", "Snare head", "Snare", "head", "snare", "Hit the SNARE head a few times", (38,)),
    Zone("snare_rim", "Snare rim", "Snare", "rim", "snare", "Hit the SNARE RIM: rimshots and cross-stick", (40, 37)),
    Zone("hihat", "Hi-hat bow", "Hi-hat", "bow", "hihat", "Hit the HI-HAT on top (bow), pedal up and down", (42, 46)),
    Zone("hihat_edge", "Hi-hat edge", "Hi-hat", "edge", "hihat", "Hit the HI-HAT EDGE, pedal up and down", (22, 26)),
    Zone("hihat_pedal", "Hi-hat pedal", "Hi-hat", "pedal", "pedal", "Stomp the HI-HAT PEDAL a few times (chick)", (44,)),
    Zone("crash", "Crash L bow", "Crash L", "bow", "crash", "Hit the LEFT CRASH on the bow", (49,)),
    Zone("crash_edge", "Crash L edge", "Crash L", "edge", "crash", "Hit the LEFT CRASH on the edge", (55,)),
    Zone("crash2", "Crash R bow", "Crash R", "bow", "crash2", "Hit the RIGHT CRASH on the bow", (57,)),
    Zone("crash2_edge", "Crash R edge", "Crash R", "edge", "crash2", "Hit the RIGHT CRASH on the edge", (52,)),
    Zone("tom1", "Rack tom head", "Rack tom", "head", "tom1", "Hit the RACK TOM head", (48,)),
    Zone("tom1_rim", "Rack tom rim", "Rack tom", "rim", "tom1", "Hit the RACK TOM RIM", (50,)),
    Zone("floor", "Floor tom head", "Floor tom", "head", "floor", "Hit the FLOOR TOM head", (43, 45)),
    Zone("floor_rim", "Floor tom rim", "Floor tom", "rim", "floor", "Hit the FLOOR TOM RIM", (58, 47)),
    Zone("ride", "Ride bow", "Ride", "bow", "ride", "Hit the RIDE on the bow", (51,)),
    Zone("ride_edge", "Ride edge", "Ride", "edge", "ride", "Hit the RIDE on the edge", (59,)),
    Zone("ride_bell", "Ride bell", "Ride", "bell", "ride", "Hit the RIDE BELL", (53,)),
]
ZONE = {z.key: z for z in ZONES}
ZONE_KEYS = [z.key for z in ZONES]
PADS = []                       # [(pad name, [zone keys]), ...] in wizard order
for _z in ZONES:
    if not PADS or PADS[-1][0] != _z.pad:
        PADS.append((_z.pad, []))
    PADS[-1][1].append(_z.key)

# Instruments: what charts refer to. The first four also drive the menus.
INSTRUMENTS = ["kick", "snare", "hihat", "crash", "tom1", "floor", "ride", "crash2", "pedal"]
LABELS = {"kick": "Kick", "snare": "Snare", "hihat": "Hi-Hat", "crash": "Crash L",
          "tom1": "Rack tom", "floor": "Floor tom", "ride": "Ride", "crash2": "Crash R", "pedal": "HH pedal"}
COLORS = {
    "kick": (245, 90, 90),
    "snare": (250, 170, 60),
    "hihat": (245, 230, 80),
    "crash": (110, 220, 110),
    "tom1": (80, 200, 230),
    "floor": (100, 130, 250),
    "ride": (190, 110, 240),
    "crash2": (60, 190, 150),
    "pedal": (220, 200, 120),
}
# Hi-hat articulations, named exactly as hhmapper labels them (and GetGood Drums plays them):
# openness from the pedal (CC4) x zone, plus the foot.
HH_ARTS = ["tight body", "tight edge", "mid body", "mid edge", "open body", "open edge", "pedal chick"]
HH_GLYPH = {"tight body": "+", "tight edge": ">+", "mid body": "/", "mid edge": ">/", "open body": "o", "open edge": ">o",
            "pedal chick": "^"}
# Charts that use one crash accept either crash pad; only charts with both lanes tell them apart.
CRASH_PAIR = {"crash": "crash2", "crash2": "crash"}
INSTRUMENT_ZONES = {inst: [z.key for z in ZONES if z.instrument == inst] for inst in INSTRUMENTS}
# Fallback kit, General MIDI / TD-17 factory numbers, used until the wizard has run.
DEFAULT_KIT = {z.key: list(z.defaults) for z in ZONES}


def kit_notes(kit: dict, instrument: str):
    """Every input note number of an instrument across its zones."""
    out = set()
    for zk in INSTRUMENT_ZONES.get(instrument, []):
        out.update(kit.get(zk, []))
    return sorted(out)


# Notes that come from the same zone depending on pedal position. When the kit wizard
# hears one of them, it assigns the pair, so a hi-hat captured with the pedal up still
# counts when it is closed.
NOTE_FAMILIES = [
    {42, 46},    # hi-hat bow: closed / open
    {22, 26},    # hi-hat edge: closed / open
]


def expand_family(notes):
    out = set(notes)
    for fam in NOTE_FAMILIES:
        if out & fam:
            out |= fam
    return sorted(out)


# Chart notes from MIDI files are folded into instruments when they are the GM drum numbers.
GM_TO_INSTRUMENT = {35: "kick", 36: "kick", 37: "snare", 38: "snare", 40: "snare",
                    22: "hihat", 26: "hihat", 42: "hihat", 44: "pedal", 46: "hihat",
                    49: "crash", 55: "crash", 52: "crash2", 57: "crash2",
                    47: "tom1", 48: "tom1", 50: "tom1", 41: "floor", 43: "floor", 45: "floor", 58: "floor",
                    51: "ride", 53: "ride", 59: "ride"}
GM_DRUM_NAMES = {
    37: "Side Stick", 39: "Clap", 41: "Floor Tom 2", 43: "Floor Tom", 45: "Low Tom", 47: "Mid Tom",
    48: "High Tom 2", 50: "High Tom", 51: "Ride", 52: "China", 53: "Ride Bell", 54: "Tambourine",
    55: "Splash", 56: "Cowbell", 58: "Vibraslap", 59: "Ride 2",
}
EXTRA_PALETTE = [(240, 120, 190), (140, 200, 160), (200, 200, 200), (230, 200, 120)]


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
    strip: int = None       # index of this stroke in the chart's sticking strip (lit when played)
    accent: bool = False    # an accented stroke (judged when the chart has dynamics)
    hit_velocity: int = None
    dyn: str = None         # ACCENT / TAP (right) or SOFT / LOUD (wrong), set when hit
    art: str = None         # required hi-hat articulation (HH_ARTS), judged when the chart has expression
    art_ok: bool = None     # set when hit: the stroke's articulation matched
    played: str = None      # the articulation actually played


@dataclass
class Lane:
    index: int
    key: str
    label: str
    color: tuple
    notes: set = field(default_factory=set)   # input note numbers that hit this lane


# Counting syllables per subdivision; the first one is replaced by the beat number.
COUNT_LABELS = {1: ["1"], 2: ["1", "&"], 3: ["1", "&", "a"], 4: ["1", "e", "&", "a"], 6: ["1", "2", "3", "4", "5", "6"]}
GRIDS = [1, 2, 3, 4]            # subdivisions per beat we recognise, coarsest first
GRID_TOLERANCE = 0.12           # of a grid step
GRID_COVERAGE = 0.95            # fraction of onsets that must sit on the grid
PHRASE_BARS = 4                 # subdivision is decided per phrase of this many bars
PHRASE_KEYS = 10                # transport: the number keys 1..9 and 0, one phrase each


@dataclass
class Chart:
    name: str
    notes: list
    bpm: float = 120.0
    desc: str = ""
    segments: list = None       # [(start_bar, subdivision), ...] sorted; inferred when None.
                                # start_bar may be fractional (0.5 = beat 3) for mixed rudiments
    beats: list = None          # beat times in chart seconds when the tempo is not constant (songs)
    audio: str = None           # audio file played along (songs)
    audio_offset: float = 0.0   # audio time of chart time 0 (the first charted downbeat)
    sticking: list = None       # ["R", "L", ...] pattern shown as a strip (rudiments)
    accents: set = None         # indices within the sticking pattern that are accented
    sticking_groups: list = None  # indices where a new cell of the pattern starts (strip separators)
    dynamics: bool = False      # judge accents vs taps by velocity
    expression: bool = False    # judge hi-hat articulations (openness, zone, chick)
    rate: float = 1.0           # tempo multiplier this chart was scaled by (see at_rate)
    lead: str = None            # "R" / "L": which hand leads; None when the level has no hand lead
                                # (one instrument per hand, feet, hi-hat lessons). Set by the builders.
    backing: str = None         # a backing of its own ("cumbia": sounds.CUMBIA_STYLE, "keygen": the waiting screen's tune); None = by subdivision

    @property
    def key(self):
        """The progress / run-log name: the level name, plus " (L)" for the left-hand-lead version."""
        return self.name + (" (L)" if self.lead == "L" else "")

    @property
    def title(self):
        return self.name + ("  ·  left hand lead" if self.lead == "L" else "")

    def mirrored(self):
        """The same level led by the other hand: every R becomes L and vice versa, in the
        notes, the sticking strip and the description's R / L tokens. Same name, other key."""
        if not self.lead:
            return self
        swap = {"R": "L", "L": "R"}
        notes = [dataclasses.replace(n, hand=swap.get(n.hand, n.hand)) for n in self.notes]
        desc = re.sub(r"\b[RL]\b", lambda m: swap[m.group(0)], self.desc)
        return dataclasses.replace(self, notes=notes, desc=desc, lead=swap[self.lead],
                                   sticking=[swap.get(h, h) for h in self.sticking] if self.sticking else None)

    def at_rate(self, rate: float):
        """A copy of this chart played at `rate` times the tempo: note times, beat grid and
        audio offset stretched, bpm scaled. rate 0.5 = half speed."""
        rate = float(rate)
        if rate == 1.0:
            return self
        notes = [dataclasses.replace(n, t=n.t / rate) for n in self.notes]
        ch = dataclasses.replace(self, notes=notes, bpm=self.bpm * rate, rate=rate,
                                 beats=[b / rate for b in self.beats] if self.beats else None,
                                 audio_offset=self.audio_offset / rate)
        return ch

    @property
    def length(self):
        return self.notes[-1].t if self.notes else 0.0

    def doubles(self):
        """The doubles: runs of two or three consecutive strokes of one hand (or foot) on one
        instrument, each within a beat of the last, as (first index, last index) into notes.
        The highway brackets them on the hand's side. Notes without a hand are not strokes and
        are skipped over; a longer run is a one-hand exercise, not a double, and gets nothing."""
        out, run = [], []
        beat = 60 / self.bpm

        def flush():
            if 2 <= len(run) <= 3:
                out.append((run[0], run[-1]))

        for i, n in enumerate(self.notes):
            if not n.hand:
                continue
            if run:
                p = self.notes[run[-1]]
                if p.hand != n.hand or p.key != n.key or n.t - p.t > beat * 1.01:
                    flush()
                    run = []
            run.append(i)
        flush()
        return out

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

    def phrase_bounds(self, count=PHRASE_KEYS):
        """The transport's markers: the level cut into at most `count` phrases of whole bars,
        plus the end, so phrase i (0-based) is [out[i], out[i + 1]) and there are
        len(out) - 1 of them, one per number key from 1. Phrases are equal whenever the
        level allows it: a level of up to ten bars has one bar per key (8 bars, keys 1..8),
        a longer one takes the largest count in 10..5 that divides it exactly (16 bars are 8
        of 2, 12 are 6 of 2, 120 are 10 of 12), and only a length nothing divides (83 bars)
        gets ten of nearly equal size. Keys past the last phrase do nothing."""
        cached = getattr(self, "_phrase_cache", None)
        if cached is not None and cached[0] == count:
            return cached[1]
        bars = max(1, self.bars)
        if bars <= count:
            n = bars
        else:
            n = next((k for k in range(count, 4, -1) if bars % k == 0), count)
        marks = sorted({round(i * bars / n) for i in range(n)})
        out = [self.beat_time(b * 4) for b in marks] + [self.beat_time(bars * 4)]
        self._phrase_cache = (count, out)
        return out

    def phrase_at(self, t: float, count=PHRASE_KEYS):
        """Index of the phrase holding chart time t (the first one during the count-in);
        None past the end of the level."""
        bounds = self.phrase_bounds(count)
        if t >= bounds[-1]:
            return None
        for i in range(len(bounds) - 1, 0, -1):
            if t >= bounds[i - 1]:
                return i - 1
        return 0

    def place(self, t: float) -> str:
        """'bar 3' or 'bar 3 beat 3': where chart time t falls, 1-based, for the transport."""
        pos = self.beat_pos(max(t, 0.0)) + 1e-6          # beat_time and beat_pos round-trip a hair short
        bar, beat = int(pos // 4) + 1, int(pos % 4) + 1
        return f"bar {bar}" if beat == 1 else f"bar {bar} beat {beat}"

    def segment_list(self):
        if self.segments is None:
            self.segments = infer_segments(self.notes, self.bpm, beat_pos=self.beat_pos)
        return self.segments

    def subdivision_at(self, t: float) -> int:
        """Subdivisions per beat for chart time t (negative t = count-in uses the first).
        Segments may start on a fraction of a bar, so this can change from beat to beat."""
        pos = self.beat_pos(t) / 4 + 1e-6       # bars; the epsilon absorbs beat_time rounding
        sub = self.segment_list()[0][1]
        for start, n in self.segment_list():
            if start <= pos:
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


def _rudiment(name, desc, bpm, bars, sticking, sub, accents=(0,), lanes=None, backing=None):
    """A practice-pad rudiment: `sticking` repeats over the bar at `sub` notes per beat.
    accents: indices within the sticking pattern that are accented; the chart then judges
    dynamics (accent vs tap velocity). lanes: optional map hand -> instrument key
    (default: everything on the snare). backing: see Chart.backing."""
    beat = 60 / bpm
    lanes = lanes or {"R": "snare", "L": "snare"}
    notes = []
    per_bar = 4 * sub
    for bar in range(bars):
        for i in range(per_bar):
            hand = sticking[i % len(sticking)]
            accent = (i % len(sticking)) in accents
            notes.append(ChartNote((bar * 4 + i / sub) * beat, lanes[hand], ACCENT_VELOCITY if accent else TAP_VELOCITY,
                                   hand=hand, accent=accent, strip=i % len(sticking)))
    ch = Chart(name, notes, bpm, desc, [(0, sub)])
    ch.sticking = list(sticking)
    ch.accents = set(accents)
    ch.dynamics = bool(accents)
    ch.lead = _lead_of(sticking, lanes)
    ch.backing = backing
    return ch


def _lead_of(sticking, lanes):
    """"R" when the level can be played led by either hand: both hands appear and land on the
    same instrument. A hands-on-different-drums level has one version only."""
    return "R" if {"R", "L"} <= set(sticking) and len(set(lanes.values())) == 1 else None


def _swap(sticking):
    return sticking.translate(str.maketrans("RL", "LR"))


def _reversed(cells):
    return [(_swap(s), sub, acc) for s, sub, acc in cells]


def _rudiment_mix(name, desc, bpm, bars, phrase, lanes=None, backing=None):
    """A rudiment whose beats can differ in subdivision. phrase: cells (sticking, sub,
    accents), each lasting len(sticking) / sub beats, repeated to `bars`. The chart's
    segments carry the subdivision per cell (fractional bar starts), so the count panel,
    the congas and the sticking strip follow every switch. backing: see Chart.backing."""
    beat = 60 / bpm
    lanes = lanes or {"R": "snare", "L": "snare"}
    phrase_beats = sum(len(s) / sub for s, sub, _ in phrase)
    assert abs(phrase_beats - round(phrase_beats)) < 1e-9 and (4 * bars) % round(phrase_beats) == 0, (name, phrase_beats)
    notes, segments, sticking, accents, groups = [], [], [], set(), []
    pos = 0.0                                   # in beats
    for rep_i in range(int(4 * bars // round(phrase_beats))):
        for j, (s, sub, acc) in enumerate(phrase):
            if rep_i == 0:
                groups.append(len(sticking))
                accents |= {len(sticking) + a for a in acc}
                sticking += list(s)
            base = groups[j]                        # this cell's first index in the strip
            if not segments or segments[-1][1] != sub:
                segments.append((pos / 4, sub))
            for i, hand in enumerate(s):
                accent = i in acc
                notes.append(ChartNote((pos + i / sub) * beat, lanes[hand], ACCENT_VELOCITY if accent else TAP_VELOCITY,
                                       hand=hand, accent=accent, strip=base + i))
            pos += len(s) / sub
    ch = Chart(name, notes, bpm, desc, segments)
    ch.sticking, ch.accents, ch.dynamics = sticking, accents, True
    ch.sticking_groups = groups[1:]
    ch.lead = _lead_of(sticking, lanes)
    ch.backing = backing
    return ch


# Chart velocities for accented and unaccented strokes; also what the guide plays.
ACCENT_VELOCITY = 115
TAP_VELOCITY = 70


# cells for the mixed rudiments: (sticking, subdivision, accented indices)
_SINGLES = ("RLRL", 4, (0,))
_PD, _PD_L = ("RLRR", 4, (0,)), ("LRLL", 4, (0,))
_SSR, _SSR_L = ("RLLRRL", 6, (0, 5)), ("LRRLLR", 6, (0, 5))
_DP, _DP_L = ("RLRLRR", 6, (0,)), ("LRLRLL", 6, (0,))
_PDD = ("RLRRLL", 6, (0,))
_PPSP = [_PD, _PD_L, _SSR, _PD]           # paradiddle, paradiddle, six stroke roll, paradiddle

RUDIMENTS = [
    _rudiment("Single strokes 8ths", "Alternate hands on the eighths.", 80, 8, "RL", 2),
    _rudiment("Single strokes 16ths", "Alternate hands on the sixteenths, accent on the beat.", 70, 8, "RLRL", 4),
    _rudiment("Paradiddle", "R L R R  L R L L, accent on the first of each group.", 70, 8, "RLRRLRLL", 4, accents=(0, 4)),
    _rudiment("Paradiddle hat / snare", "Right hand on the hi-hat, left on the snare.", 75, 8, "RLRRLRLL", 4, accents=(0, 4),
              lanes={"R": "hihat", "L": "snare"}),
    _rudiment("Triplets", "Eighth-note triplets, alternating, accent on the beat.", 70, 8, "RLRLRL", 3, accents=(0, 3)),
    _rudiment("Triplets R L L", "R L L on every beat: the shuffle hand pattern.", 70, 8, "RLL", 3),
    _rudiment("Double paradiddle", "R L R L R R  L R L R L L in triplets.", 70, 8, "RLRLRRLRLRLL", 3, accents=(0, 6)),
    _rudiment("Paradiddle-diddle", "R L R R L L in triplets, accent on the first.", 75, 8, "RLRRLL", 3, accents=(0,)),
    # six stroke roll: R L L R R L, the two singles accented, the doubles soft
    _rudiment("Six stroke roll in triplets", "R L L R R L over two beats of triplets: accent the singles, keep the doubles soft.",
              70, 8, "RLLRRL", 3, accents=(0, 5)),
    _rudiment("Six stroke roll", "The same six strokes inside one beat: a sextuplet, accents on the first and the last.",
              60, 8, "RLLRRL", 6, accents=(0, 5), backing="keygen"),
    _rudiment("Six stroke roll R L R R L L", "Singles first: the two accents land together, then the two doubles.",
              60, 8, "RLRRLL", 6, accents=(0, 1), backing="keygen"),
    # combinations: the six stroke roll inside sixteenth flow or next to other sextuplet rudiments.
    # Tempo is one number each; the game's rate control is the speed ladder.
    _rudiment_mix("Sixteenths + six stroke roll", "Single strokes on the sixteenths, a six stroke roll as a sextuplet on beat 4.",
                  60, 8, [_SINGLES, _SINGLES, _SINGLES, _SSR]),
    _rudiment_mix("Paradiddle + six stroke roll", "Paradiddle on the sixteenths, six stroke roll as a sextuplet, alternating beats and hands: the subdivision switches on every beat.",
                  60, 8, [_PD, _SSR_L, _PD_L, _SSR]),
    _rudiment_mix("2 paradiddles, six stroke, 1 more", "Two paradiddles, a six stroke roll as a sextuplet, one more paradiddle: the bar ends on the right, so the next one starts on the left and the whole thing plays reversed.",
                  60, 8, _PPSP + _reversed(_PPSP)),
    _rudiment_mix("Double paradiddle + six stroke", "All sextuplets: a double paradiddle, then a six stroke roll, alternating beats and hands.",
                  60, 8, [_DP, _SSR_L, _DP_L, _SSR], backing="keygen"),
    _rudiment_mix("Six stroke + paradiddle-diddle", "All sextuplets: the same six strokes with the doubles in two different places, one beat each.",
                  60, 8, [_SSR, _PDD, _SSR, _PDD], backing="keygen"),
    _rudiment("Doubles 16ths", "R R L L on the sixteenths.", 70, 8, "RRLL", 4, accents=(0,)),
    # accent control: same hands, the accent walks through the sixteenth
    _rudiment("Accent on 1", "Sixteenths, accent on the beat, taps in between.", 70, 8, "RLRL", 4, accents=(0,)),
    _rudiment("Accent on e", "Sixteenths, accent on the e.", 70, 8, "RLRL", 4, accents=(1,)),
    _rudiment("Accent on &", "Sixteenths, accent on the &.", 70, 8, "RLRL", 4, accents=(2,)),
    _rudiment("Accent on a", "Sixteenths, accent on the a.", 70, 8, "RLRL", 4, accents=(3,)),
    _rudiment("Moving accent", "The accent walks: 1, then e, then &, then a, one per beat.", 70, 8,
              "RLRLRLRLRLRLRLRL", 4, accents=(0, 5, 10, 15)),
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
# --- grooves: a small notation --------------------------------------------------------
# One string per instrument per bar, sixteen slots (sixteenths): "x" a stroke, "X" an
# accent, "o" a ghost, "." nothing. Bars are dicts; a level is a list of bars, its
# phrase, repeated to the level length.
GROOVE_KEYS = {"hh": "hihat", "kk": "kick", "sn": "snare", "t1": "tom1", "ft": "floor",
               "rd": "ride", "cl": "crash", "cr": "crash2", "pd": "pedal"}
GROOVE_VEL = {"X": 120, "x": 96, "o": 62}      # only X draws as an accent
# In the "hh" string these letters ask for an articulation (bow taps soft, edge strokes hard);
# "x"/"X"/"o" stay "any hi-hat". In "pd", "x" is a chick.
HH_LETTERS = {"t": "tight body", "T": "tight edge", "m": "mid body", "M": "mid edge", "a": "open body", "A": "open edge"}


def _groove(name, desc, bpm, phrase, bars=8, backing=None):
    """A groove level from `phrase` (list of bar dicts, see GROOVE_KEYS) repeated to `bars`.
    backing: a style of its own for the music (sounds.make_arrangement's feel), else generic."""
    beat = 60 / bpm
    notes = []
    for bar in range(bars):
        for k, pat in phrase[bar % len(phrase)].items():
            key = GROOVE_KEYS[k]
            assert len(pat) == 16, (name, k, pat)
            for i, c in enumerate(pat):
                t = (bar * 4 + i / 4) * beat
                if k == "pd" and c in GROOVE_VEL:
                    notes.append(ChartNote(t, "pedal", GROOVE_VEL[c], art="pedal chick"))
                elif k == "hh" and c in HH_LETTERS:
                    art = HH_LETTERS[c]
                    notes.append(ChartNote(t, "hihat", 110 if "edge" in art else 88, accent="edge" in art, art=art))
                elif c in GROOVE_VEL:
                    notes.append(ChartNote(t, key, GROOVE_VEL[c], accent=(c == "X")))
    notes.sort(key=lambda n: (n.t, INSTRUMENTS.index(n.key)))
    ch = Chart(name, notes, bpm, desc)
    ch.expression = any(n.art for n in notes)
    ch.backing = backing
    return ch


_H8 = "x.x.x.x.x.x.x.x."          # hats on the eighths
_H4 = "x...x...x...x..."          # hats on the quarters
_H16 = "xxxxxxxxxxxxxxxx"         # hats on the sixteenths
_S24 = "....X.......X..."         # backbeat

# The curriculum: every level keeps what the one before taught and adds one idea.
BEATS = [
    _groove("1 · Money beat", "Kick on 1 and 3, snare on 2 and 4, hats on the quarters.", 85, [
        {"hh": _H4, "kk": "x.......x.......", "sn": _S24},
    ]),
    _groove("2 · Eighth-note hats", "The same beat with the hats on every eighth: the right hand keeps time.", 90, [
        {"hh": _H8, "kk": "x.......x.......", "sn": _S24},
    ]),
    _groove("3 · Kick on the &", "A second kick on the & of 3, then on the & of 1 in the answering bar.", 90, [
        {"hh": _H8, "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": _S24},
    ]),
    _groove("4 · Four-bar phrase", "Three bars of groove, then a bar with a snare pickup on the & of 4: the phrase has a shape.", 92, [
        {"hh": _H8, "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": _S24},
        {"hh": _H8, "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": "....X.......X.x."},
    ]),
    _groove("5 · Crash on the one", "The left crash replaces the hat on the 1 that starts each phrase.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": _S24},
        {"hh": _H8, "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": "....X.......X.x."},
    ]),
    _groove("6 · First fill", "Bar 4 ends with snare sixteenths on beat 4, straight into the crash.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": _S24},
        {"hh": _H8, "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "kk": "x.x.....x.......", "sn": "....X.......xxxx"},
    ]),
    _groove("7 · Rack tom enters", "The fill moves: two snares, two rack toms. The tom lane is new.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": _S24},
        {"hh": _H8, "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "kk": "x.x.....x.......", "sn": "....X.......xx..", "t1": "..............xx"},
    ]),
    _groove("8 · Floor tom enters", "The fill walks down snare, rack tom, floor tom over beats 3 and 4.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": _S24},
        {"hh": _H8, "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "x.x.x.x.........", "kk": "x.x.....x.......", "sn": "....X...xxxx....", "t1": "............xx..", "ft": "..............xx"},
    ]),
    _groove("9 · Toms in the groove", "The floor tom takes over the hat's job on beats 1 and 3, the rack tom answers on the & of 4.", 88, [
        {"cl": "x...............", "ft": "..x.....x.x.....", "hh": "....x.x.....x.x.", "kk": "x.......x.......", "sn": _S24},
        {"ft": "x.x.....x.x.....", "hh": "....x.x.....x.x.", "kk": "x.......x.......", "sn": _S24, "t1": "..............x."},
        {"ft": "x.x.....x.x.....", "hh": "....x.x.....x.x.", "kk": "x.......x.......", "sn": _S24},
        {"ft": "x.x.....x.x.....", "hh": "....x.x.........", "kk": "x.......x.......", "sn": "....X.......x.x.", "t1": "............x.x."},
    ]),
    _groove("10 · Ride", "The right hand moves to the ride for the second half of the phrase, crash on the way in.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": _H8, "kk": "x.x.....x.......", "sn": _S24},
        {"cl": "x...............", "rd": "..x.x.x.x.x.x.x.", "kk": "x.......x.x.....", "sn": _S24},
        {"rd": "x.x.x.x.x.x.....", "kk": "x.x.....x.......", "sn": "....X.......xx..", "t1": "..............xx"},
    ]),
    _groove("11 · Two crashes", "Left crash opens the phrase, right crash answers on beat 3 of bar 4 and closes it.", 92, [
        {"cl": "x...............", "rd": "..x.x.x.x.x.x.x.", "kk": "x.......x.x.....", "sn": _S24},
        {"rd": _H8, "kk": "x.x.....x.......", "sn": _S24},
        {"rd": _H8, "kk": "x.......x.x.....", "sn": _S24},
        {"cr": "........x.......", "rd": "x.x.x.x.........", "kk": "x.x.....x.......", "sn": "....X...xx......", "t1": "..........xx....", "ft": "............xx.."},
    ], bars=8),
    _groove("12 · Half time", "Snare on 3 only, kick on 1 and the & of 2: twice the space, same phrase shape.", 80, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.....x.........", "sn": "........X......."},
        {"hh": _H8, "kk": "x.....x...x.....", "sn": "........X......."},
        {"hh": _H8, "kk": "x.....x.........", "sn": "........X......."},
        {"hh": "x.x.x.x.........", "kk": "x.....x.........", "sn": "........X...xx..", "t1": "..............xx", "ft": "................"},
    ]),
    _groove("13 · Sixteenth kicks", "Kicks land on the e and the a: the funk pocket under the hats.", 88, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": _S24},
        {"hh": _H8, "kk": "x.........x..x..", "sn": _S24},
        {"hh": _H8, "kk": "x..x......x.x...", "sn": _S24},
        {"hh": _H8, "kk": "x.........x..x..", "sn": "....X.......X.ox"},
    ]),
    _groove("14 · Ghost notes", "Soft snares on the e of 2 and the a of 3 between the backbeats.", 88, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x.........x..x..", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x.........x..x..", "sn": "....Xo.....oX.ox"},
    ]),
    _groove("15 · Sixteenth hats", "One hand plays every sixteenth on the hats while the funk pocket stays put.", 76, [
        {"cl": "x...............", "hh": ".xxxxxxxxxxxxxxx", "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": _H16, "kk": "x.........x..x..", "sn": "....Xo.....oX..."},
        {"hh": _H16, "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": "xxxxxxxxxxxx....", "kk": "x.........x..x..", "sn": "....Xo.....oX...", "t1": "............xx..", "ft": "..............xx"},
    ]),
    _groove("16 · Linear", "Nothing lands together: hats, kick and snare take turns, toms and ride fill the gaps.", 86, [
        {"cl": "x...............", "hh": ".x.x..x..x.x..x.", "kk": "x...x......x....", "sn": "....X.......X..."},
        {"hh": ".x.x..x..x.x..x.", "kk": "x...x......x....", "sn": "....X.......X...", "t1": "..............x."},
        {"rd": ".x.x..x..x.x..x.", "kk": "x...x......x....", "sn": "....X.......X..."},
        {"rd": ".x.x..x.........", "kk": "x...x......x....", "sn": "....X...x.x.....", "t1": ".........x.x....", "ft": "..........x.x.x.", "cr": "...............x"},
    ]),
    _groove("17 · Song form", "A A B A: hats verses, a ride bridge with tom hits, two crashes, ghost notes, the works.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x.........x..x..", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": "x.x.x.x.x.x.....", "kk": "x.........x..x..", "sn": "....Xo.....oX.xx"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x.........x..x..", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": "x.x.x.x.........", "kk": "x.........x..x..", "sn": "....X...xx......", "t1": "..........xx....", "ft": "............xx.."},
        {"cr": "x...............", "rd": "..x.x.x.x.x.x.x.", "kk": "x.....x.........", "sn": "........X.......", "ft": "..........x....."},
        {"rd": _H8, "kk": "x.....x...x.....", "sn": "........X.......", "t1": "..............x."},
        {"rd": _H8, "kk": "x.....x.........", "sn": "........X.......", "ft": "..........x....."},
        {"rd": "x.x.x.x.........", "kk": "x.....x.........", "sn": "........X...x...", "t1": ".............x..", "ft": "..............xx"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x.........x..x..", "sn": "....Xo.....oX..."},
        {"hh": _H8, "kk": "x..x......x.x...", "sn": "....Xo.....oX..."},
        {"cr": "..............x.", "hh": "x.x.x.x.x.x.....", "kk": "x.........x..x..", "sn": "....X.......xxx.", "cl": "...............x"},
    ], bars=16),
    # --- rock, metal, punk: beats and fills ---
    _groove("18 · Fill vocabulary", "A different fill every two bars: snare sixteenths, snare-tom pairs, the walk down, then all four drums.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "kk": "x.........x.....", "sn": "....X.......xxxx"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "kk": "x.........x.....", "sn": "....X.......xx..", "t1": "..............xx"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": _S24},
        {"hh": "x.x.x.x.........", "kk": "x.........x.....", "sn": "....X...xxxx....", "t1": "............xx..", "ft": "..............xx"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x......x.x...", "sn": _S24},
        {"hh": "x.x.............", "kk": "x...........x.x.", "sn": "....xxxx........", "t1": "........xxxx....", "ft": "............x.x."},
    ]),
    _groove("19 · Kick doubles", "Two kicks in a row on the sixteenths, single pedal: the & a of 1 and the a of 3.", 90, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.xx..", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "kk": "x.xx......x.....", "sn": "....X.......xx..", "t1": "..............xx"},
    ]),
    _groove("20 · Punk", "Snare on every &, kick on every beat, hats along: the upbeat snare drives it.", 96, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x...x...x...x...", "sn": "..X...X...X...X."},
        {"hh": _H8, "kk": "x...x...x...x...", "sn": "..X...X...X...X."},
        {"hh": _H8, "kk": "x...x...x...x...", "sn": "..X...X...X...X."},
        {"hh": "x.x.x.x.x.x.....", "kk": "x...x...x...x...", "sn": "..X...X...X.xxxx"},
    ]),
    _groove("21 · Gallop", "The metal gallop in the kick: eighth plus two sixteenths under straight hats.", 88, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xxx.xxx.xxx.xx", "sn": _S24},
        {"hh": _H8, "kk": "x.xxx.xxx.xxx.xx", "sn": _S24},
        {"hh": _H8, "kk": "x.xxx.xxx.xxx.xx", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "kk": "x.xxx.xxx.xx....", "sn": "....X.......xx..", "ft": "..............xx"},
    ]),
    _groove("22 · Riding the crash", "Second half of the phrase the right hand rides the left crash on the eighths; the right crash marks the return.", 92, [
        {"cr": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.xx..", "sn": _S24},
        {"cl": "x.x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"cl": "x.x.x.x.x.x.....", "kk": "x.xx......x.....", "sn": "....X.......xx..", "t1": "..............xx"},
    ]),
    _groove("23 · Crash on the &", "The right crash lands on the & of 4 with a kick under it, pushing into the next bar.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": "x.x.x.x.x.x.x...", "cr": "..............x.", "kk": "x.xx......x...x.", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "cr": "..............x.", "kk": "x.xx......x...x.", "sn": "....X.......xx.."},
    ]),
    _groove("24 · Kick runs", "Sixteenth-note kick runs in bar 4, first half a bar, then a whole bar under the snare.", 84, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.xx..", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": "x.x.x.x.........", "kk": "x.xx....xxxxxxxx", "sn": "....X.......X..."},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.xx..", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.x...", "sn": _S24},
        {"cr": "x...............", "kk": "xxxxxxxxxxxxxxxx", "sn": "....X.......X..."},
    ]),
    _groove("25 · Around the kit", "Whole-bar fills every other bar: snare to rack to floor, then back up, then in pairs, then with kicks.", 90, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"kk": "x...........x...", "sn": "xxxx............", "t1": "....xxxx........", "ft": "........xxxx....", "cr": "............x..."},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"kk": "x...........x...", "ft": "xxxx............", "t1": "....xxxx........", "sn": "........xxxxxxxx"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"kk": "x...x...x...x...", "sn": "xx..xx..........", "t1": "..xx......xx....", "ft": "......xx....xxxx"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"kk": "..xx..xx..xx..xx", "sn": "xx..............", "t1": "....xx..........", "ft": "........xx..xx.."},
    ]),
    _groove("26 · Breakdown", "Half time, right crash on every beat, snare on 3, the kick chops the sixteenths underneath.", 80, [
        {"cl": "x...............", "cr": "....x...x...x...", "kk": "x..x..x.....x.x.", "sn": "........X......."},
        {"cr": "x...x...x...x...", "kk": "x..x..x...x...xx", "sn": "........X......."},
        {"cr": "x...x...x...x...", "kk": "x..x..x.....x.x.", "sn": "........X......."},
        {"cr": "x...x...x.......", "kk": "x..x..x.........", "sn": "........X...xx..", "t1": "............x...", "ft": "..............xx"},
    ]),
    _groove("27 · Three over four", "The kick walks in groups of three sixteenths across the bar while the hats and the backbeat stay straight.", 86, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x..x..x..x..x", "sn": _S24},
        {"hh": _H8, "kk": "..x..x..x..x..x.", "sn": _S24},
        {"hh": _H8, "kk": "x..x..x..x..x..x", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "kk": "..x..x..x..x....", "sn": "....X.......xx..", "ft": "..............xx"},
    ]),
    _groove("28 · Blast, lite", "Kick and snare alternate on the eighths under a ride on the eighths. Speed it up as it settles.", 84, [
        {"cl": "x...............", "rd": "..x.x.x.x.x.x.x.", "kk": "x.x.x.x.x.x.x.x.", "sn": "..X...X...X...X."},
        {"rd": _H8, "kk": "x.x.x.x.x.x.x.x.", "sn": "..X...X...X...X."},
        {"rd": _H8, "kk": "x.x.x.x.x.x.x.x.", "sn": "..X...X...X...X."},
        {"rd": "x.x.x.x.x.x.....", "kk": "x.x.x.x.x.x.....", "sn": "..X...X...X.xxxx"},
    ]),
    _groove("29 · Crash landing", "Fills that end on a crash with a kick, then the right crash answers on the & before the next phrase.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.xx..", "sn": _S24},
        {"hh": "x.x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": "x.x.x...........", "kk": "x.......x...x.x.", "sn": "....X...xx......", "t1": "..........xx....", "ft": "............x...", "cl": "............x...", "cr": "..............x."},
    ]),
    _groove("30 · Rock anthem", "Sixteen bars: verse on the hats, a build with the snare growing from ghosts to accents, chorus riding the crash, big ending.", 92, [
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.xx..", "sn": _S24},
        {"hh": _H8, "kk": "x.xx......x.x...", "sn": _S24},
        {"hh": "x.x.x.x.x.x.....", "kk": "x.xx......x.....", "sn": "....X.......xx..", "t1": "..............xx"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x..x..x..x..x..x", "sn": _S24},
        {"hh": _H8, "kk": "..x..x..x..x..x.", "sn": _S24},
        {"hh": "x.x.x.x.x.x.x.x.", "kk": "x...x...x...x...", "sn": "oooooooooooooooo"},
        {"hh": "x.x.x.x.x.x.x.x.", "kk": "x...x...x...x...", "sn": "xxxxxxxxXXXXXXXX", "cr": "...............x"},
        {"cl": "x.x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"cl": "x.x.x.x.x.x.x.x.", "kk": "x.xx......x.xx..", "sn": _S24},
        {"cl": "x.x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"cl": "x.x.x.x.x.x.....", "cr": "..............x.", "kk": "x.xx......x...x.", "sn": "....X.......xx.."},
        {"cl": "x.x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"cl": "x.x.x.x.x.x.x.x.", "kk": "x.xx......x.xx..", "sn": _S24},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "x.xx......x.x...", "sn": _S24},
        {"kk": "x...x...x...x.x.", "sn": "xxxx........xx..", "t1": "....xxxx........", "ft": "........xxxx....", "cl": "..............x.", "cr": "..............x."},
    ], bars=16),
    # Cumbia villera on the three drum bodies (asked 2026-09-12, named plena until 2026-09-14): the
    # cumbia sway, floor tom low on 1 and 3, rack tom on every &, snare on 2 and 4 (the redoblante),
    # then the pickups: the tom's double into the beat, the floor's "4 a" into the 1. Hands free: no
    # kick, no hats.
    _groove("31 · Cumbia", "Cumbia villera on the drum bodies: floor on 1 and 3, rack tom on every &, snare on 2 and 4; pickups on the tom and the floor.", 96, [
        {"ft": "x.......x.......", "t1": "..x...x...x...x.", "sn": _S24},
        {"ft": "x.......x.......", "t1": "..x...x...x...x.", "sn": _S24},
        {"ft": "x.......x.......", "t1": "..x...x...x...xx", "sn": _S24},
        {"ft": "x.......x.....x.", "t1": "..x...x...x....x", "sn": "....X.......X..."},
    ], bars=16, backing="cumbia"),
    # The cumbia with the redoblante's repiques (asked 2026-09-14): the same sway and the two
    # pickups of level 31, plus the snare's picado figures, staccato sixteenths: the drag (a
    # ghost on the "a" into the backbeat), the run out of the 4 into the 1 (4 e & a), the run
    # into the 4 (& a of 3), and a whole two-beat repique closing the phrase, accented on the
    # beats. The rack tom yields its & where a repique needs the snare alone.
    _groove("32 · Cumbia: repiques", "The cumbia of level 31 with the redoblante's repiques: ghost drags into the backbeat, staccato sixteenth runs out of the 4 and into it, and a two-beat repique closing the phrase.", 96, [
        {"ft": "x.......x.......", "t1": "..x...x...x...x.", "sn": _S24},
        {"ft": "x.......x.......", "t1": "..x...x...x...x.", "sn": "...oX......oX..."},
        {"ft": "x.......x.......", "t1": "..x...x...x...xx", "sn": "...oX......oX..."},
        {"ft": "x.......x.......", "t1": "..x...x...x.....", "sn": "....X.......Xxxx"},
        {"ft": "x.......x.......", "t1": "..x...x...x...x.", "sn": "...oX......oX..."},
        {"ft": "x.......x.......", "t1": "..x...x.......x.", "sn": "....X.....xxX..."},
        {"ft": "x.......x.....x.", "t1": "..x...x...x....x", "sn": "...oX......oX..."},
        {"ft": "x.......x.......", "t1": "..x...x.........", "sn": "....X...XxxxXxxx"},
    ], bars=16, backing="cumbia"),
    # Reggae one drop with the classic fills (asked 2026-09-13): kick and cross-stick together on
    # the 3, hats on the eighths; every fourth bar a fill, the crash on the 1 after it.
    # Fills: the four sixteenths on beat 4 into the drop; the walk down snare, rack, floor over
    # beats 3 and 4 with the kick pickup on the "a" of 4; snare doubles then rack and floor on
    # beat 4; the sixteenth roll over beats 3 and 4 accented on every third stroke.
    _groove("33 · Reggae: one drop, fills", "One drop (kick and cross-stick on the 3, hats on the eighths); every fourth bar a classic fill: the four on beat 4, the walk down the toms, snare doubles to the floor, the roll over 3 and 4.", 76, [
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": "x.x.x.x.x.x.....", "kk": "........x.......", "sn": "........x...xxxX"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "........x.......", "sn": "........x......."},
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": "x.x.x.x.x.......", "kk": "........x......x", "sn": "........xx......", "t1": "..........xx....", "ft": "............xx.."},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "........x.......", "sn": "........x......."},
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": "x.x.x.x.x.x.....", "kk": "........x.......", "sn": "........x...xx..", "t1": "..............x.", "ft": "...............x"},
        {"cl": "x...............", "hh": "..x.x.x.x.x.x.x.", "kk": "........x.......", "sn": "........x......."},
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": _H8, "kk": "........x.......", "sn": "........x......."},
        {"hh": "x.x.x.x.x.......", "kk": "........x.......", "sn": "........XxxXxxXx"},
    ], bars=16),
]
# --- hi-hat control: what the pedal, the zone and the foot can say ----------------------
# Notation: t/T tight bow/edge, m/M mid, a/A open, pd x = chick. The names of the articulations
# are hhmapper's, so what the lesson asks for is what GetGood Drums plays.
_KS = {"kk": "x.......x.......", "sn": _S24}
HIHAT_LESSONS = [
    _groove("Tight and open", "Eighths on the bow: a bar with the pedal down tight, a bar with it up open. Feel the pedal travel.", 84, [
        {"hh": "t.t.t.t.t.t.t.t.", **_KS},
        {"hh": "a.a.a.a.a.a.a.a.", **_KS},
    ]),
    _groove("Half open", "The same eighths at half pedal: the sloshy mid hat. Find the spot and hold it.", 84, [
        {"hh": "m.m.m.m.m.m.m.m.", **_KS},
        {"hh": "m.m.m.m.m.m.m.m.", "kk": "x.....x.x.......", "sn": _S24},
    ]),
    _groove("Openness ladder", "Two bars tight, two bars mid, two bars open, then back down to tight, edge on the beats and bow on the &s. Every step is a pedal position.", 84, [
        {"hh": "T.t.T.t.T.t.T.t.", **_KS}, {"hh": "T.t.T.t.T.t.T.t.", **_KS},
        {"hh": "M.m.M.m.M.m.M.m.", **_KS}, {"hh": "M.m.M.m.M.m.M.m.", **_KS},
        {"hh": "A.a.A.a.A.a.A.a.", **_KS}, {"hh": "A.a.A.a.A.a.A.a.", **_KS},
        {"hh": "M.m.M.m.M.m.M.m.", **_KS}, {"hh": "T.t.T.t.T.t.T.t.", **_KS},
    ]),
    _groove("Open on the &", "Tight eighths, the hat opens on the & of 4 and the foot closes it on the next 1: the disco hat.", 88, [
        {"hh": "t.t.t.t.t.t.t.a.", "pd": "x...............", **_KS},
        {"hh": "t.t.t.t.t.t.t.a.", "pd": "x...............", "kk": "x.......x.x.....", "sn": _S24},
    ]),
    _groove("Bark", "An open hat choked right away by the foot: open on the & of 2, chick on 3, tight around it.", 88, [
        {"hh": "t.t.t.a...t.t.t.", "pd": "........x.......", **_KS},
        {"hh": "t.t.t.a...t.t.a.", "pd": "........x.......", "kk": "x.......x.x.....", "sn": _S24},
    ]),
    _groove("Foot on 2 and 4", "No sticks on the hat: the foot chicks on 2 and 4 under kick and snare, then on every beat.", 84, [
        {"pd": "....x.......x...", "kk": "x.......x.......", "sn": _S24},
        {"pd": "....x.......x...", "kk": "x.......x.......", "sn": _S24},
        {"pd": "x...x...x...x...", "kk": "x.......x.......", "sn": _S24},
        {"pd": "x...x...x...x...", "kk": "x.......x.x.....", "sn": _S24},
    ]),
    _groove("Bow and edge", "Tight eighths alternating bow taps and edge accents: the shoulder of the stick speaks on the beat.", 84, [
        {"hh": "T.t.T.t.T.t.T.t.", **_KS},
        {"hh": "T.t.t.t.T.t.t.t.", **_KS},
    ]),
    _groove("Edge on the open", "Open hats on the edge: the & of 2 and the & of 4 open on the edge, chicks close them on 3 and 1.", 88, [
        {"hh": "t.t.t.A.t.t.t.A.", "pd": "x.......x.......", **_KS},
        {"hh": "t.t.t.A.t.t.t.A.", "pd": "x.......x.......", "kk": "x..x......x.x...", "sn": _S24},
    ]),
    _groove("Sixteenths, mid and tight", "Sixteenths on the bow, tight on the beats and mid in between: the pedal breathes with every beat.", 76, [
        {"hh": "tmmmtmmmtmmmtmmm", **_KS},
        {"hh": "tmmmtmmmtmmmtmmm", "kk": "x..x......x.x...", "sn": _S24},
    ]),
    _groove("Hi-hat song", "Sixteen bars that use everything: tight groove, open &s, edge accents, a mid section, barks, and the foot on 2 and 4.", 88, [
        {"hh": "t.t.t.t.t.t.t.t.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "t.t.t.t.t.t.t.a.", "pd": "x...............", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "t.t.t.t.t.t.t.t.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "t.t.t.t.t.t.t.a.", "pd": "x...............", "kk": "x.......x.x.....", "sn": "....X.......X.x."},
        {"hh": "T.t.T.t.T.t.T.t.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "T.t.T.t.T.t.T.a.", "pd": "x...............", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "T.t.T.t.T.t.T.t.", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "T.t.T.t.T.t.t.A.", "pd": "x...............", "kk": "x.......x.x.....", "sn": "....X.......xx.."},
        {"hh": "m.m.m.m.m.m.m.m.", "kk": "x.....x.x.......", "sn": _S24},
        {"hh": "m.m.m.m.m.m.m.m.", "kk": "x.....x.x.......", "sn": _S24},
        {"hh": "m.m.m.m.m.m.m.m.", "kk": "x.....x.x.......", "sn": _S24},
        {"hh": "m.m.m.m.m.m.m.A.", "pd": "x...............", "kk": "x.....x.x.......", "sn": "....X.......X.x."},
        {"hh": "t.t.t.a...t.t.t.", "pd": "........x.......", "kk": "x.......x.x.....", "sn": _S24},
        {"hh": "t.t.t.a...t.t.a.", "pd": "x.......x.......", "kk": "x.......x.x.....", "sn": _S24},
        {"pd": "....x.......x...", "kk": "x.......x.......", "sn": _S24},
        {"hh": "t.t.t.t.t.t.t.A.", "pd": "....x.......x...", "kk": "x.......x.x.....", "sn": "....X.......xxxx"},
    ], bars=16),
]
# --- double kick: a foot ostinato under simple hands ---------------------------------
def _kick_ostinato(name, desc, bpm, bars, feet, sub, hands):
    """A double-kick level. `feet`: the kick pattern for one bar on a grid of `sub` per
    beat, R / L the foot, "." a rest; it is the sticking strip (rests shown as dots).
    `hands`: GROOVE_KEYS key -> pattern on the same grid (x / X / .). Feet are judged on
    time only, no dynamics."""
    beat = 60 / bpm
    slots = 4 * sub
    assert len(feet) == slots and all(len(p) == slots for p in hands.values()), name
    notes = []
    for bar in range(bars):
        for i, c in enumerate(feet):
            if c in "RL":
                notes.append(ChartNote((bar * 4 + i / sub) * beat, "kick", GROOVE_VEL["x"], hand=c, strip=i))
        for k, pat in hands.items():
            for i, c in enumerate(pat):
                if c in GROOVE_VEL:
                    notes.append(ChartNote((bar * 4 + i / sub) * beat, GROOVE_KEYS[k], GROOVE_VEL[c], accent=(c == "X")))
    notes.sort(key=lambda n: (n.t, INSTRUMENTS.index(n.key)))
    ch = Chart(name, notes, bpm, desc, [(0, sub)])
    ch.sticking = list(feet)
    ch.accents = set()
    ch.sticking_groups = [sub * b for b in range(1, 4)]
    return ch


# hands for the double-kick levels: the pulse on the hats, the backbeat on the snare
_DK_HANDS_16 = {"hh": "x...x...x...x...", "sn": "....X.......X..."}
_DK_HANDS_8 = {"hh": "x.x.x.x.", "sn": "..X...X."}
_DK_HANDS_12 = {"hh": "x..x..x..x..", "sn": "...X.....X.."}

# The curriculum: the feet learn to alternate, then to burst, then to run; the hands
# keep a plain beat on top so the feet become an ostinato, not a fill.
KICK_OSTINATOS = [
    _kick_ostinato("Double kick 8ths", "Right, left on the eighths; hats on the beat, snare on 2 and 4.", 80, 8,
                   "RLRLRLRL", 2, _DK_HANDS_8),
    _kick_ostinato("Double kick bursts of two", "Two sixteenths on every beat, right then left, then rest.", 80, 8,
                   "RL..RL..RL..RL..", 4, _DK_HANDS_16),
    _kick_ostinato("Double kick gallop", "Eighth, sixteenth, sixteenth on every beat: right, left, right.", 80, 8,
                   "R.LRR.LRR.LRR.LR", 4, _DK_HANDS_16),
    _kick_ostinato("Double kick 16ths", "Sixteenths on the feet, right foot on the beat, under the plain beat.", 80, 8,
                   "RLRLRLRLRLRLRLRL", 4, _DK_HANDS_16),
    _kick_ostinato("Double kick 16ths, left lead", "The same run leading with the left foot: the weak foot lands on the beat.", 80, 8,
                   "LRLRLRLRLRLRLRLR", 4, _DK_HANDS_16),
    _kick_ostinato("Double kick 16ths, hats 8ths", "Sixteenths on the feet with the hats on the eighths: hands and feet at different speeds.", 80, 8,
                   "RLRLRLRLRLRLRLRL", 4, {"hh": "x.x.x.x.x.x.x.x.", "sn": "....X.......X..."}),
    _kick_ostinato("Double kick triplets", "Eighth-note triplets on the feet, alternating, so the lead foot swaps every beat.", 80, 8,
                   "RLRLRLRLRLRL", 3, _DK_HANDS_12),
    _kick_ostinato("Double kick, bursts of four", "Four sixteenths on beats 2 and 4, rest on 1 and 3: start and stop cleanly.", 80, 8,
                   "....RLRL....RLRL", 4, {"hh": "x...x...x...x...", "sn": "X.......X......."}),
]


EXERCISES = EXERCISES + RUDIMENTS + KICK_OSTINATOS + HIHAT_LESSONS
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
            notes = set(kit_notes(kit, key))
            if key in CRASH_PAIR and CRASH_PAIR[key] not in keys:
                notes |= set(kit_notes(kit, CRASH_PAIR[key]))      # one crash lane: either pad counts
            lane = Lane(i, key, LABELS[key], COLORS[key], notes)
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
