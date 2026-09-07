"""Song ingestion: turn an audio file plus a section map into a playable, educational chart.

    .venv/bin/python -m drumhero.ingest songs/i-wont-back-down

The folder holds:
  song.json   title, artist, audio file name, a bpm hint, an optional first-downbeat
              offset, and the form: a list of sections with bar counts and a drum
              pattern each. Written by hand, usually with Claude's help.
  audio.mp3   the recording (any format SDL_mixer / librosa can read). You supply it.
Ingest writes:
  beats.json  the tracked beat times, the chosen downbeat phase and the tempo
  chart.mid   the drum chart on the recording's own beat grid (tempo map per beat),
              chart time 0 = the first downbeat, drums on MIDI channel 10
and fills in "offset" in song.json (audio time of chart time 0).

Patterns per bar (beat 0..3, key, velocity):
  rest            nothing (intro without drums)
  hats            eighth-note hats only
  kick_snare      kick 1 & 3, snare 2 & 4, no hats
  rock            kick 1 & 3, snare 2 & 4, eighth hats
  rock_pickup     rock plus a kick on the "and" of 3
  rock_quarters   rock with quarter-note hats
  halftime        kick 1, snare 3, eighth hats
  eighth_kick     kick on every eighth (driving), snare 2 & 4, hats
Section flags: "crash": true puts a crash on the section's first downbeat;
"fill": true replaces the last beat of the section with a sixteenth-note snare fill.
"""
import argparse
import json
import os
import statistics
import sys

import mido
import numpy as np

NOTES = {"kick": 36, "snare": 38, "hihat": 42, "hihat_open": 46, "crash": 49}
TPB = 480


def _hats(vel_on=85, vel_off=70):
    return [(e / 2, "hihat", vel_on if e % 2 == 0 else vel_off) for e in range(8)]


PATTERNS = {
    "rest": lambda: [],
    "hats": lambda: _hats(),
    "kick_snare": lambda: [(0, "kick", 110), (2, "kick", 110), (1, "snare", 115), (3, "snare", 115)],
    "rock": lambda: [(0, "kick", 110), (2, "kick", 110), (1, "snare", 115), (3, "snare", 115)] + _hats(),
    "rock_pickup": lambda: [(0, "kick", 110), (2, "kick", 110), (2.5, "kick", 100), (1, "snare", 115), (3, "snare", 115)] + _hats(),
    "rock_quarters": lambda: [(0, "kick", 110), (2, "kick", 110), (1, "snare", 115), (3, "snare", 115)]
    + [(b, "hihat", 85) for b in range(4)],
    "halftime": lambda: [(0, "kick", 110), (2, "snare", 118)] + _hats(),
    "eighth_kick": lambda: [(e / 2, "kick", 105 if e % 2 == 0 else 90) for e in range(8)]
    + [(1, "snare", 115), (3, "snare", 115)] + _hats(),
}
FILL = [(3, "snare", 100), (3.25, "snare", 90), (3.5, "snare", 105), (3.75, "snare", 110)]


def track_beats(audio_path, bpm_hint=None, offset_hint=None):
    """Beat times (s) and the index of the first downbeat, from librosa's beat tracker.
    The first downbeat is the tracked beat nearest offset_hint (audio time of a known
    downbeat) when given; otherwise the beat nearest the first strong onset, since most
    recordings start on a downbeat. Songs that start with a pickup need the hint."""
    import librosa
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    kw = {"start_bpm": bpm_hint} if bpm_hint else {}
    tempo, frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, units="frames", trim=False, **kw)
    beats = librosa.frames_to_time(frames, sr=sr)
    if len(beats) < 8:
        sys.exit(f"only {len(beats)} beats found in {audio_path}; is it silent?")
    if offset_hint is None:
        peaks = librosa.onset.onset_detect(onset_envelope=onset, sr=sr, units="frames")
        strong = [f for f in peaks if onset[f] >= 0.35 * onset.max()]
        offset_hint = float(librosa.frames_to_time(strong[0] if strong else peaks[0], sr=sr)) if len(peaks) else beats[0]
    phase = int(np.argmin(np.abs(beats - offset_hint)))
    tempo = float(np.atleast_1d(tempo)[0])
    return beats.tolist(), phase, tempo


