---
name: twitch-clips
description: Cut a recording of the user's Twitch stream (a video file whose path they give) into clips for distribution (16:9 and 9:16), choosing the moments from the game's own data about what happened. Use when the user asks for clips of a stream, to edit a VOD or stream recording, or to "editar el stream". Takes the file path plus optional instructions (which parts, how to edit).
argument-hint: "<path to the stream video> [which parts, how to edit; default: moments picked from the run logs]"
---

# Clips from a stream recording

Arguments for this run: $ARGUMENTS

The first argument is the path of the video (a Twitch VOD the user downloaded, a screen recording,
an export). The rest, if any, are instructions. No path: ask for it and stop, nothing else can
be done. No browser, no downloading: the file is the input.

The stream is the display as the viewers saw it: the game full frame, the camera as a
picture-in-picture in a corner, the LIVE badge top right, sometimes the chat pane bottom left.
Every stream's audio also stays as an AAC copy in
`~/Movies/drumhero/streams/<local start stamp> stream.aac`.

## Rules

- **Never start or stop the stream**: no T, no `python -m drumhero.twitch --stop`, no touching
  the daemon.
- **Never publish**: the clips land in a folder, the user distributes them. Do not upload, post,
  or share anything.
- Never modify or move the input file.
- Never leave a clip unwatched: every delivered file is probed and one frame of it looked at.

## 1. The video's clock against the game's

Video time = wall time - start of the video - offset. The helper does that for the run logs and
the takes:

    .venv/bin/python .claude/skills/twitch-clips/moments.py --file <video> [--start EPOCH] [--offset S] [--json]

Without `--start` it guesses when the first frame was (a `YYYYmmdd-HHMMSS` stamp in the file
name as local time, else the container's creation_time, else mtime minus duration) and says which.
A Twitch VOD's own start is its `created_at` (yt-dlp's `timestamp`, measured 2026-09-16: 13 s after
the daemon's `started` in `~/.config/drumhero/stream.json`); if the user knows when the recording
began, pass it as `--start`.

It prints every level played during the video on the video's clock (name, bpm, grade, stars, max
combo, misses, longest PERFECT/GOOD streak and where it ends), every take the drummer chose to
record, and suggested clips (levels with 2 stars or more, 2 s of run-up, 4 s of results screen,
best first). `--json` gives the same with `chart_zero` per level (video time of the chart's beat
0, so a downbeat is at `chart_zero + k * 4 * 60 / bpm`) and the run log paths, for cutting on the
beat. `--layout WxH` prints the crop rectangles for the 9:16 (below).

**Check the alignment before cutting**, always: extract a frame at a level's first hit
(`ffmpeg -ss T -i video -frames:v 1 frame.png`, read it) and see that the note is at the line and
the results screen appears where the log says the level ended. If it is off by more than a
second, measure the offset: on the picture (the results screen's first frame, found by extracting
frames around the expected time), or exactly with the AAC copy of the stream (same content; decode
60 s of each around a loud moment to mono 8 kHz PCM with ffmpeg and cross-correlate with numpy in
the venv). Pass it as `--offset` and write it in notes.md. If no run log falls inside the video,
say so and cut only from the instructions (or ask).

## 2. Which moments

With instructions, follow them: parts as video times ("1:02:30-1:03:10"), as descriptions ("the
four stars", "the last level", "when the plena finally passed"), or as a style ("hype under 60 s",
"one long highlight", "only the 9:16"). Map descriptions onto the helper's list.

By default, 3 to 6 clips, from these, in this order of preference:
1. The best runs (most stars, then grade): the whole level from the run-up to the stars on the
   results screen. The run-up shows the phrase's landing, keep 2 s of it.
2. A level passed after failing it: the fail's last bars, a cut, the pass (a story in one clip).
3. The longest PERFECT/GOOD streak of the night, if it is inside a run that did not make the cut.
4. Takes the drummer recorded during the stream: they chose those moments.
Clip lengths: 15..60 s for the 9:16, up to 90 s for the 16:9. Nothing shorter than 10 s, nothing
over 3 minutes unless asked. Skip a level under 10 s (an abandoned start).

## 3. Cut and edit

Output folder: `~/Movies/drumhero/clips/<YYYYMMDD> <label>/` (the video's local date, the label
from the main level or the instructions, like the existing `20260913 31 Plena 100`). Two files per
clip, numbered in video order: `NN <what happens> computer.mp4` (1920x1080) and `NN <what happens>
social.mp4` (1080x1920), plus `notes.md`: video in/out of each clip, what happens, why it was
chosen, the start and offset used, and anything left out and why.

- **Cuts on the beat**: start a clip on a downbeat of the run-up (chart_zero minus whole bars) and
  end 3..4 s after the results screen appears; a hard cut inside a level goes on a downbeat.
- **Picture, 16:9**: the frame as it is, with a 0.3 s fade in and out, and a lower-third with the
  level name (and "4 stars" when it ends in stars) for the first 2 s (drawtext,
  `/System/Library/Fonts/Helvetica.ttc`, inside the frame, at most two lines, not over the badge
  corner or the camera). Encode like the game's editions: `-c:v libx264 -preset medium -crf 16
  -profile:v high -pix_fmt yuv420p`, 30 fps, `-movflags +faststart`. h264_videotoolbox is fine for
  a quick preview, not for the delivered file.
- **Picture, 9:16**: the game as a strip across the top and the camera under it, the layout of the
  game's own social edition. The helper prints the rectangles and the ffmpeg filter for it:
  `moments.py --layout 1920x1080` (the camera is cropped out of the picture-in-picture, so it is
  upscaled: acceptable on a phone; the crop rectangle comes from the current settings, check on a
  frame that it is the camera and not the highway, the corner may have been different that
  night). The lower-third goes over the black band above the game strip.
- **Audio**: the video's, loudness-normalised like the editions:
  `-af loudnorm=I=-16:TP=-1.5:LRA=11 -c:a aac -b:a 256k` (the early streams went out at -42 LUFS).
  Never stretch the audio; a slow-motion repeat is picture only, or `atempo`, and only when asked.
- Keep the LIVE badge and the chat pane as they are (the stream as seen) unless the instructions
  say to crop them off.

## 4. Check and report

For every delivered file: `ffprobe -v error -show_entries format=duration:stream=width,height,r_frame_rate,codec_name -of compact`
(the right size, 30 fps, H.264, an audio stream, the expected length), one frame extracted at the
lower-third (`-ss 1`) and one from the middle, read as images: the text inside the frame, the
camera crop is the camera, nothing letterboxed by mistake. Fix and re-render anything wrong before
reporting.

Report in a short table: clip number, video in..out, what happens, the two files. Say which video
it was (name, date, length), the start and offset used, and what was left out. Do not post
anywhere. End with the user's next action: open the folder, watch the 9:16 ones on the phone, and
say which to distribute or what to change (`/twitch-clips <path> <the wish>`).
