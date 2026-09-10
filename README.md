# drumhero

Guitar Hero style drum trainer driven by MIDI. Sibling of [hhmapper](../hhmapper).

Notes fall down their lanes towards the hit line. Play along on a MIDI drum kit
and get instant visual feedback the moment each hit lands, with the timing
error in milliseconds. Synthesized drum sounds play on every hit, and a guide
track plays the chart so you can hear what you are aiming at.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```
.venv/bin/python -m drumhero                      # auto-picks a port that looks like a drum module
.venv/bin/python -m drumhero --port TD-17         # pick the MIDI input explicitly
.venv/bin/python -m drumhero song.mid             # add MIDI files to the Songs section
.venv/bin/python -m drumhero --songs ~/mids       # scan another folder (default: ./songs)
.venv/bin/python -m drumhero --log hits.csv       # dump every judged hit of the last run
```

Without a MIDI input you can play with the keyboard: keys `1`..`9`, `0` hit
lanes 1..10.

## Navigating with the drums

The hub shows four colored sections, one per drum. Strike the drum to open it:

| drum   | section   | what is in it                          |
|--------|-----------|----------------------------------------|
| kick   | Exercises | one drum at a time, slow               |
| snare  | Beats     | full grooves                           |
| hi-hat | Songs     | MIDI files from `songs/` or the CLI    |
| crash  | Setup     | kit wizard, soundcheck, sounds, quit   |

Inside a section the drums are buttons, and the legend at the bottom of every
screen shows which does what, lighting up when you hit it:

- **hi-hat** moves down, **crash** moves up
- **snare** accepts, **kick** goes back

On the results screen the snare goes to the next level, the hi-hat retries and
the kick goes back to the list. Hits are ignored for the first second after a
level ends so the last fill does not navigate. Every hit is one action, immediately
(no debounce), and hits softer than velocity 25
never navigate, so resting the sticks on the snare does nothing. Every hit above
velocity 15 is still heard, so a roll played on a menu sounds whole. The keyboard works everywhere too:
arrows, Enter, Esc, and keys 1 to 4 on the hub. `h j k l` do the same as the
arrows, with `l` for Enter and `h` for Esc, for one-handed use.

## First launch: set up your kit

The first time a MIDI input is connected the wizard runs. It walks through every
zone of a TD-17 kit, one step each, head or bow before rim or edge:

| pad | zones |
|---|---|
| Kick | pad |
| Snare | head, rim |
| Hi-hat | bow, edge, pedal (the chick) |
| Crash L / Crash R | bow, edge |
| Rack tom / Floor tom | head, rim |
| Ride | bow, edge, bell |

Hit the zone a few times: every distinct note number heard in the next 1.5
seconds is assigned to it. The pad on screen flashes and shows the note number
and velocity of each hit; the list on the left shows what every zone got.

- Enter moves on early, S skips a zone you do not have, Backspace redoes the
  previous one, Esc cancels.
- A number heard in two steps goes to the later one, so a rimshot that leaks
  into the "snare head" step still ends up as "snare rim".
- The hi-hat's open and closed notes come in pairs: hear 46 (bow, pedal up) and
  42 comes along; hear 22 (edge, pedal down) and 26 comes along. So one strike
  per zone is enough, whatever the pedal was doing.
- The kit is saved to `~/.config/drumhero/kit.json` (`--kit` picks another
  file), one list of note numbers per zone. Kits saved by earlier versions are
  spread over the zones by their factory numbers. Rerun the wizard any time from
  the menu with "Set up kit".

Lessons so far only use kick, snare, hi-hat and crash; a chart's hi-hat notes
are satisfied by any hi-hat zone, crash notes by either crash. The other zones
are stored now so later lessons (rimshots, ride bell, toms) can use them, and
MIDI files that carry toms or ride get lanes bound to those pads.

## Soundcheck

After the wizard, and any time from Setup, the soundcheck screen shows the eight
pads with their zones. Hit each zone: its row lights up, the pad plays its sound
and the row shows the note number and velocity it sent. A note that is not
assigned to any zone is called out in red with its number so you can redo the
setup. The hi-hat widget under the cards shows the pedal openness and the last
stroke. Once every assigned zone has been heard, the snare continues to the hub
and the kick goes back to the wizard.

## Levels

Exercises are warm-ups, one or two drums at slow tempos: kick on the beat,
snare on the beat, snare on 2 and 4, hi-hat on the beat, crash on the beat,
kick and snare, alternating, hi-hat eighths.