def build_events(sections, bars_available):
    """[(bar, beat, key, velocity)] from the section list. Bars beyond the audio are dropped."""
    events, bar = [], 0
    for sec in sections:
        pattern = PATTERNS.get(sec.get("pattern", "rock"))
        if pattern is None:
            sys.exit(f"unknown pattern {sec.get('pattern')!r}; choose from {', '.join(PATTERNS)}")
        n = int(sec.get("bars", 8))
        start = int(sec.get("start_bar", bar))
        for i in range(n):
            b = start + i
            if b >= bars_available:
                break
            beats = list(pattern())
            if sec.get("fill") and i == n - 1:
                beats = [e for e in beats if e[0] < 3] + FILL
            if sec.get("crash") and i == 0:
                beats.append((0, "crash", 120))
            for beat, key, vel in beats:
                events.append((b, beat, key, vel))
        bar = start + n
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    return events, bar


def write_chart(path, beats, phase, events, bars_needed):
    """chart.mid with one set_tempo per beat so the MIDI grid follows the recording.
    Chart time 0 = beats[phase]. Beats past the tracked range are extrapolated."""
    grid = beats[phase:]
    median = statistics.median(np.diff(grid)) if len(grid) > 1 else 0.5
    while len(grid) < bars_needed * 4 + 2:
        grid.append(grid[-1] + median)
    mid = mido.MidiFile(ticks_per_beat=TPB)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    msgs = []                                                     # (tick, order, message)
    for i in range(len(grid) - 1):
        us = int(round((grid[i + 1] - grid[i]) * 1e6))
        msgs.append((i * TPB, 0, mido.MetaMessage("set_tempo", tempo=max(1, min(us, 0xFFFFFF)))))
    for bar, beat, key, vel in events:
        tick = int(round((bar * 4 + beat) * TPB))
        msgs.append((tick, 1, mido.Message("note_on", channel=9, note=NOTES[key], velocity=vel)))
        msgs.append((tick + TPB // 8, 2, mido.Message("note_off", channel=9, note=NOTES[key])))
    msgs.sort(key=lambda m: (m[0], m[1]))
    last = 0
    for tick, _, msg in msgs:
        msg.time = tick - last
        track.append(msg)
        last = tick
    mid.save(path)
    return grid


def ingest(folder, bpm=None, offset=None):
    meta_path = os.path.join(folder, "song.json")
    meta = json.load(open(meta_path))
    audio = os.path.join(folder, meta.get("audio", "audio.mp3"))
    if not os.path.exists(audio):
        sys.exit(f"missing {audio}: drop the recording there first")
    bpm = bpm or meta.get("bpm")
    offset = offset if offset is not None else meta.get("offset_hint")
    beats, phase, tempo = track_beats(audio, bpm, offset)
    bars_available = (len(beats) - phase) // 4
    events, bars_needed = build_events(meta["sections"], bars_available + 64)   # extrapolate a bit past the audio
    grid = write_chart(os.path.join(folder, "chart.mid"), beats, phase, events, bars_needed)
    meta["offset"] = round(beats[phase], 4)
    meta["tracked_bpm"] = round(60.0 / statistics.median(np.diff(grid)), 2)
    json.dump(meta, open(meta_path, "w"), indent=2)
    json.dump({"beats": [round(b, 4) for b in beats], "downbeat_index": phase, "tempo": tempo},
              open(os.path.join(folder, "beats.json"), "w"))
    print(f"{meta.get('title', folder)}: {len(beats)} beats tracked, ~{meta['tracked_bpm']} bpm, "
          f"first downbeat at {meta['offset']} s, {bars_available} bars of audio, chart covers {bars_needed} bars, "
          f"{len(events)} notes -> chart.mid")
    if bars_needed > bars_available:
        print(f"note: the form asks for {bars_needed} bars but the audio has {bars_available}; trim the sections")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="drumhero.ingest", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="song folder with song.json and the audio file")
    ap.add_argument("--bpm", type=float, help="tempo hint for the beat tracker (overrides song.json)")
    ap.add_argument("--offset", type=float, help="audio time of a known downbeat (overrides song.json offset_hint)")
    args = ap.parse_args(argv)
    ingest(args.folder, args.bpm, args.offset)


if __name__ == "__main__":
    main()
