"""What happened in the game while a Twitch VOD was recording, on the VOD's clock.

    .venv/bin/python .claude/skills/twitch-clips/moments.py --vod vod.json [--offset S] [--json]
    .venv/bin/python .claude/skills/twitch-clips/moments.py --start EPOCH --duration S
    .venv/bin/python .claude/skills/twitch-clips/moments.py --layout 1920x1080

vod.json is `yt-dlp -J <vod url>`: its `timestamp` is when Twitch started recording (epoch, UTC)
and `duration` the VOD's length. VOD time of anything = wall - timestamp - offset (offset: the
correction measured against the AAC copy in ~/Movies/drumhero/streams, 0 by default).

Prints, on the VOD's clock: every level played (from the run logs in ~/Library/Logs/drumhero/runs:
name, bpm, grade, stars, max combo, misses, longest streak of PERFECT/GOOD with its time) and every
take the drummer chose to record (~/Movies/drumhero/*/take.json), then suggested clips: every level
with 2 stars or more, from 2 s before its first hit to 4 s after its end (the results screen),
best first. --layout prints the camera picture-in-picture rectangle of the VOD frame (from
settings.json, capture.pip_rect) and the rects of the 1080x1920 social edition (capture.social_layout).
"""
import argparse
import glob
import json
import os
import sys
import time

RUNS_DIR = os.path.expanduser("~/Library/Logs/drumhero/runs")
TAKES_DIR = os.path.expanduser("~/Movies/drumhero")
SETTINGS = os.path.expanduser("~/.config/drumhero/settings.json")


def hms(s):
    s = max(0, int(round(s)))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def runs_between(t0, t1):
    out = []
    for path in sorted(glob.glob(os.path.join(RUNS_DIR, "*.jsonl"))):
        with open(path) as f:
            try:
                head = json.loads(f.readline())
            except ValueError:
                continue
            if head.get("kind") != "run" or head.get("started", 0) > t1 or head.get("ended", head.get("started", 0)) < t0:
                continue          # keep the runs that overlap [t0, t1]
            events = []
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        hits = [e for e in events if e.get("kind") == "hit"]
        misses = [e for e in events if e.get("kind") == "miss"]
        streak, best, best_end = 0, 0, None
        for e in sorted(hits + misses, key=lambda e: e["wall"]):
            if e["kind"] == "hit" and e.get("judge") in ("PERFECT", "GOOD"):
                streak += 1
                if streak > best:
                    best, best_end = streak, e["wall"]
            else:
                streak = 0
        st = head.get("stats") or {}
        chart = head["chart"]
        first_hit = min((h["wall"] for h in hits), default=head["started"])
        out.append({
            "path": path, "name": chart["name"], "bpm": chart.get("bpm"), "started": head["started"],
            "ended": head.get("ended", first_hit), "first_hit": first_hit,
            "chart_zero": next((h["wall"] - h["chart_t"] for h in sorted(hits, key=lambda h: h["wall"])
                                if h.get("chart_t") is not None), first_hit),
            "grade": st.get("grade"), "stars": st.get("stars"), "max_combo": st.get("max_combo"),
            "accuracy": st.get("accuracy"), "hits": len(hits), "misses": len(misses), "notes": st.get("notes"),
            "best_streak": best, "best_streak_end": best_end,
        })
    return out


def takes_between(t0, t1):
    out = []
    for path in glob.glob(os.path.join(TAKES_DIR, "*", "take.json")) + glob.glob(os.path.join(TAKES_DIR, "* take.json")):
        try:
            with open(path) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if meta.get("t0", 0) <= t1 and meta.get("t0", 0) + meta.get("duration", 0) >= t0:
            out.append({"path": os.path.dirname(path) if path.endswith("/take.json") else path,
                        "t0": meta["t0"], "duration": meta.get("duration", 0)})
    return sorted(out, key=lambda t: t["t0"])


