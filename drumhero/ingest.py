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


ONSET_LAG_S = 0.0135        # onset-strength peaks trail the stroke by this much (calibrated on synthetic drums)
CONSTANT_STD_MS = 12.0      # residual std under which a song counts as constant tempo (played to a click)


def comb_bpm(onset_env, fps, lo=65.0, hi=150.0, step=0.5):
    """Global tempo by comb filter over the onset envelope: for each candidate bpm, the best
    phase's summed onset energy on the pulse train. From santi's tempo repo. Exact on steady
    tempos where the tempogram is off by a bpm or two; blind to drift."""
    cands = np.arange(lo, hi, step)
    scores = np.zeros(len(cands))
    n = len(onset_env)
    for i, bpm in enumerate(cands):
        period = 60.0 / bpm * fps
        n_pulses = int(n / period)
        if n_pulses < 2:
            continue
        pulses = np.arange(n_pulses) * period
        offs = np.linspace(0, period, min(int(period), 50), endpoint=False)
        idx = np.round(pulses[None, :] + offs[:, None]).astype(int)
        valid = (idx >= 0) & (idx < n)
        idx = np.clip(idx, 0, n - 1)
        scores[i] = np.max(np.sum(onset_env[idx] * valid, axis=1))
    return float(cands[np.argmax(scores)])


def track_beats(audio_path, bpm_hint=None, offset_hint=None):
    """Beat times (s), the index of the first downbeat, the tempo and a mode string.

    1. Global tempo: the bpm hint from song.json, else the comb filter (65..150 bpm).
    2. Coarse beats from librosa's tracker seeded with that tempo (23 ms frames).
    3. Each beat refined to the onset peak within +-40 ms at 2.9 ms resolution, minus the
       calibrated onset lag; beats without a strong onset (intros) are not anchored.
    4. Linear fit over anchored beats: if the residual std is under CONSTANT_STD_MS the
       song was played to a click and the grid is the fitted constant tempo over the
       whole track; otherwise the grid follows the anchored beats with a light smoothing.
    5. Octave guard: a grid faster than 1.5x the tempo estimate is decimated.
    The first downbeat is the beat nearest offset_hint when given, else the beat nearest
    the first strong onset (most recordings start on a downbeat; pickups need the hint)."""
    import librosa
    y, sr = librosa.load(audio_path, sr=22050, mono=True)
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
    hint = float(bpm_hint) if bpm_hint else comb_bpm(onset, sr / 512)
    _, frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, units="frames", trim=False, start_bpm=hint)
    coarse = librosa.frames_to_time(frames, sr=sr)
    if len(coarse) < 8:
        sys.exit(f"only {len(coarse)} beats found in {audio_path}; is it silent?")

    hop = 64
    fine = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    ffps = sr / hop
    thresh = 0.3 * np.percentile(fine, 95)
    win = int(0.04 * ffps)
    refined, anchored = [], []
    for t in coarse:
        c = int(round(t * ffps))
        lo, hi = max(0, c - win), min(len(fine), c + win + 1)
        seg = fine[lo:hi]
        if len(seg) and seg.max() > thresh:
            refined.append((lo + int(np.argmax(seg))) / ffps)
            anchored.append(True)
        else:
            refined.append(t)
            anchored.append(False)
    refined = np.array(refined) - ONSET_LAG_S
    anchored = np.array(anchored)
    i = np.arange(len(refined))

    if anchored.sum() < 8:
        beats, bpm, mode = refined, hint, "coarse (too few clear onsets)"
    else:
        ia, ta = i[anchored], refined[anchored]
        b, a = np.polyfit(ia, ta, 1)
        keep = np.abs(ta - (a + b * ia)) < 0.03
        if keep.sum() >= 8:
            b, a = np.polyfit(ia[keep], ta[keep], 1)
        resid = ta - (a + b * ia)
        std = float(np.std(resid[keep]) * 1000) if keep.sum() >= 8 else float(np.std(resid) * 1000)
        if std < CONSTANT_STD_MS:
            beats, bpm, mode = a + b * i, 60.0 / b, f"constant tempo (residual {std:.1f} ms)"
        else:
            knots = np.interp(i, ia, ta)
            iv = np.diff(knots)
            sm = np.array([np.median(iv[max(0, j - 2): j + 3]) for j in range(len(iv))])
            beats, bpm, mode = np.concatenate([[knots[0]], knots[0] + np.cumsum(sm)]), 60.0 / float(np.median(sm)), \
                f"variable tempo (residual {std:.1f} ms)"

    while bpm > 1.5 * hint and len(beats) > 16:          # tracked the eighths: keep the stronger half
        idx = [np.clip(np.round(beats[k::2] * ffps).astype(int), 0, len(fine) - 1) for k in (0, 1)]
        k = int(np.argmax([fine[ix].sum() for ix in idx]))
        beats, bpm = beats[k::2], bpm / 2
    while bpm < hint / 1.5 and len(beats) > 8:            # tracked the half notes: insert midpoints
        beats = np.sort(np.concatenate([beats, (beats[:-1] + beats[1:]) / 2]))
        bpm *= 2

    if offset_hint is None:
        peaks = librosa.onset.onset_detect(onset_envelope=onset, sr=sr, units="frames")
        strong = [f for f in peaks if onset[f] >= 0.35 * onset.max()]
        offset_hint = float(librosa.frames_to_time(strong[0] if strong else peaks[0], sr=sr)) if len(peaks) else beats[0]
    phase = int(np.argmin(np.abs(np.asarray(beats) - offset_hint)))
    return [float(x) for x in beats], phase, float(bpm), mode


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
    beats, phase, tempo, mode = track_beats(audio, bpm, offset)
    bars_available = (len(beats) - phase) // 4
    events, bars_needed = build_events(meta["sections"], bars_available + 64)   # extrapolate a bit past the audio
    grid = write_chart(os.path.join(folder, "chart.mid"), beats, phase, events, bars_needed)
    meta["offset"] = round(beats[phase], 4)
    meta["tracked_bpm"] = round(60.0 / statistics.median(np.diff(grid)), 2)
    json.dump(meta, open(meta_path, "w"), indent=2)
    meta["tempo_mode"] = mode
    json.dump({"beats": [round(b, 4) for b in beats], "downbeat_index": phase, "tempo": tempo, "mode": mode},
              open(os.path.join(folder, "beats.json"), "w"))
    print(f"{meta.get('title', folder)}: {len(beats)} beats tracked, {mode}, {meta['tracked_bpm']} bpm, "
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
