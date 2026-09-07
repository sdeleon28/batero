"""Game state: the clock, judging hits, misses, guide track and stats. No drawing."""
import csv
import math
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass

PERFECT_MS = 25          # |error| <= this -> PERFECT
GOOD_MS = 60             # |error| <= this -> GOOD
OK_MS = 100              # |error| <= this -> OK; beyond -> the hit is stray / the note is missed
SCORE = {"PERFECT": 100, "GOOD": 50, "OK": 20}
TAIL_S = 2.0             # seconds after the last note before the results


@dataclass
class Flash:
    wall_t: float
    lane: int
    judge: str
    error_ms: float
    velocity: int


def lead_in_for(bpm: float) -> float:
    """Count-in: one bar of four beats, two bars if that is under two seconds."""
    beat = 60 / bpm
    return 4 * beat if 4 * beat >= 2.0 else 8 * beat


class Game:
    def __init__(self, chart, lanes, by_note, offset_ms=0.0, speed=1.0, sounds=None, guide=True):
        self.tracks = {}                    # name -> (Track, enabled); pre-rendered audio on the chart timeline
        self.chart = chart
        self.notes = chart.notes
        self.lanes = lanes
        self.by_note = by_note
        self.offset_ms = offset_ms
        self.speed = speed
        self.sounds = sounds
        self.guide = guide
        self.beat = 60 / chart.bpm
        self.lead_in = lead_in_for(chart.bpm)
        self.lock = threading.Lock()
        self.reset()

    # --- clock -------------------------------------------------------------
    def reset(self):
        self.stop_tracks()
        with self.lock:
            for n in self.notes:
                n.state, n.judge, n.error_ms, n.sounded = "pending", None, None, False
            self.wall_start = time.perf_counter()
            self.paused_at = None
            self.paused_total = 0.0
            self.flashes = deque(maxlen=64)
            self.hits = []                      # (chart_t, lane, judge, error_ms) for every judged input
            self.combo = self.max_combo = self.score = 0
            self.counts = {k: 0 for k in ("PERFECT", "GOOD", "OK", "MISS", "STRAY")}
            self.cursor = 0                     # first note that may still be pending
            self.guide_cursor = 0
            self.last_click_beat = None
            self.finished = False

    def song_time(self, wall_t=None):
        if wall_t is None:
            wall_t = time.perf_counter()
        if self.paused_at is not None:
            wall_t = self.paused_at
        return wall_t - self.wall_start - self.paused_total - self.lead_in

    @property
    def paused(self):
        return self.paused_at is not None

    def toggle_pause(self):
        with self.lock:
            if self.paused_at is None:
                self.paused_at = time.perf_counter()
            else:
                self.paused_total += time.perf_counter() - self.paused_at
                self.paused_at = None
        self.stop_tracks()                  # on resume they restart from the exact position

    # --- pre-rendered tracks (backing, metronome) --------------------------------
    def set_track(self, name, track, enabled=True):
        old = self.tracks.get(name)
        if old:
            old[0].stop()
        self.tracks[name] = (track, enabled)

    def enable_track(self, name, enabled):
        if name in self.tracks:
            track, _ = self.tracks[name]
            self.tracks[name] = (track, enabled)
            if not enabled:
                track.stop()

    def track_enabled(self, name):
        return name in self.tracks and self.tracks[name][1]

    def stop_tracks(self):
        for track, _ in self.tracks.values():
            track.stop()

    def _drive_tracks(self, t):
        """Start each enabled track at its own t0 (the count-in), restart it after a pause
        from the current time, and stop everything when the level is over."""
        for track, enabled in self.tracks.values():
            want = enabled and not self.finished and t < track.end
            if want and not track.playing and t >= track.t0 - 0.004:
                track.start_at(t)
            elif not want and track.playing:
                track.stop()

    # --- input -------------------------------------------------------------
    def hit(self, note: int, velocity: int = 100, wall_t: float = None):
        """Judge an incoming MIDI note. Called from the MIDI thread; must be quick."""
        lane = self.by_note.get(note)
        if lane is None:
            return None
        return self.hit_lane(lane, velocity, wall_t)

    def hit_lane(self, lane: int, velocity: int = 100, wall_t: float = None):
        if wall_t is None:
            wall_t = time.perf_counter()
        if self.sounds:
            self.sounds.play(self.lanes[lane].key, velocity)
        with self.lock:
            if self.paused or self.finished:
                return None
            t = self.song_time(wall_t) - self.offset_ms / 1000   # offset = your chain's latency: hits are treated as earlier
            best, best_err = None, None
            for n in self.notes[self.cursor:]:
                if n.t - t > OK_MS / 1000:
                    break
                if n.lane != lane or n.state != "pending":
                    continue
                err = t - n.t
                if abs(err) <= OK_MS / 1000 and (best is None or abs(err) < abs(best_err)):
                    best, best_err = n, err
            if best is None:
                judge, err_ms = "STRAY", None
            else:
                err_ms = best_err * 1000
                a = abs(err_ms)
                judge = "PERFECT" if a <= PERFECT_MS else "GOOD" if a <= GOOD_MS else "OK"
                best.state, best.judge, best.error_ms = "hit", judge, err_ms
            self._register(judge, lane, err_ms, velocity, wall_t, best)
            return judge

    def _register(self, judge, lane, err_ms, velocity, wall_t, note):
        self.counts[judge] += 1
        if judge in SCORE:
            self.combo += 1
            self.max_combo = max(self.max_combo, self.combo)
            self.score += SCORE[judge] * (1 + self.combo // 10)
        else:
            self.combo = 0
        self.flashes.append(Flash(wall_t, lane, judge, err_ms, velocity))
        self.hits.append((note.t if note else None, lane, judge, err_ms))

    # --- per-frame housekeeping ----------------------------------------------
    def update(self):
        """Misses, guide sounds, count-in clicks. Call once per frame."""
        with self.lock:
            if self.paused:
                return
            t = self.song_time()
            for i in range(self.cursor, len(self.notes)):
                n = self.notes[i]
                if n.t > t - OK_MS / 1000:
                    break
                if n.state == "pending":
                    n.state, n.judge = "miss", "MISS"
                    self._register("MISS", n.lane, None, 0, time.perf_counter(), n)
            while self.cursor < len(self.notes) and self.notes[self.cursor].state != "pending":
                self.cursor += 1
            if not self.finished and t > self.notes[-1].t + TAIL_S:
                self.finished = True
            self._drive_tracks(t)

            if self.sounds:
                # guide track: the chart's own notes as they cross the line
                while self.guide_cursor < len(self.notes) and self.notes[self.guide_cursor].t <= t:
                    n = self.notes[self.guide_cursor]
                    if self.guide and not n.sounded:
                        self.sounds.play(n.key, n.velocity, 0.45)
                    n.sounded = True
                    self.guide_cursor += 1

    # --- stats ---------------------------------------------------------------
    def stats(self):
        errs = [e for _, _, j, e in self.hits if e is not None]
        total = len(self.notes)
        hit = sum(self.counts[k] for k in SCORE)
        return {
            "notes": total,
            "hit": hit,
            "accuracy": hit / total if total else 0.0,
            "mean_ms": statistics.fmean(errs) if errs else 0.0,
            "std_ms": statistics.pstdev(errs) if len(errs) > 1 else 0.0,
            "early": sum(1 for e in errs if e < 0),
            "late": sum(1 for e in errs if e > 0),
        }

    def write_csv(self, path):
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["chart_time_s", "lane", "label", "judge", "error_ms"])
            for t, lane, judge, err in self.hits:
                w.writerow([f"{t:.4f}" if t is not None else "", lane, self.lanes[lane].label, judge,
                            f"{err:.1f}" if err is not None else ""])