Beats are a curriculum of seventeen grooves, each keeping what the one before
taught and adding one idea, at moderate tempos (use `[` and `]` for speed):
money beat; eighth-note hats; a second kick on the &; the four-bar phrase with
a snare pickup; crash on the one; the first fill; the rack tom enters in the
fill; the floor tom enters and the fill walks down; toms inside the groove; the
right hand moves to the ride; two crashes in conversation; half time; sixteenth
kicks; ghost notes; sixteenth hats; a linear groove; and a 16-bar A A B A song
form with everything. Then rock, metal and punk: fill vocabulary; kick doubles;
punk with the snare on the &; the gallop; riding the crash; the right crash on
the & of 4; sixteenth kick runs; whole-bar fills around the kit; a half-time
breakdown; three-over-four kicks; a lite blast; fills that land on a crash;
and a 16-bar rock anthem with a build from ghosts to accents. Grooves are
written in a small notation in `chart.py`
(sixteen slots per bar per instrument: `x` stroke, `X` accent, `o` ghost).

Exercises also hold the rudiments, practice-pad style on the snare: single
strokes in eighths and sixteenths, paradiddle at 70 and 90, paradiddle split
between hi-hat and snare, triplets, R L L triplets, double paradiddle,
paradiddle-diddle, doubles, and five accent-control exercises (accent on 1, on
e, on &, on a, and a moving accent). Each note carries its hand (R or L) and
the strip under the metronome shows the whole sticking with the stroke being
played lit up.

## Hi-hat control

Exercises end with ten hi-hat lessons that use everything the TD-17 hi-hat can
say: the pedal's openness (tight, mid, open, read from CC4 at the moment of the
stroke), the zone (bow or edge) and the foot (the chick). The articulations are
named exactly as hhmapper labels them, so a lesson that asks for "open edge" is
what GetGood Drums plays through hhmapper: tight body, tight edge, mid body,
mid edge, open body, open edge, pedal chick. The chick has its own lane.

Notes carry a glyph: `+` tight, `/` mid, `o` open, `>` on the edge, `^` the
foot. Each hit is judged on timing and on articulation: the right one earns a
bonus and shows its name under the note; a wrong one shows "want open body",
and "HAT: OPEN BODY" under the judgement. The HUD counts them and the results
give the rate, which feeds the grade like dynamics do. The game's own kit plays
open, mid, tight or chick samples according to what you actually played, and
the guide plays what the chart asks for. Lessons: tight and open, half open,
the openness ladder, open on the &, the bark, foot on 2 and 4, bow and edge,
edge on the open, sixteenths mid and tight, and a 16-bar hi-hat song.

## Accents

Rudiments judge dynamics as well as timing. Accented strokes are the tall notes
with a white outline and a `>`; taps are the narrow dim ones. A hit is an
ACCENT when it lands at velocity 88 or more on an accented note, a TAP at 84 or
less on an unaccented one; either earns a bonus. "ACCENT MISSING" and "TAP TOO
LOUD" are called out under the judgement, and a velocity in the band between
the thresholds is neither. The thresholds come from measured paradiddles on the
TD-17 snare: accents 88..124 (median 112), taps 29..84 (median 66). The hi-hat
pad reads much hotter (taps 64..96, accents 120..127), so on the hi-hat a tap is
104 or less and an accent 116 or more.

The HUD counts accents and taps and shows the contrast: median accent velocity
divided by median tap velocity over the last strokes, with the target (1.4x)
marked on the bar. The results screen repeats the tallies and contrast for the
whole run, and the `--log` CSV carries every hit's velocity and dynamic.

Songs are folders in `songs/` made by the ingest pipeline (see below), or plain
MIDI files dropped in `songs/` or given on the command line. General MIDI drum
numbers fold into the four instruments (36 kick, 38 snare, 42/44/46 hi-hat,
49/57 crash); any other note number gets a lane of its own, matched by raw
number. A song with audio plays the recording along with the chart, on the
recording's own beat grid, with the guide sounds off since the record already
has drums.

## Adding a song: the sourcing pipeline

1. Make `songs/<name>/` and drop the recording there as `audio.mp3` (or wav,
   ogg, flac). Recordings are yours; they are git-ignored.
