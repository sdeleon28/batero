# drumhero roadmap / open items

Loose ends as of 2026-09-09. Each item carries the context needed to pick it up cold.
Related ground truth lives in `CLAUDE.md` (hi-hat gesture, filter rules) and
`../hhmapper/CLAUDE.md` (GGD output map).

## A. Hi-hat detection drops notes (setup menu, probably gameplay too)

**Symptom.** In the Setup kit wizard the hi-hat steps do not always register strokes,
and the user suspects the same during gameplay: fast hi-hat notes go missing.

**Suspect: the ghost filter eats real notes.** `drumhero/ghost.py` is applied in
`App.on_midi` (app.py ~line 341-354) to *every* screen, the wizard included, before
any screen sees the note. Rules, with the ones most likely to eat fast real strokes marked:

| rule | constant | risk on fast playing |
|---|---|---|
| hi-hat stick note velocity < 25 | `HIHAT_MIN_VELOCITY` | soft ghost-note rolls at 23..24 |
| stick note within 60 ms after a chick (44) | `CHICK_SPLASH_MS` | **"chick + stroke" figures, hat barks, fast foot/hand alternation** |
| stick note while CC4 moved >= 20 in the last 50 ms | `PEDAL_MOTION_CC/MS` | **any stroke played while opening/closing (open-hat accents, foot splashes)** |
| stroke on the other zone than the previous one: <= 85 % of its velocity within 50 ms, <= 65 % within 100 ms | `ZONE_CROSSTALK` | **bow/edge alternation: a 16th at 150 bpm is 100 ms apart, so a softer edge after a hard bow (or vice versa) is dropped** |
| chained reference: the reference stays the last *accepted* stroke | `GhostFilter` state | after one wrong drop the next stroke is compared against an older, harder note and can fall too |
| any note velocity < 8 | `ANY_MIN_VELOCITY` | none |
| menu navigation ignores velocity < 45 | `NAV_MIN_VELOCITY` (app.py) | wizard/menu only: soft taps do not register there by design |

The rules were tuned on 2026-09-07 by listening to *isolated* hard edge strokes and
chicks (see the ghost table in CLAUDE.md); they were never validated against fast
hi-hat rolls, alternating bow/edge patterns, or the hi-hat control lessons
(`chart.HIHAT_LESSONS`). hhmapper has the same rules (`hhmapper.py`), so a Bitwig
performance would lose the same notes.

**How to diagnose.** Play a fast hi-hat passage in a level, then:

```
.venv/bin/python -m drumhero.audit --hits      # last run log, ~/Library/Logs/drumhero/runs
```

Every dropped note is logged as `ghost` with its `why` (app.py runlog.add("ghost", ...)),
so count `ghost` entries per reason during the passage. The same can be seen live: the
wizard and the play screen show "ignored note N vel V: reason" for 1.5 s
(`PlayScreen.on_ghost`). `~/Library/Logs/drumhero-midi.log` (setting `midi_trace`) has
the raw MIDI with timestamps to compare against.

**Candidate fixes** (change both repos and the CLAUDE.md filter section together):
- Zone crosstalk: shrink the 100 ms tier (ghosts measured at 73..93 ms, so 95 ms
  might do) and/or require the ghost to be the *same* note the crosstalk produces
  (measured: 42 or 46 after an edge stroke, i.e. bow ghosts after edge, never
  edge ghosts after bow). Compare bow-after-edge only.
- Chick splash: the measured ghost is a 46 at 60..78 within 3..5 ms; a 60 ms window
  is generous. 15..20 ms would keep real "chick then stroke" figures.
- Pedal motion: only drop when the note is *soft* (measured ghosts 30..36), keep
  strokes >= ~50 while the pedal moves.
- Wizard: bypass the ghost filter entirely in the kit wizard steps (a wizard only
  needs to see note numbers; crosstalk ghosts are harmless there since bow/edge
  steps assign both notes anyway).

## B. Takes have no audio: XR18 does not send the mix to USB 17/18

**Goal.** A take (`V` key, `capture.py`) must contain what the user hears: the game's
sounds plus Bitwig/GGD played through hhmapper, i.e. the audio interface's monitor
output. The recorder captures it with sounddevice from device `X18/XR18`, channels
`capture_audio_channels = [17, 18]` (1-based; indices 16/17), which on the XR18
are the last USB send pair, normally carrying Main L/R.

**Measured.** Twice, and again during a 15-minute passive watch, USB inputs 17/18
were digital silence (-180 dBFS) while music was playing, so the mixer is not
routing anything to those USB sends. Inputs 1..16 carry the analog preamps.
Everything else in the pipeline works: 1080p video, camera PiP in sync, wav
muxing (an empty wav is muxed, so the files exist but are silent).

