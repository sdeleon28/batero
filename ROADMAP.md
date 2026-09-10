# drumhero roadmap / open items

Loose ends as of 2026-09-09. Each item carries the context needed to pick it up cold.
Related ground truth lives in `CLAUDE.md` (hi-hat gesture, filter rules) and
`../hhmapper/CLAUDE.md` (GGD output map).

## Next up (in this order, decided 2026-09-09)

### 1. Social (9:16) edition of every take (done 2026-09-09)

**What it became.** Not a second in-game layout: the capture shows exactly what was on
screen, and the phone version is an *edition* rendered from the raws. Decided while
building it (a full portrait re-layout of every screen was prototyped and thrown away:
the user wants the take to show what the screen showed, and both versions of every take
without choosing one).

- A take is a folder, `~/Movies/drumhero/<stamp> <name>/`: `take.json`, `raw/screen.mp4`
  (the window as it was), `raw/camera.mp4`, `raw/audio.wav`, and the editions
  `computer.mp4` (16:9, camera picture-in-picture) and `social.mp4` (1080x1920: the screen
  as a thumbnail across the top, the camera under it `capture_split` of the height, half by
  default, cropped to fill; the pair centred vertically). Both editions render when the
  take stops; the raws stay. `capture.render_edition` / `python -m drumhero.capture
  --render DIR social` render one again; the Setup screen "Takes: editions, Claude edits"
  does the same from the game, and its Claude styles edit an edition (the job folder is
  `edits/<edition>-<style>/` inside the take's folder). The camera check previews both
  editions; S cycles the social camera share.
- Quality (2026-09-09, the user saw compression artifacts and wanted publishable output):
  raws are HEVC by VideoToolbox at 40 Mbps target (`capture.RAW_ENCODE`), the camera at
  1920x1080 (`CAMERA_SIZE`; 720p was being stretched to 960 px in the social edition);
  editions are libx264 crf 16 medium, H.264 High, AAC 256k, lanczos scaling
  (`EDITION_ENCODE`), ~5x real time on the M1 Max in the finish thread. Measured on
  fresh camera frames: h264_videotoolbox tops out at ~2 Mbps whatever it is asked
  (SSIM 0.9984), hevc_videotoolbox at a high target matches libx264 crf 16 (0.9992).
  Budget about 300 MB per minute per raw with a moving drummer; the disk had 11 GB
  free that day. 60 fps takes would be the next step for the scrolling notes.
- Legacy flat takes (`<stamp> take.mp4` + `.json`) still list and can be edited with
  Claude, but have no raws to re-render from.
- Readability of the social thumbnail: notes, judgement words and combo are fine on a
  phone; the HUD's small text (24 px on the 1080p capture) is about 13 px in the reel.
  If that matters later, the fix is a bigger HUD font while recording, not a new layout.

Not done, by choice: the in-game layout switch (a `layout` setting, `P` key, portrait
variants of every screen). If it ever comes back, the prototype's ideas were: draw the
game into a 9:16 frame centred in the window, scale from the frame width (540 design px),
compact hub/list/wizard variants, and a live camera band under the game.

### 2. In-game volume on the curly braces (done 2026-09-09)

`{` lowers and `}` raises the game's own output level on every screen, steps of 5 %,
clamped 0..1, persisted as `volume` in settings (default 1.0), one toast that updates in
place. Implemented as a module-level master factor in `sounds.py` (`set_master` /
`master`) that every `set_volume` multiplies; `App.set_volume` re-applies it to the menu
music and to a running level's tracks (`Track.apply_gain`), a held drum hit keeps its
level. Setup shows a "Volume" row (select raises, wraps to 5 %). The XR18's fader stays
the overall level.


## A. Hi-hat detection drops notes (resolved 2026-09-09)

Confirmed and fixed by recording real playing (paradiddles slow and fast, bow/edge
single strokes accelerating, chick alone, chick + stroke together, open hats) and
replaying the takes through `drumhero/ghost.py` and hhmapper's `State`. The old
rules ate real strokes in three places: bow taps 44..90 ms after an edge accent
(zone crosstalk), every stroke played together with a chick (60 ms chick splash),
and strokes played while the pedal was still opening (pedal motion). New rules and
the measurements behind them are in `CLAUDE.md` (hi-hat gesture section); both
repos carry them. Remaining, by design: a missed tap that reads under 25 is lost,
and the 42 ms / 70..76 % crosstalk ghost after a hard edge accent is let through
because real taps land in the same window.

Same day, the menus: a hit only sounded when it also counted as a button press, and
presses were debounced to one per drum per 220 ms, so a roll on a menu lost its
doubles. Now every hit above velocity 15 is heard and every hit above 25 is one
action, immediately, with no debounce at all (`app.nav_hit`): the user wants the
game to feel instant and MIDI hits never bounce, so a five-stroke roll on a menu
moves five items, on purpose. Do not bring a debounce or a hold back in either repo.

Still to check: gameplay and the hi-hat lessons with the new rules (item D). To
diagnose a dropped stroke, log the TD-17 with mido and ms timestamps while the user
plays a prescribed simple exercise, then replay the log through `GhostFilter` and
hhmapper's `State` with explicit timestamps before changing a number; the recorded
takes' numbers are in `CLAUDE.md`.

## B. Takes have no audio: XR18 does not send the mix to USB 17/18 (resolved 2026-09-09)

**Fixed.** In X AIR Edit the page is Setup -> **In/Out** -> **USB Sends** tab, a matrix
(rows USB 1..18, columns Channel / Aux In / FX / Bus / Effect / Main). USB 17/18 were
tapping Bus 1/2 post fader, which carry nothing; moved to Main L / Main R post fader.
Measured right after: USB 17/18 at -34 dBFS with Bitwig playing (was -180). Rows 1..16
still tap the analog preamps. The user then set the game's `audio_device` to the XR18 and the first
`V` take had 150 ms of game sound and silence after that: opening the take's PortAudio
input silenced SDL's output on the same device. Cause and fix, measured the same day with
a looping tone and the USB 17/18 loopback: PortAudio sets the CoreAudio device buffer to
its own latency target (1024+ frames) and SDL's output, opened with 256 frames, dies within
a second; the old `audio_input_opened` mixer reopen never restored it (and opening the
mixer after such an input killed the input, PaMacCore err -50). Opening every input on
the interface with `blocksize = MIXER_BUFFER` and `latency = MIXER_BUFFER / sr`
(`capture.input_stream_kwargs`, used by the take and the camera-check meter) keeps both
streams alive: a 5 s test take carried the tone at a constant level with 0 overflows and
the mixer kept playing afterwards. The mixer reopen is gone. ffmpeg's avfoundation input
also left SDL alone, kept as a fallback idea. Original notes kept for reference:

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

**Already tried and reverted (2026-09-09).** In X-AIR Edit, Input tab, channels 15/16
(labelled "L OUT_64" / "R OUT_65", the faders that drive Main L/R and monitoring) were
set to Channel Source = USB, USB Return = USB 17/18. That is *computer -> mixer*
(it plays the Mac's USB outputs 17/18 through channels 15/16) and does not send
anything *to* the Mac; the user has restored channels 15/16 to their previous
(analog / A/D) source. Do not go down that road again.

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
have audio. Note: any PortAudio input on the X18 must be opened with
`capture.input_stream_kwargs` (mixer-sized buffer), see the top of this item.

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

- Verify in the app that game audio survives "Camera & take check" and a `V` take with
  the mixer-sized input buffers (item B) and that the camera PiP is in sync
  (commit 22b2331; `capture_camera_delay_ms` in settings can shift it if not).
- iPhone Continuity Camera only appears while the phone is **unlocked/awake** on
  the tripod; it can take ~35 s to show up after unlocking. The camera check
  rescans every 5 s.
- Hi-hat control lessons (`chart.HIHAT_LESSONS`): user has not yet played them
  with hhmapper -> Bitwig; check that the articulation labels feel natural.
- Gameplay with the 2026-09-09 ghost rules: play a fast hi-hat level and count
  `ghost` entries per reason in the runlog (`drumhero.audit --hits`); the rules were
  validated on recorded takes, not yet inside a level.
- Social edition: check a real take's social.mp4 on the phone (thumbnail readability,
  camera crop at split 0.5; S in the camera check changes it).
- hhmapper: run `hhmapper.py --probe --out` against both GGD tracks (One Kit
  Wonder, Modern & Massive 2). Open question: closed hats HH2 = 59 vs preset's
  Closed Tip = 53; switch "mid body/edge" to 53 if closed hats are silent.
- `songs/i-wont-back-down`: audio not ingested yet (needs the user's own recording,
  never download one; see `drumhero/ingest.py`).
- First real runs of "Edit a take with Claude" (`edit.py`) and the Coach
  (`coach.py`) have not happened; both shell out to `claude -p`.
