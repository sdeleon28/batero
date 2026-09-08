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
level ends so the last fill does not navigate. Each drum acts at most every
220 ms, so a double trigger is one press, and hits softer than velocity 45
never navigate, so resting the sticks on the snare does nothing. The keyboard works everywhere too:
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
form with everything. Grooves are written in a small notation in `chart.py`
(sixteen slots per bar per instrument: `x` stroke, `X` accent, `o` ghost).

Exercises also hold the rudiments, practice-pad style on the snare: single
strokes in eighths and sixteenths, paradiddle at 70 and 90, paradiddle split
between hi-hat and snare, triplets, R L L triplets, double paradiddle,
paradiddle-diddle, doubles, and five accent-control exercises (accent on 1, on
e, on &, on a, and a moving accent). Each note carries its hand (R or L) and
the strip under the metronome shows the whole sticking with the stroke being
played lit up.

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
milliseconds, cached): a chord progression picked per level from ten, in a key
of its own, with a soft pad, a bass line, a plucked arpeggio and a generated
lead. Every four bars the section changes: pad and bass; the arpeggio comes in;
the lead answers; the bass syncopates and the arpeggio turns around; a
breakdown; a build with bouncing bass and sixteenth-note arpeggio; and so on,
cycling, with inversions and sevenths shifting underneath. The count-in gets
pad and sparse bass. Songs from MIDI files get no backing, since a made-up
progression would clash with the tune. Toggle it with B during play or from
Setup, or start with `--no-backing`.

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
- `drumhero/app.py`: menu, level select, wizard, play screen, main loop
