# drumhero

Guitar Hero style drum trainer (pygame + mido). Run with `.venv/bin/python -m drumhero`.
Sibling of `../hhmapper`; same conventions: own `.venv`, commits allowed, reply ends with the user's next action.

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
| 3..5 ms after the chick, pedal moving fast | 46 | 60..78 | window after chick |
| up to ~250 ms after the chick, pedal settling | 42 | 30..36 | pedal motion |
| 42 ms after a hard edge stroke | 42 or 46 | 70..76 % of the stroke | zone crosstalk |
| 73..93 ms after an edge stroke, one or two of them, nearly every hard stroke | 42 | 35..56 % of the stroke | zone crosstalk |
| chick double trigger ~110 ms after a chick | 44 | 16..20 | chick velocity floor |

Softest real stroke measured: velocity 23. Resting sticks on a pad: 4..14.

**Filter rules, identical in both repos** (hhmapper.py constants, drumhero/ghost.py):

- hi-hat stick note with velocity < 25 (hhmapper: <= 15 plus a 40 ms hold that
  cancels the note if a chick follows; drumhero cannot hold because feedback
  must be instant, so it uses the higher floor)
- hi-hat stick note within 60 ms after a chick (44): drop
- hi-hat stick note while CC4 moved >= 20 within the last 50 ms: drop
- hi-hat stick note on the other zone than the previous stroke: within 50 ms at
  <= 85 % of its velocity, or within 100 ms at <= 65 %: drop (zone crosstalk;
  the reference stays the last real stroke, so chained ghosts fall too)
- chick with velocity <= 20: drop (hhmapper)
- any note with velocity < 8: drop (drumhero)

A change to a threshold goes to both repos and to this section.

**Kit wizard rule (drumhero).** The wizard has one step per zone of the TD-17
(17 zones, `chart.ZONES`): the hi-hat has three, bow, edge and pedal. In the
bow step hearing 42 or 46 assigns both; in the edge step 22 or 26 assigns
both; 44 is only assigned in the pedal step. A number heard in two steps goes
to the later zone. Charts still refer to instruments (kick/snare/hihat/crash,
plus tom1/floor/ride), and an instrument accepts every note of all its zones.
Menu navigation ignores hits under velocity 45.