**What the user changed so far (wrong direction).** In X-AIR Edit, Input tab, on
channels 15 and 16 (labelled "L OUT_64" / "R OUT_65", the faders that drive the
Main L/R and monitoring): Channel Source = USB, USB Return = USB 17 / USB 18,
USB Trim -4 dB. That is *computer -> mixer* (the mixer now plays back whatever the
Mac sends to its USB outputs 17/18 through channels 15/16); it does not make the
mixer send anything *to* the Mac. **Undo it**: set channels 15/16 back to their
previous source (analog / A/D) or they will feed silence, or feedback if the game
ever outputs to the XR18's 17/18.

**What has to happen on the XR18 (firmware 1.17).** The mixer -> computer routing
is "USB Sends" (a.k.a. USB Out / card outputs): 18 selectors, one per USB send,
each choosing which bus goes to the computer on that channel. Set:

- USB Send 17 = Main L (or the LR mix / monitor bus the user actually listens to)
- USB Send 18 = Main R

In X-AIR Edit it is under Setup (gear) -> Audio/Midi -> "USB Sends" section
(on some versions the block is inside Setup -> "Audio Interface" / the routing
page reachable from the USB icon). In X AIR (iPad/Android) it is Setup -> Audio ->
USB Sends. If the software only offers a routing page with "USB Out" rows, the
same 18 rows are there. The user could not find this section while connected via
the mixer's own Wi-Fi AP; try connecting the Mac to the mixer over Ethernet on the
same LAN instead (see C), or use the iPad app.

**Also check** `~/.config/drumhero/settings.json`: `audio_device` is
"JBL Tune 780NC", so the *game's* sounds go to the headphones, not into the XR18,
and would not be in the take even with USB sends fixed. Either set `audio_device`
to the XR18 (the game's Audio device setting in Setup) and monitor from the mixer,
or accept takes that only contain Bitwig.

**Verify** after the change, from the drumhero venv:

```
.venv/bin/python - <<'PY'
import sounddevice as sd, numpy as np
dev = [i for i, d in enumerate(sd.query_devices()) if "X18" in d["name"]][0]
rec = sd.rec(int(3 * 48000), samplerate=48000, channels=18, device=dev, dtype="float32"); sd.wait()
for ch in (16, 17):
    x = rec[:, ch]; print(ch + 1, "dBFS", 20 * np.log10(max(np.abs(x).max(), 1e-9)))
PY
```

with music playing in Bitwig; anything above -60 dBFS means the sends work. Then
Setup -> "Camera & take check" shows the same meter live, and a `V` take should
have audio. Note: opening a PortAudio input on the X18 breaks SDL's output on the
same device; `App.audio_input_opened` reopens the mixer afterwards (commit 5a06fde),
keep that call whenever a new input path is added.

## C. XR18 network / OSC (optional, would let Claude configure the mixer)

OSC is UDP port 10024 (`/xinfo` gets a reply). Findings:
- In AP mode the mixer answers at 192.168.1.1 as `XR18-7D-D8-75`, fw 1.17, but the
  Mac must join the mixer's Wi-Fi and loses internet (no LLM backend), so no.
- The switch has three positions: AP, Wi-Fi client, Ethernet. In Ethernet mode the
  mixer took DHCP 192.168.15.2 / 255.255.0.0 from whatever the cable is plugged into,
  which is not the Mac's LAN (SaltaPirata router, 192.168.1.x), so it was never
  reachable from the Mac. Fix: plug the mixer's Ethernet into a LAN port of the
  192.168.1.1 router (or the same switch as the Mac), power-cycle, then
  `/xinfo` on 192.168.1.x finds it (a broadcast to 192.168.1.255:10024 works).
- tcpdump needs sudo; run it from another pane with `| tee /tmp/xr18.pcap.txt`.

With OSC reachable, USB sends are `/config/usbsends` style addresses on the
XR18 (undocumented; check the X32/XR18 OSC docs, "Unofficial X32/M32 OSC
Remote Protocol", the XR18 mirrors X32 addresses for routing: `/config/routing/CARD/1-8` etc.).

## D. Smaller pending items

- Verify game audio survives "Camera & take check" and takes (commit 5a06fde) and
  that the camera PiP is in sync (commit 22b2331; `capture_camera_delay_ms` in
  settings can shift it if not).
- iPhone Continuity Camera only appears while the phone is **unlocked/awake** on
  the tripod; it can take ~35 s to show up after unlocking. The camera check
  rescans every 5 s.
- Hi-hat control lessons (`chart.HIHAT_LESSONS`): user has not yet played them
  with hhmapper -> Bitwig; check that the articulation labels feel natural.
- hhmapper: run `hhmapper.py --probe --out` against both GGD tracks (One Kit
  Wonder, Modern & Massive 2). Open question: closed hats HH2 = 59 vs preset's
  Closed Tip = 53; switch "mid body/edge" to 53 if closed hats are silent.
- `songs/i-wont-back-down`: audio not ingested yet (needs the user's own recording,
  never download one; see `drumhero/ingest.py`).
- First real runs of "Edit a take with Claude" (`edit.py`) and the Coach
  (`coach.py`) have not happened; both shell out to `claude -p`.