2. Write `songs/<name>/song.json`: title, artist, a bpm hint, and the form as a
   list of sections with a bar count and a drum pattern each (`rest`, `hats`,
   `kick_snare`, `rock`, `rock_pickup`, `rock_quarters`, `halftime`,
   `eighth_kick`), plus `crash` and `fill` flags. This is the step to do with
   Claude: describe the song and let it draft the form.
3. Run the ingest:

   ```
   .venv/bin/python -m drumhero.ingest songs/<name>
   ```

   It tracks the beats with librosa, takes the beat nearest the first strong
   onset as the first downbeat (set `offset_hint` in song.json, in seconds, if
   the song starts with a pickup), lays the sections on that grid and writes
   `chart.mid` with a tempo change on every beat, so the chart follows the
   recording even where it drifts. It also writes `beats.json` and fills in
   `offset` in song.json.
4. Play it from Songs. If a section is a bar too long or short, fix the number
   in song.json and run the ingest again.

`songs/i-wont-back-down/` has the form for Tom Petty's song written from
memory as a starting point; add the recording and ingest it.

Each level starts with a one-bar count-in with clicks. The level select shows
your best accuracy and mean timing for the session.

## Judgement

| error            | judgement | score |
|------------------|-----------|-------|
| within 25 ms     | PERFECT   | 100   |
| within 60 ms     | GOOD      | 50    |
| within 100 ms    | OK        | 20    |
| beyond 100 ms    | STRAY     | resets combo |
| note never hit   | MISS      | resets combo |

Score gets a multiplier of `1 + combo // 10`. A hit is matched to the nearest
pending note in its lane; a note can only be hit once.

The error shown is `hit time - note time`: negative is early, positive is late.

## Stars

Every run gets a grade from 0 to 100: half from accuracy (notes hit), a third
from hit quality (PERFECT counts 1, GOOD 0.6, OK 0.3), a fifth from dynamics
on levels that judge them (otherwise quality again), minus two points per
stray in a hundred notes. Grades 30, 50, 70, 85 and 94 earn one to five stars.
The best grade per level is saved to `~/.config/drumhero/progress.json` and its
stars show next to the level in the lists, with totals on the hub cards. When
a level ends the backing stops and a jingle plays for the star count: a sag for
none or one, a plain cadence for two, a bright one for three, a rising fanfare
for four, and for five the full victory fanfare with drums.

## Recording a take (V)

Press V anywhere (or "Recording" in Setup) to start a take, V again to stop.
Every take is its own folder in `~/Movies/drumhero/`, the raws in `raw/` and
two editions rendered from them when the take stops (both, every time; you
manage the disk):

```
20260909-173554 Paradiddle/
    take.json          when, how long, the levels played, where everything is
    raw/screen.mp4     the game exactly as it was on screen
    raw/camera.mp4     the iPhone, 1920x1080
    raw/audio.wav      the interface's mix
    computer.mp4       16:9: the screen with the camera picture-in-picture
    social.mp4         1080x1920 for Reels / TikTok / Shorts: the screen as a
                       thumbnail across the top, the camera under it
    edits/             what Claude cuts from an edition (see below)
```

The social edition keeps the screen as it was (the notes, the judgement words
and the combo stay readable on a phone; the HUD's small text gets small) and
gives the camera `capture_split` of the height (half by default, cropped to
fill; 0.32 shows the whole camera picture), the pair centred vertically. Either
edition can be rendered again later from the raws: "Takes" in Setup, or
`python -m drumhero.capture --render "<take folder>" social`.

- **Picture**: the game hands a copy of its own frame to a writer thread 30
  times a second, piped into ffmpeg (H.264 by VideoToolbox). No screen
  recording permission and no display to choose; the main loop only pays for
  one blit.
- **Sound**: the interface's own input, two channels of it (defaults: device
  "X18/XR18", channels 17 and 18). On the XR18 set USB sends 17/18 to Main L/R
  in X-AIR Edit and those channels carry exactly the mix you monitor, the game
  and Bitwig included. `python -m drumhero.capture` records two seconds and
  shows the level of every channel so you can confirm the routing.
- **Camera**: the first avfoundation video device whose name contains the
  `capture_camera` setting ("iPhone": Continuity Camera or Camo) is recorded by
  ffmpeg to its own file and overlaid at stop, aligned by wall clock. The first
  time, macOS asks the app for camera permission; without a camera the take is
  picture and sound only.
- At stop the raws move into the take's folder and both editions render in
  the background ("rendering ... edition" bottom right); the raws stay either
  way.

