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