def layout(size):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    from drumhero.capture import pip_rect, social_layout
    with open(SETTINGS) as f:
        st = json.load(f)
    w, h = size
    x, y, pw, ph = pip_rect((w, h), float(st.get("capture_pip", 0.28)), st.get("capture_corner", "br"))
    (gx, gy, gw, gh), (cx, cy, cw, ch) = social_layout((w, h), float(st.get("capture_split", 0.32)))
    print(f"VOD frame {w}x{h}: camera picture-in-picture at x={x} y={y} {pw}x{ph} (corner {st.get('capture_corner', 'br')})")
    print(f"social 1080x1920: game scaled to {gw}x{gh} at y={gy}; camera {cw}x{ch} at y={cy} (crop the PiP, scale to fill)")
    print(f"ffmpeg: [0:v]scale={gw}:{gh}:flags=lanczos,pad=1080:1920:0:{gy}:black[g];"
          f"[0:v]crop={pw}:{ph}:{x}:{y},scale={cw}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,crop={cw}:{ch}[cam];"
          f"[g][cam]overlay=0:{cy}[v]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vod", help="yt-dlp -J output of the VOD")
    ap.add_argument("--start", type=float, help="epoch seconds of the VOD's first frame")
    ap.add_argument("--duration", type=float, help="VOD length in seconds")
    ap.add_argument("--offset", type=float, default=0.0, help="seconds to subtract from wall - start (measured alignment)")
    ap.add_argument("--layout", help="print the crop rects for a VOD frame of this size, e.g. 1920x1080")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.layout:
        layout(tuple(int(v) for v in a.layout.lower().split("x")))
        return
    if a.vod:
        with open(a.vod) as f:
            v = json.load(f)
        start, duration = float(v["timestamp"]), float(v["duration"])
        print(f"VOD {v.get('id')} '{v.get('title')}' started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(start))} local, {hms(duration)}")
    elif a.start and a.duration:
        start, duration = a.start, a.duration
    else:
        ap.error("--vod, or --start and --duration")
    t = lambda wall: wall - start - a.offset
    runs = runs_between(start, start + duration)
    takes = takes_between(start, start + duration)
    for r in runs:
        r.update(vod_start=t(r["first_hit"]), vod_end=t(r["ended"]), vod_chart_zero=t(r["chart_zero"]),
                 vod_best_streak_end=t(r["best_streak_end"]) if r["best_streak_end"] else None)
    for k in takes:
        k.update(vod_start=t(k["t0"]), vod_end=t(k["t0"] + k["duration"]))
    clips = sorted((r for r in runs if (r["stars"] or 0) >= 2), key=lambda r: (-(r["stars"] or 0), -(r["grade"] or 0)))
    if a.json:
        json.dump({"start": start, "duration": duration, "offset": a.offset, "runs": runs, "takes": takes,
                   "clips": [{"name": c["name"], "in": max(0, c["vod_start"] - 2), "out": min(duration, c["vod_end"] + 4),
                              "stars": c["stars"], "grade": c["grade"]} for c in clips]}, sys.stdout, indent=1)
        return
    print(f"\n{len(runs)} levels played during the VOD (vod time in..out, name, bpm, grade, stars, combo, misses, best streak):")
    for r in runs:
        streak = f"{r['best_streak']} ending {hms(r['vod_best_streak_end'])}" if r["best_streak_end"] else "-"
        print(f"  {hms(r['vod_start'])}..{hms(r['vod_end'])}  {r['name']}  {r['bpm']:.0f} bpm  grade {r['grade'] or 0:.0f}  "
              f"{r['stars'] or 0}*  combo {r['max_combo'] or 0}/{r['notes'] or 0}  misses {r['misses']}  streak {streak}")
    print(f"\n{len(takes)} takes recorded during the VOD:")
    for k in takes:
        print(f"  {hms(k['vod_start'])}..{hms(k['vod_end'])}  {os.path.basename(k['path'])}")
    print("\nsuggested clips (2 stars or more, best first; 2 s of run-up, 4 s of results):")
    for c in clips:
        print(f"  {hms(max(0, c['vod_start'] - 2))}..{hms(min(duration, c['vod_end'] + 4))}  {c['name']}  {c['stars']}*  grade {c['grade']:.0f}")
    if not clips:
        print("  none: pick from the levels and takes above, or from the instructions")


if __name__ == "__main__":
    main()