Settings: `capture_audio_device`, `capture_audio_channels`, `capture_camera`,
`capture_pip` (camera height as a fraction of the picture, 0.28) and
`capture_corner` for the computer edition, `capture_split` (the camera's share
of the height, 0.5) for the social one.

## Devices: hot-plug and toasts

A watcher thread polls the MIDI inputs and audio outputs every 1.5 s and the
cameras every 12 s (not while a take records). When the TD-17, the audio
device from settings or the camera appears or goes away, a toast slides in at
the top right and the game reacts: the TD-17 is opened as soon as it shows up
(plug it in after launch and the drums go live), the mixer moves back to the
saved device when it returns and falls back to the system default when it
goes, the camera is marked available for takes. Any other MIDI device that
appears gets a quiet mention. The hub's footer shows the three devices with a
green dot when present.

## Progress (S)

The hub shows a strip with your streak, today's minutes and the total; S (or
"Progress" in Setup) opens the progress screen: day streak and best streak,
minutes today and overall, notes hit, stars; minutes per day this week; the
accuracy, timing-consistency and grade trends over the last runs; records (best
run, tightest timing, longest combo); and the last runs with their stars. It is
all read from the run logs, header lines only, so it costs nothing.

## Coach (C)

C (or "Coach" in Setup) sends a compact report to Claude: the catalogue of
levels, every level you played with best and mean results, the last runs in
detail and your practice habit. Claude answers, in your language, with your
strengths and weaknesses (each citing the evidence), the focus for the next
session, a weekly diet, and three playlists: warm-up and fundamentals, one that
attacks the main weakness, one that stretches toward the next curriculum step.
Each playlist item names a level, a tempo rate and repetitions. Snare or Enter
starts a playlist as a session: the game runs its levels in order at the given
tempo, shows "session x/y" and why the level is there, and returns to the coach
when it is done. A asks again. The analysis is kept in
`~/.config/drumhero/coach/coach.json` (`coach_language`, `coach_model` in
settings).

## Camera & take check

Setup has "Camera & take check", the soundcheck of takes: the game picture on
the left, the camera live on the right (ffmpeg feeds the iPhone's frames into
the game at 15 fps), both editions below as they will land (the computer one
with the picture-in-picture, the social one with the screen thumbnail over the
camera), and the take's audio channels metered next to them, with a hint when
they are silent. Hi-hat and crash change the picture-in-picture size (20, 28,
36 or 45 % of the height), `[` and `]` move it between the four corners, S
cycles the camera's share of the social edition (32, 40, 50 or 60 %), R
rescans the cameras, and snare or Enter records a 3-second test take, renders
its editions and shows the result. Sizes, corner and share are saved.

## Takes: editions and Claude edits

Setup has "Takes: editions, Claude edits". Pick a take (newest first, `[` and
`]` change it; the line under it says which editions exist) and either an
edition to render again from the raws (computer or social) or a style for
Claude, no typing:

| style | what Claude makes |
|---|---|
| Hype video | under 60 s, bold title, the best streaks and fills, beat-synced cuts, speed ramps, ends on the stars |
| Highlights | the best 30..90 s in order, lower-thirds, results at the end |
| Full, polished | the whole take with a title, lower-thirds and fades, no cuts |
| Lesson | the whole take, every fill repeated at half speed with captions, wrong dynamics and misses captioned |
| Raw with a title | a title card and an end card with the stars |

Claude edits the edition of the take that matches your window (the computer
one), or the other if only that one exists. The game writes a job folder in
the take's folder, `edits/<edition>-<style>/` (`~/Movies/drumhero/edits/<take>/`
for takes recorded before the folders existed) with `job.json` (the edition,
the take's `take.json`, the run logs of the levels played during it, the style
and its brief) and starts Claude Code there in print mode,
allowed to use ffmpeg, ffprobe and files only. The run logs give Claude every
hit with a wall-clock time, so it can cut on downbeats, find PERFECT streaks
and caption misses. The result is `edit-<style>.mp4` next to `notes.md`
(the edit decisions) and `claude.log`; the state shows bottom right while it
runs. Needs the `claude` CLI on the PATH (`claude_bin` in settings otherwise).

## Run logs and auditing

