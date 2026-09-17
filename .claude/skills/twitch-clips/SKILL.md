---
name: twitch-clips
description: Download the latest Twitch stream (VOD) of the user's channel through Chrome and cut it into clips for distribution (16:9 and 9:16), on the game's own data about what happened. Use when the user asks for clips of a stream, to download or edit a VOD, or to "editar el último stream". Takes optional instructions (which stream, which parts, how to edit).
argument-hint: "[which stream, which parts, how to edit; default: the latest finished VOD, moments picked from the run logs]"
---

# Clips from the latest Twitch stream

Instructions from the user for this run (empty means the defaults): $ARGUMENTS

The channel is `twitch_channel` in `~/.config/drumhero/settings.json` (xantwav). The stream is the
display as the viewers saw it: the game full frame, the camera as a picture-in-picture in a corner,
the LIVE badge top right, sometimes the chat pane bottom left. Every stream's audio also stays as an
AAC copy in `~/Movies/drumhero/streams/<local start stamp> stream.aac`.

## Rules

- **Never start or stop the stream**: no T, no `python -m drumhero.twitch --stop`, no touching
  the daemon. If a stream is live (`.venv/bin/python -m drumhero.twitch --status`), the newest VOD is
  still being written: use the previous one and say so, unless the instructions ask for the live one.
- **Never publish**: the clips land in a folder, the user distributes them. Do not upload, post,
  or share anything, and do not touch the Twitch settings.
- The browser is for looking. Downloads go through yt-dlp in the shell, never through the page's
  Download button. If Twitch asks for a login, the user logs in themselves.
- Never leave a clip unwatched: every delivered file is probed and one frame of it looked at.

## 1. Which stream

Load the browser tools in one call (`ToolSearch` with
`select:mcp__claude-in-chrome__tabs_context_mcp,mcp__claude-in-chrome__tabs_create_mcp,mcp__claude-in-chrome__navigate,mcp__claude-in-chrome__read_page,mcp__claude-in-chrome__get_page_text,mcp__claude-in-chrome__tabs_close_mcp,mcp__claude-in-chrome__list_connected_browsers,mcp__claude-in-chrome__select_browser`).
Two Chromes are usually connected on this Mac: list them and let the user pick (the
list_connected_browsers rule), then `tabs_context_mcp`, a new tab, and open the Video Producer,
which lists the channel's past broadcasts with title, date, length and status:

    https://dashboard.twitch.tv/u/<channel>/content/video-producer

Read the list. The latest **finished** past broadcast is the default (a row still "Live" is the
running stream). If the instructions name a stream (a date, a title, a VOD id or URL), take that
one. The VOD id is the number in its link (`twitch.tv/videos/<id>`); the URL to download is
`https://www.twitch.tv/videos/<id>`. Close the tab when done.

If no Chrome is connected, or the page cannot be read, fall back to the public list:

    yt-dlp --no-update --flat-playlist --playlist-end 3 -J "https://www.twitch.tv/<channel>/videos?filter=archives&sort=time"

(entries newest first with id, title, duration and url; warnings go to stderr, parse stdout only).

If the channel has no past broadcasts at all, "Store past broadcasts" is off in the Twitch
settings (Settings, Stream, VOD settings): tell the user, there is nothing to download.

## 2. Download

Metadata first, then the video, into `~/Movies/drumhero/vods/`. yt-dlp's `timestamp` is when
Twitch started recording (epoch; measured 2026-09-16: 13 s after the daemon's `started` in
`~/.config/drumhero/stream.json`), `duration` the length, `is_live` true while the stream is still
running (then it is not the one to cut). yt-dlp's ids carry a `v` (`v2876217952`); the URL takes
the number. The file is named with the **local** stamp of `timestamp` (the `%(timestamp>...)s`
template formats in UTC, do not use it):

    mkdir -p ~/Movies/drumhero/vods && cd ~/Movies/drumhero/vods
    yt-dlp --no-update -J "https://www.twitch.tv/videos/<id>" > "<id>.json"
    stamp=$(date -r <timestamp> +%Y%m%d-%H%M%S)
    yt-dlp --no-update -N 8 -f "bv*[height<=1080]+ba/b[height<=1080]" --merge-output-format mp4 \
        --no-overwrites -o "$stamp <id>.%(ext)s" "https://www.twitch.tv/videos/<id>"

Skip the download if the file is already there. A 1080p stream is about 2.7 GB per hour; when the
instructions name the parts and the VOD is long, download only those parts instead
(`--download-sections "*1:02:30-1:04:00"`, one per part, with `-o` naming the range) and cut from
them. The chat is a signal for the highlights and comes as subtitles:
`yt-dlp --no-update --write-subs --sub-langs rechat --skip-download -o "<id>" <url>` (a JSON of
messages with `content_offset_seconds`; a burst of messages marks a moment). If yt-dlp cannot extract Twitch
(their site changes often): `brew upgrade yt-dlp`, retry; a subscriber-only VOD needs
`--cookies-from-browser chrome`.

## 3. The VOD's clock against the game's

