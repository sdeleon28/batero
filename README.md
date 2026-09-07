# drumhero

Guitar Hero style drum trainer driven by MIDI. Sibling of [hhmapper](../hhmapper).

Feed it any MIDI file as the chart. Notes fall down their lanes towards the hit
line; play along on a MIDI drum kit and get instant visual feedback the moment
each hit lands, with the timing error in milliseconds.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```
.venv/bin/python drumhero.py song.mid --port TD-17
.venv/bin/python drumhero.py --demo --port TD-17      # built-in rock beat, no file needed
.venv/bin/python drumhero.py song.mid --out IAC       # also play the chart to a synth
.venv/bin/python drumhero.py song.mid --channel 10    # only chart notes on MIDI channel 10
.venv/bin/python drumhero.py song.mid --log hits.csv  # dump every judged hit on exit
```

Without `--port` you can play with the keyboard: keys `1`..`9`, `0` hit lanes 1..10.

## Lanes

Every distinct note number in the chart gets its own lane, lowest on the left,
labelled with its General MIDI drum name when it has one. Chart notes and input
notes are matched by number.

If your kit sends different numbers than the chart (a TD-17 hi-hat sends 22 and
26 for the edge, the chart may only have 42 and 46), fold them in with
`--alias 22=42,26=46`. Each entry is `input=chart`.

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

## Calibration

Everything between your stick and the screen adds latency: the module's
triggering, USB, the OS, and the display. If your PERFECT hits keep reading
"late", the offset is compensating for that chain. Press `.` to add 5 ms
(hits are treated as earlier) or `,` to subtract, until the mean on the results
screen sits near 0. Then pass it as `--offset 20` next time.

The results screen and `--log` CSV give the mean and standard deviation of your
timing errors; the mean is your latency, the deviation is you.

## Controls

| key        | action                      |
|------------|-----------------------------|
| Esc, Q     | quit                        |
| Space      | pause / resume              |
| R          | restart                     |
| [ / ]      | scroll speed                |
| , / .      | input offset -/+ 5 ms       |
| 1..9, 0    | hit lanes from the keyboard |

## Feedback latency

The render loop runs uncapped (target 240 fps, no vsync) and the MIDI callback
judges the hit on the MIDI thread, so the flash appears on the very next frame
after the note arrives, typically under 5 ms plus whatever the display adds.