Every level you play is written, when it ends, to
`~/Library/Logs/drumhero/runs/<date-time> <level>.jsonl`: a header with the chart
(every note with its time, instrument and accent), the lanes, the kit, the
settings, the offset, the tempo rate and the judging thresholds, then one line per
MIDI message, ghost, hit (judge, error, velocity, dynamic), miss and change.
Recording is an in-memory append per event and the file is written once at the
end, so the main loop and the MIDI thread never wait on disk.

```
.venv/bin/python -m drumhero.audit                  # the latest run: timing, dynamics, wrong calls
.venv/bin/python -m drumhero.audit --hits           # every hit
.venv/bin/python -m drumhero.audit --hihat-tap 104  # re-judge the run's dynamics with other thresholds
```

## Drum sounds off: play through Bitwig

"Drum sounds" in Setup (or D while playing) silences the game's own kit: hits,
guide and menu navigation stop making sound, while the metronome, backing and
menu music stay. Use it when the TD-17 module or Bitwig with GetGood Drums
(fed by the sibling hhmapper) is the drum sound, so nothing plays twice. Saved
in settings.

## Tempo

`[` and `]` slow the level down or speed it up in steps of 10 % (0.3x to 2x)
and restart it at the new tempo. Everything follows: the notes, the metronome,
the backing loop, and for songs the recording itself, time-stretched with
librosa's phase vocoder (pitch kept; a few seconds of work the first time for
each tempo). The notes scroll slower too, so a beat is always the same distance
on screen. The HUD shows the multiplier and the effective bpm. `--speed 0.5`
starts every level at half tempo.

## Calibration

The MIDI input itself is instant (measured: the game judges a note within 2 ms
of its arrival at the Mac). What adds latency is the reference you play to: the
metronome and backing come out after the mixer buffer and the interface, and the
display draws the falling note a frame or two late. Playing to what you see and
hear therefore reads "late" by a constant amount, and the offset compensates
for that chain. Press `.` to add 5 ms (hits are treated as earlier) or `,` to
subtract, until the mean on the results screen sits near 0. The value is shown
in the HUD, saved to settings and used from then on; `--offset 20` overrides it
for one run.

The results screen and `--log` CSV give the mean and standard deviation of your
timing errors; the mean is your latency, the deviation is you.

## Controls

| key           | action                             |
|---------------|------------------------------------|
| arrows, Enter | navigate menus                     |
| Esc           | back                               |
| F11, Cmd+F    | fullscreen on / off                |
| Space         | pause / resume                     |
| R             | restart the level                  |
| Enter         | next level, on the results screen  |
| G             | guide track on / off               |
| B             | backing loop on / off              |
| M             | metronome full / beats / off       |
| h j k l       | same as the arrows, Enter and Esc in menus |
| [ / ]         | tempo -/+ 10 % (restarts the level) |
| , / .         | input offset -/+ 5 ms              |
| D             | drum sounds on/off (saved)         |
| V             | start / stop a take (screen + mix + camera, both editions rendered) |
| { / }         | game volume -/+ 5 % (saved), any screen; the mixer fader stays the overall level |
| S / C (hub)   | progress / coach                   |
| 1..9, 0       | hit lanes from the keyboard        |

`--no-sound` disables audio entirely, `--no-guide` starts with the guide track
off. If the audio crackles, raise `MIXER_BUFFER` in `drumhero/sounds.py`.

## Metronome

Every level has a metronome that follows the subdivision being practised, with
a visual counter and a sound that sits inside the mix.

**The convention.** A chart's subdivision is the coarsest grid that covers its
note onsets: quarter notes, eighths, triplets or sixteenths. It is decided per
four-bar phrase, so a song can move from eighths in the verse to sixteenths in
a fill section and the metronome follows. Built-in levels infer it from their
notes the same way; a level can force it with `subdivision=` in `_build`. MIDI
songs get it automatically, no tagging needed. Humanized files are fine: an
onset counts as on the grid within 12% of a step, and 95% of onsets must fit.

**The counter.** Four squares at the top, one per beat, each split into the
subdivision with the counting syllables: `1 e & a` for sixteenths, `1 &` for
eighths, `1 trip let` for triplets. The current cell lights up, white on the
beat and blue on the subdivisions, fading over the cell's duration. It runs
through the count-in too.

**The sound.** Congas rather than clicks: a low conga on the downbeat, a mid
conga on beats 2 to 4, a soft muted tap on the subdivisions, all synthesized
with a pitch drop and gentle saturation so they stay round over a long session.
The whole metronome is pre-rendered for the level, sample-accurate, and started
with the count-in; after a pause it restarts from the exact position. Modes:
`full` (subdivisions), `beats` (beats only), `off`. Press M during play or
change it in Setup; `--no-metronome` starts off.