VOD time = wall time - `timestamp`. The helper does that for the run logs and the takes:

    .venv/bin/python .claude/skills/twitch-clips/moments.py --vod ~/Movies/drumhero/vods/<id>.json [--json]

It prints every level played during the VOD on the VOD's clock (name, bpm, grade, stars, max combo,
misses, longest PERFECT/GOOD streak and where it ends), every take the drummer chose to record, and
suggested clips (levels with 2 stars or more, 2 s of run-up, 4 s of results screen, best first).
`--json` gives the same with `chart_zero` per level (VOD time of the chart's beat 0, so a downbeat
is at `chart_zero + k * 4 * 60 / bpm`) and the run log paths, for cutting on the beat.

Check the alignment once before cutting: a level's first hit should land where the note reaches
the line on the picture (extract a frame at that VOD time with `ffmpeg -ss T -i vod.mp4 -frames:v 1
frame.png` and look at it). Twitch's `timestamp` is usually within a second or two of the first
frame; if it is off, the exact offset is the AAC copy against the VOD's audio (same content,
`~/Movies/drumhero/streams/<stamp> stream.aac`, stamp = local time the stream started): decode 60 s
of each around a loud moment with ffmpeg to mono 8 kHz PCM and cross-correlate with numpy (in the
venv), then pass the result as `--offset`. Write the offset used in notes.md.

## 4. Which moments

With instructions, follow them: parts as VOD times ("1:02:30-1:03:10"), as descriptions ("the four
stars", "the last level", "when the plena finally passed"), or as a style ("hype under 60 s",
"one long highlight", "only the 9:16"). Map descriptions onto the helper's list.

By default, 3 to 6 clips, from these, in this order of preference:
1. The best runs (most stars, then grade): the whole level from the run-up to the stars on the
   results screen. The run-up shows the phrase's landing, keep 2 s of it.
2. A level passed after failing it: the fail's last bars, a cut, the pass (a story in one clip).
3. The longest PERFECT/GOOD streak of the night, if it is inside a run that did not make the cut.
4. Takes the drummer recorded during the stream: they chose those moments.
5. Chat bursts (from the rechat file), if any.
Clip lengths: 15..60 s for the 9:16, up to 90 s for the 16:9. Nothing shorter than 10 s, nothing
over 3 minutes unless asked. Skip a level under 10 s (an abandoned start).

## 5. Cut and edit

Output folder: `~/Movies/drumhero/clips/<YYYYMMDD> <stream label>/` (the VOD's local date, the
label from the title or the main level, like the existing `20260913 31 Plena 100`). Two files per
clip, numbered in VOD order: `NN <what happens> computer.mp4` (1920x1080) and `NN <what happens>
social.mp4` (1080x1920), plus `notes.md`: VOD in/out of each clip, what happens, why it was chosen,
the offset used, and anything left out and why. Never modify the VOD.

- **Cuts on the beat**: start a clip on a downbeat of the run-up (chart_zero minus whole bars) and
  end 3..4 s after the results screen appears; a hard cut inside a level goes on a downbeat.
- **Picture, 16:9**: the VOD frame as it is, with a 0.3 s fade in and out, and a lower-third with
  the level name (and "4 stars" when it ends in stars) for the first 2 s (drawtext,
  `/System/Library/Fonts/Helvetica.ttc`, inside the frame, at most two lines, not over the badge
  corner or the camera). Encode like the game's editions: `-c:v libx264 -preset medium -crf 16
  -profile:v high -pix_fmt yuv420p`, 30 fps, `-movflags +faststart`. h264_videotoolbox is fine for
  a quick preview, not for the delivered file.
- **Picture, 9:16**: the game as a strip across the top and the camera under it, the layout of the
  game's own social edition. The helper prints the rectangles and the ffmpeg filter for it:
  `moments.py --layout 1920x1080` (the camera is cropped out of the VOD's picture-in-picture, so it
  is upscaled: acceptable on a phone; the crop rectangle comes from the current settings, check on
  a frame that it is the camera and not the highway, the corner may have been different that
  night). The lower-third goes over the black band above the game strip.
- **Audio**: the VOD's, loudness-normalised like the editions:
  `-af loudnorm=I=-16:TP=-1.5:LRA=11 -c:a aac -b:a 256k` (the early streams went out at -42 LUFS).
  Never stretch the audio; a slow-motion repeat is picture only, or `atempo`, and only when asked.
- Keep the LIVE badge and the chat pane as they are (the stream as seen) unless the instructions
  say to crop them off.

## 6. Check and report

For every delivered file: `ffprobe -v error -show_entries format=duration:stream=width,height,r_frame_rate,codec_name -of compact`
(the right size, 30 fps, H.264, an audio stream, the expected length), one frame extracted at the
lower-third (`-ss 1`) and one from the middle, read as images: the text inside the frame, the
camera crop is the camera, nothing letterboxed by mistake. Fix and re-render anything wrong before
reporting.

Report in a short table: clip number, VOD in..out, what happens, the two files. Say which stream it
was (title, date, length), the alignment offset, and what was left out. Do not post anywhere. End
with the user's next action: open the folder, watch the 9:16 ones on the phone, and say which to
distribute or what to change (the skill takes instructions: `/twitch-clips` plus the wish).
