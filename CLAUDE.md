# drumhero

Guitar Hero style drum trainer (pygame + mido). Run with `.venv/bin/python -m drumhero`.
Sibling of `../hhmapper`; same conventions: own `.venv`, reply ends with the user's next action.

## Git: commit and push when a task is done (required)

Commits are allowed without asking. When a task is finished and verified (and deployed, see
below), commit it and `git push origin master` without being asked. `origin` is github
`sdeleon28/batero`; the branch is `master`.

## Deploy after every change (required)

The user launches the game through an rcmd shortcut bound to `/Applications/drumhero.app`,
and expects that shortcut to always open the latest code. After any change to this repo
(code, assets, dependencies), run:

```
./deploy.sh
```

It rebuilds the bundle with `install_app.py` and, if the app was running, quits and
relaunches it. Do this before reporting the change as done. The bundle does not copy
the code (it points at this repo), but a running instance keeps the old code loaded,
so the relaunch is what matters.

## Twitch stream (T), the badge, and the ! layer (camera + chat)

`drumhero/twitch.py`. `T` starts and stops the stream (also the Setup row "Stream (T)" and the
x next to the LIVE badge). **The stream never starts on its own, never in a test, never from a
script: only the user presses T, and Claude does not press it for them.** For pipeline checks use
`python -m drumhero.twitch --selftest [--source screen]` (a local rtmp listener, no Twitch;
prints the received audio's level, so silence is caught) and
`python -m drumhero.twitch --chat xantwav` (reads the chat, no account needed).

- **The stream is its own process** (since 2026-09-13, so deploy.sh relaunching the game does not
  kill a live stream): T spawns `.venv/bin/python -m drumhero.twitch --daemon --settings JSON` in
  a new session (`twitch.daemon`), which runs the `Streamer` on the screen source and writes
  `~/.config/drumhero/stream.json` twice a second (pid, when it started, ffmpeg's progress); its
  output goes to `~/Library/Logs/drumhero/stream.log`. The game holds a `StreamLink` (`app.streamer`):
  `phase` is off / starting / live / stopping from the state file and the pid; a game started under a
  live stream attaches to it (toast "stream live since hh:mm", the layer comes on); quitting the game
  leaves the daemon alone; `stop()` sends SIGTERM and never blocks (the daemon stops ffmpeg cleanly,
  a few seconds; SIGKILL plus a `pkill -f drumhero-stream-` after 10 s if it ignores it). A daemon
  that dies on its own (or is killed) is reported "stream ended: ..." from the state file's error,
  and its leftover capture is killed. `python -m drumhero.twitch --status` / `--stop` from a
  terminal. The "window" source is still the in-process Streamer and dies with the game (it is the
  game's frames); a daemon refuses to start while another is live.
- **The badge** (`drumhero/badge.py`) is the daemon's own window, so it is on screen exactly while
  the stream process lives, game or no game: Cocoa through PyObjC (`pyobjc-framework-Cocoa` in the
  venv), a borderless transparent window at the maximum window level, on every Space
  (CanJoinAllSpaces, FullScreenAuxiliary: it shows over the game's fullscreen Space and follows a
  Mission Control switch), one per display, top right under the menu bar (at the very top when a
  fullscreen Space hides it; re-placed every half second), no Dock icon, never takes the focus. A
  red pill "LIVE mm:ss" with a blinking dot; amber STARTING until ffmpeg reports, dim STOPPING;
  after an unrequested end "STREAM ENDED · reason" stays until its x is clicked or another stream
  goes live. Speed under 0.95x and dropped frames appear in amber inside the pill. The x next to it
  stops the stream like T (the daemon's SIGTERM path). The game draws no badge of its own; the
  play HUD's right column starts under the corner while the stream lives (`Renderer.top_inset`
  from `App.badge_height`). Viewers see the badge too (the stream is the display).
  `python -m drumhero.badge --demo` shows one for 10 s. Without PyObjC the daemon still streams,
  badge-less. Tests give the daemon its own state file with `DRUMHERO_STREAM_STATE`.
- **The ! layer** (`App.set_layer`, `layer_on`): the streamer's overlay over any screen, independent
  of the stream: the camera as the computer edition's picture-in-picture (`capture_pip` of the
  height in `capture_corner`, 30 fps preview, border purple while live, red while a take records
  and it shows the recorder's own frames) and the chat pane (bottom left, the velocity viewer's
  place, above it when both are on; bottom right when the camera has the bottom-left corner). The
  chat connects when the layer turns on and closes when it turns off. Going live (or attaching to a
  live stream) turns the layer on, because the stream is the display and this is how the iPhone
  gets into it; ! hides it again during the stream if wanted. A layer the stream turned on goes
  off when the stream ends (`layer_auto`), so the camera preview's ffmpeg is released and
  Continuity Camera disconnects; a layer the user switched with ! stays. The camera check screen hides the
  layer (it has its own picture). This replaced the old camera monitor (a small 15 fps picture)
  and the chat-tied-to-the-stream of 2026-09-12.
- Source (`stream_source`, default "screen", since the first live tests 2026-09-12): ffmpeg
  captures display `stream_display` (0) through avfoundation, so the terminal or anything else
  on that display goes out too; the interface's mix comes from a separate feeder process
  (`python -m drumhero.twitch --audio-feed`, a sounddevice input with the take's channels and
  buffer settings) through a named pipe; both inputs on the wall clock (`-copyts`, setpts
  minus the launch time), `aresample=async` for drift. Needs the Screen Recording permission
  for drumhero.app (macOS asks once, then relaunch the app). Every stream keeps an AAC copy of
  its audio in `~/Movies/drumhero/streams/` to check afterwards.
  **Never capture the interface's audio with ffmpeg's avfoundation**: measured 2026-09-12 with
  the native buffer timestamps, it drops buffers even capturing audio alone (6.1: 1.3 s lost
  in 15 s; 8.0.1: 0.67 s) because it keeps one pending buffer and sleeps 10 ms between reads;
  viewers heard those as pops. The feeder loses 0..2 ms in 15 s. And never a sounddevice
  input inside the game process for the stream: the callback waits for the GIL and the
  window source logged 0.25 s stalls; the headphone return also chopped that night.
  "window" stays as the fallback (`stream_source: "window"`).
- What goes out is the display as the user sees it, every overlay included (the layer, the
  toasts, the velocity viewer, the badge): not an edition, no social layout.
- Video: 30 fps, H.264 on VideoToolbox, `stream_height` (1080) and `stream_kbps` (6000) from
  settings, keyframe every 2 s, FLV over RTMPS to Twitch's ingest. Audio: the same interface
  channels as the take (X18/XR18, USB 17/18 = Main L/R) through the feeder, float32 stereo,
  padded with silence onto the stream's clock.
- The key lives in `~/.config/drumhero/twitch_key` (one line). Never log it, never copy it
  into settings.json or the repo; `twitch.redact` strips it from ffmpeg's messages.
  `stream_url` in settings replaces the whole URL (tests); `stream_bandwidth_test` appends
  `?bandwidthtest=true` (Twitch takes the stream without going live, inspector.twitch.tv).
- Chat: anonymous IRC over TLS, `justinfan` nick, reconnects by itself. Writing to the chat would
  need a token; not built.
- Network measured 2026-09-12 on Wi-Fi (en1): uplink 14..24 Mbps, responsiveness low
  (2.6..3.7 s under load). The user has two USB Ethernet adapters; a wired link is the fix
  if the stream drops frames. Ingest TCP round trip 47..51 ms.

## Waiting screen (`cortina`)

`drumhero/agi.py`, a separate terminal program, for the moments the user leaves
the camera. It runs as `cortina` from any terminal: `~/bin/cortina` is a symlink
to `./cortina` in this repo, which resolves the symlink before cd-ing here (so
`$0` is not `~/bin`) and execs `.venv/bin/python -m drumhero.agi`. The user named
it, and that is the name to use for it:
a Twitch-style be-right-back card (EN VIVO badge, channel, AFK timer, big title,
a rotating line of excuses) over six scenes that cycle every 26 s. The stream
captures the display, so a full-screen terminal on display 0 is what goes out;
nothing of this touches the game or the stream code. Keys: q, space, 1..6, p, b,
h, f. `--title`, `--note`, `--scene`, `--seconds`, `--fps`, `--no-card`.

- Each cell is the half block U+2580 with the top half as the foreground colour
  and the bottom as the background, so the canvas is cols x 2*rows pixels in
  24-bit colour; a sparse layer of real characters on top keeps the HUD and the
  labels crisp. Everything is composited additively in linear light and tone
  mapped at the end, which is why the palette constants look so dark.
- Two things keep it cheap enough for 30 fps at 212x58 (measured: 4..14 ms per
  frame, 0.1..6.8 MB/s to the terminal): only the cells whose colour moved by
  more than 3/255 are rewritten (and what is remembered is what was written, so
  a slow drift still arrives), and a cell whose halves match is a space with one
  colour instead of a half block with two. `splat()` scatters points with
  np.bincount, or np.add.at when there are few enough that allocating a canvas
  per channel would cost more.
- The big title is rasterised by pygame (already a dependency) into a mask:
  `font.render(..., True, ...)` puts the shape in the ALPHA channel, so it is
  `surfarray.array_alpha`, not `array3d`. Set PYGAME_HIDE_SUPPORT_PROMPT before
  importing pygame or the banner lands on the alt screen.
- The music is `drumhero/keygen.py`, its own renderer (not `make_arrangement`,
  which has no drums because it plays under a drummer): four minor chords,
  sixteenth arps with a 3/16 ping-pong delay, pulse lead, square bass and a drum
  machine, eight sections of eight bars, ~100 s, looped, all from one seed, using
  the oscillators and the drum hits of `sounds`. `python -m drumhero.keygen
  [--wav f] [--seed N]` renders or plays one on its own. Rendering is 0.3 s
  because every distinct note is cached: four chords is a handful of waveforms.
  Balance measured by band energy after the mix (2026-09-13); the first version
  buried the melody (3 % between 400 and 4000 Hz) because the bass doubled itself
  an octave below its root at 27 Hz.
- `render()` also returns `low` and `high` envelopes, one value per 60th of a
  second, so the animation follows the music by indexing (position = wall clock
  since play started, a Sound on a channel has no cursor). `Scene.low` / `.high`
  are set every frame: the card breathes with the kick, Gray-Scott drops a cell on
  it, the forward pass rides it, the arcs and the particles brighten with the hats.
- `--snap DIR` renders a frame of every scene to PNG (plus the character layer
  as .txt) with SDL_VIDEODRIVER=dummy: that is how the look was checked without
  a terminal, and how to check it after touching a scene.

## Open items

`ROADMAP.md` lists the loose ends with their full context (hi-hat filter eating fast
notes, XR18 USB sends for take audio, XR18 network). Read it before touching the ghost
filter or `capture.py`; update it when an item is closed.

## Hand lead in Exercises

A rudiment whose sticking uses both hands on one instrument (`Chart.lead == "R"`, set by
`_rudiment` / `_rudiment_mix` when `lanes` is None) exists in two versions: right and left
hand lead. There is one level, `Chart.mirrored()` swaps every R and L (notes, strip, the R / L
tokens of the description); never add a "left lead" level by hand. `App.lead` ("R" / "L",
toms or the arrow keys in the list swap it) picks the version played. Progress is keyed by
`Chart.key`: the name, plus `" (L)"` for the left lead; the list shows one star row per hand,
the hub counts both. Old level names map to current keys in `kit.PROGRESS_MIGRATIONS`,
applied when progress loads. Levels with hands on different drums, feet patterns and hi-hat
lessons have no lead (one version).

## Testing

Headless tests use `SDL_VIDEODRIVER=dummy` and `SDL_AUDIODRIVER=dummy`; screens can be
rendered to PNG with `pygame.image.save` for visual checks.

## The hi-hat gesture (Roland TD-17 with VH-10, measured 2026-09-06/07)

This is the ground truth for both repos. hhmapper turns it into One Kit Wonder
articulations for Bitwig; drumhero turns it into game hits. Both must agree.

**What the module sends.**

| gesture | note | notes |
|---|---|---|
| stick on the bow (body), pedal closed | 42 | |
| stick on the bow, pedal open | 46 | |
| stick on the edge, pedal closed | 22 | Roland-specific |
| stick on the edge, pedal open | 26 | Roland-specific |
| pedal chick (foot close) | 44 | velocity from the stomp |
| pedal position | CC4 | 0 = fully open, 90 = fully closed on this pedal, nonlinear: "half by feel" reads 8..28 |

The module picks 42/46 or 22/26 by its own closed/open threshold; the CC value
at the moment of the stroke is the real openness. Zone = edge (22/26) or bow
(42/46); openness = from the last CC4 value, not from the note number.

**Openness classes** (closedness = CC4 value): tight >= 80, open <= 10, mid in
between. hhmapper maps zone x openness to: Tip/Edge Tight (41/42), Tip/Edge
Closed (43/44), Open 2/3 (46/47), Pedal (48) in One Kit Wonder, Kontakt C3 = 60.

**Ghost notes the pedal produces** (nobody hit the hat):

| when | note | velocity | how to catch it |
|---|---|---|---|
| ~30 ms before the chick, pedal still at CC 0 | 46 | 7..22 | velocity floor |
| 3..8 ms after the chick, pedal moving fast | 46 | 48..94 | window after chick |
| up to ~250 ms after the chick, pedal settling | 42 | 22..36 | settle window, soft closed note |
| 42 ms after a hard edge stroke | 42 or 46 | 70..76 % of the stroke | not separable, accepted (see below) |
| 73..93 ms after an edge stroke, one or two of them, nearly every hard stroke | 42 | 35..56 % of the stroke | zone crosstalk |
| ~55 ms after a hard bow stroke, rare | 22 | ~40 % of the stroke | zone crosstalk |
| chick double trigger ~110 ms after a chick | 44 | 16..20 | chick velocity floor |

**Real strokes that look like ghosts** (measured 2026-09-09 on paradiddles, fast bow/edge
alternation, chick + stroke together, open hats; the rules must keep all of these):

| gesture | what arrives |
|---|---|
| softest real tap | 29 (taps in fast doubles 29..48; a missed tap can read 7..18, lost) |
| bow tap right after an edge accent (fast alternation) | 42 at 44..90 ms, 63..85 % of the accent |
| stick landing together with the chick | 44 then, 3..5 ms later, 46 at 113..126 |
| stick landing just after the chick | 44, the 46 ghost at 3..8 ms, then 42 at 11..52 ms, velocity 56..127 |
| stroke while the pedal is still opening (CC moving 20+ in 50 ms) | 46 at 116..127 |

Resting sticks on a pad: 4..14.

**Filter rules, identical in both repos** (hhmapper.py constants, drumhero/ghost.py):

- hi-hat stick note with velocity < 25: drop (both repos; hhmapper no longer holds
  strokes 40 ms for a following chick, that hold cost 40 ms of latency on every stroke)
- hi-hat stick note within 10 ms after a chick (44) with velocity < 100: drop (chick splash)
- closed-hat note (42/22) within 250 ms after a chick with velocity <= 40: drop (pedal settling)
- hi-hat stick note while CC4 moved >= 20 within the last 50 ms, velocity < 50: drop
- hi-hat stick note on the other zone than the previous stroke, within 95 ms at
  <= 58 % of its velocity: drop (zone crosstalk; the reference stays the last real
  stroke, so chained ghosts fall too). The 42 ms / 70..76 % ghost overlaps real
  taps and is let through on purpose.
- chick with velocity <= 20: drop (hhmapper)
- any note with velocity < 8: drop (drumhero)

A change to a threshold goes to both repos and to this section. Validated 2026-09-09 by
replaying two recorded takes (306 real strokes) through both filters: no real stroke
dropped, every ghost above still caught. Record a take with `mido` (timestamps in ms)
and replay it through `State` / `GhostFilter` before touching a number.

**Kit wizard rule (drumhero).** The wizard has one step per zone of the TD-17
(17 zones, `chart.ZONES`): the hi-hat has three, bow, edge and pedal. In the
bow step hearing 42 or 46 assigns both; in the edge step 22 or 26 assigns
both; 44 is only assigned in the pedal step. A number heard in two steps goes
to the later zone. Charts still refer to instruments (kick/snare/hihat/crash,
plus tom1/floor/ride), and an instrument accepts every note of all its zones.
Menu navigation ignores hits under velocity 25; every other hit is one action, immediately,
no debounce (the user wants the game to feel instant); every hit above 15 is heard.