## Menu music

Outside the game an ambient loop plays: drifting seventh chords, a sparse
pentatonic pluck with echoes and a low hum, 32 seconds, synthesized at first
use. It fades out when a level starts. Toggle in Setup or start with
`--no-menu-music`.

## Backing

Built-in levels play over a synthesized backing so they feel like music. It is a
small arrangement rendered once for the whole level (a few hundred
milliseconds, cached). Each level gets a style, cycling through seven so
neighbouring levels sound different:

| style | harmony | parts |
|---|---|---|
| synth | pop and minor progressions | saw pad, sine bass, plucked arpeggio, sine lead phrases |
| metal | phrygian power chords, tritones | distorted chugs and gallops, dark pad, saw-lead riff |
| chiptune | pop | square bass, square sixteenth arpeggios, pentatonic square riff |
| punk | power-chord I IV V | picked bass on the eighths, strummed distorted chords |
| organ | seventh chords | organ pad, sub bass, bell stabs, dorian triangle lead |
| strings | cinematic minor with sevenths and sus2 | slow string pad, whole-note bass, bells, long saw-lead notes |
| funk | minor sevenths | slap-style bass line, clav stabs, pentatonic riff |

Every four bars the section changes along the style's own plan (parts come and
go, bass patterns switch, a breakdown, a build), the key is transposed per
level, and the riff or phrase is generated per level. The count-in gets a thin
intro. Songs from MIDI files get no backing, since a made-up progression would
clash with the tune. Toggle it with B during play or from Setup, or start with
`--no-backing`.

## Install as a Mac app

```
.venv/bin/python install_app.py
```

builds `/Applications/drumhero.app` so the Dock, Spotlight, rcmd and other app
switchers see drumhero as an installed application with its own name and icon,
and you can give it a shortcut. The bundle's executable is a copy of the venv's
base Python, and a tiny venv config inside the bundle points it at this repo,
so nothing but the interpreter is copied: edits to the code take effect on the
next launch. Run the installer again after moving the repo or recreating
`.venv`. Output goes to `~/Library/Logs/drumhero.log`. Pass a folder to install
somewhere else, for example `~/Applications`.

## Audio output

Setup has an "Audio output" item that cycles through the system's output
devices (an audio interface, the display, the speakers) and remembers the
choice in `~/.config/drumhero/settings.json`. `--audio-device XR18` picks one
by name for a single run. The mixer is reopened on the new device and every
sound is rebuilt, so it works mid-session. An unknown name falls back to the
system default with a message in the log.

## Fullscreen

The app starts in native macOS fullscreen by default; "Start fullscreen" in
Setup turns that off (saved), and `--windowed` overrides it for one run. The
window is resizable, so it has the green traffic-light button, and F11 or
Cmd+F toggle fullscreen at any time. Both use desktop fullscreen (its own
Space), never the exclusive mode: that one switches the monitor to the
window's resolution and disables trackpad gestures, and if the app dies it can
leave the monitor at 1280x720. Leaving fullscreen returns to the window size
you had before. Every screen relayouts and rescales to the window height,
so 1080p fullscreen looks like the 720p window, only bigger.

## Feedback latency

The render loop runs uncapped (target 240 fps, no vsync) and the MIDI callback
judges the hit and triggers its sound on the MIDI thread, so the flash appears
on the very next frame after the note arrives, typically under 5 ms plus
whatever the display adds. The audio mixer uses a 256-sample buffer, about 6 ms.

## Layout

- `drumhero/chart.py`: notes, built-in levels, MIDI loading, lanes
- `drumhero/game.py`: clock, judging, misses, guide track, stats
- `drumhero/render.py`: the play field
- `drumhero/sounds.py`: synthesized kit
- `drumhero/kit.py`: kit file
- `drumhero/runlog.py`, `drumhero/audit.py`: per-level run logs and the audit tool
- `drumhero/capture.py`, `drumhero/edit.py`: recording a take, editing it with Claude Code
- `drumhero/stats.py`, `drumhero/coach.py`: progress statistics, the coach and its playlists
- `drumhero/devices.py`: device watcher and toasts
- `drumhero/app.py`: menu, level select, wizard, play screen, main loop
