"""Twitch: the stream and the chat, from inside the game, no OBS.

    T in the game starts and stops the stream. Nothing else in the window changes: the stream
    is the window itself (every overlay included: toasts, the velocity viewer, the chat, the
    camera monitor), not an edition. It never starts on its own.

The stream (Streamer). Two sources, `stream_source` in settings:

"screen" (the default since 2026-09-12): ffmpeg captures display `stream_display` through
avfoundation, and the interface's mix comes from a separate process, `python -m drumhero.twitch
--audio-feed FIFO` (audio_feed): a sounddevice input on the interface (the take's channels and
buffer settings), blocks queued and written to a named pipe as float32 stereo, which ffmpeg
reads as its second input. Both inputs get wall-clock timestamps (-use_wallclock_as_timestamps,
-copyts, setpts/asetpts minus the launch time), so they stay in sync whatever each took to
open; aresample=async ties the audio's sample clock to it. What goes out is the whole display,
the terminal included when it is on that screen. Needs the Screen Recording permission for
drumhero.app (macOS asks once; then relaunch the app).
Why not avfoundation for the audio too ("Capture screen N:X18/XR18", the first version):
measured 2026-09-12 with the interface's native buffer timestamps, ffmpeg's avfoundation input
drops audio buffers even capturing audio alone (6.1: 113 drops, 1.3 s lost in 15 s; 8.0.1: 58
drops, 0.67 s), because it keeps a single pending audio buffer and sleeps 10 ms when it finds
none; viewers heard those as small pops. And why a separate process rather than the game's:
the sounddevice callback needs the GIL and the game's main loop holds it for milliseconds at a
time; the window source's in-process input logged 0.25 s stalls. The feeder reports its own
count of blocks against the wall clock (lost ms) when the stream stops.
Every stream also keeps an AAC copy of what was sent (ffmpeg's tee muxer) in STREAMS_DIR, to
check the audio afterwards.

"window": the game's own frames, like a take:
- Video: the main loop hands the surface to `push` after everything is drawn; a copy is taken
  once per 1/STREAM_FPS slot of the wall clock (the Recorder's scheme: a missed slot repeats
  the previous frame, so the picture stays on the wall clock and never runs ahead of the
  audio) and piped as raw RGB into ffmpeg, which encodes H.264 on VideoToolbox (Twitch does
  not take the takes' HEVC) at a constant bitrate with a keyframe every 2 s and muxes FLV over
  RTMPS to Twitch's ingest.
- Audio: a sounddevice input on the interface, the same two channels the takes use
  (capture_audio_device / capture_audio_channels: the XR18's USB sends 17/18 = Main L/R,
  everything you hear), opened with the mixer-sized buffer (capture.input_stream_kwargs) so
  the game keeps playing. The blocks go to ffmpeg through a named pipe as float32 stereo.
  The audio timeline is put on the stream's wall clock: silence before the first block for
  the time it took the stream to open, and silence for every gap when the device stalls
  (the XR18 has done that, see the ROADMAP), so both inputs start at 0 = the moment T was
  pressed and stay together.
- Health: ffmpeg's `-progress` output is read every second: the frame count, the bitrate
  and the speed (1.0x = keeping up; under it, the encoder or the network is behind and
  Twitch will see dropped frames). When ffmpeg exits (Twitch closed the connection, no
  network) the stream stops and the HUD shows the last line of its stderr.
- The key: ~/.config/drumhero/twitch_key (TWITCH_KEY_PATH), one line, the "Primary Stream
  key" of the Twitch dashboard. Never logged, never in settings.json. The URL is
  STREAM_URL_TWITCH with the key appended; `stream_url` in settings.json replaces the whole
  URL (a local rtmp server for tests: `python -m drumhero.twitch --selftest`). With
  `stream_bandwidth_test` on, `?bandwidthtest=true` is appended: Twitch takes the stream
  without going live and shows it at inspector.twitch.tv.

The chat (Chat): Twitch chat is IRC over TLS; reading it needs no account or token
(nick justinfan + digits). The pane in the game shows the last messages of the channel,
name in the user's Twitch colour, wrapped, and reconnects by itself. Connected while
the stream is live (or `python -m drumhero.twitch --chat` to watch it in the terminal).
"""
import os
import queue
import random
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

from .capture import AUDIO_SR, audio_device_index, ffmpeg_path, input_stream_kwargs

TWITCH_KEY_PATH = os.path.expanduser("~/.config/drumhero/twitch_key")
STREAM_URL_TWITCH = "rtmps://ingest.global-contribute.live-video.net:443/app/"
STREAM_FPS = 30
# Twitch's limits for a non-partner: 6000 kbps, 1080p, keyframes every 2 s, AAC 160 kbps 44.1/48 kHz.
DEFAULTS = {"twitch_channel": "xantwav", "stream_height": 1080, "stream_kbps": 6000, "stream_url": None,
            "stream_bandwidth_test": False, "stream_source": "screen", "stream_display": 0, "stream_video_device": None,
            "capture_audio_device": "X18/XR18", "capture_audio_channels": [17, 18]}
STREAMS_DIR = os.path.expanduser("~/Movies/drumhero/streams")   # the AAC copy of every stream's audio
AUDIO_STALL_S = 1.0          # no audio callback this long: the input is reopened (at most 3 times)
AUDIO_LAG_PAD_S = 0.25       # the audio timeline fell this far behind the wall clock: pad it with silence
CHAT_HOST, CHAT_PORT = "irc.chat.twitch.tv", 6697
CHAT_KEEP = 60               # messages kept for the pane
CHAT_RECONNECT_S = (2, 5, 10, 20, 30)


def read_key(path=TWITCH_KEY_PATH):
    """The stream key, or None (missing file, empty)."""
    try:
        with open(path) as f:
            key = f.read().strip()
    except OSError:
        return None
    return key or None


def stream_url(settings, key):
    if settings.get("stream_url"):
        return settings["stream_url"]
    if not key:
        return None
    url = STREAM_URL_TWITCH + key
    if settings.get("stream_bandwidth_test"):
        url += "?bandwidthtest=true"
    return url


def redact(text, key):
    return text.replace(key, "<key>") if key else text


def encode_args(size, fps, kbps, container=True):
    w, h = size
    return ["-c:v", "h264_videotoolbox", "-realtime", "true", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{2 * kbps}k", "-g", str(2 * fps), "-r", str(fps),
            "-c:a", "aac", "-b:a", "160k", "-ar", str(AUDIO_SR), "-ac", "2"] + (
            ["-f", "flv", "-flvflags", "no_duration_filesize"] if container else [])


def tee_output(url, audio_copy):
    """ffmpeg's tee muxer: the FLV to `url` (a failure there ends the stream) and an AAC copy of
    the audio to `audio_copy` (a failure there is ignored)."""
    esc = lambda p: p.replace("\\", "\\\\").replace("|", "\\|").replace(":", "\\:").replace("[", "\\[").replace("]", "\\]")
    return ["-f", "tee", f"[f=flv:flvflags=no_duration_filesize]{esc(url)}|[select=a:f=adts:onfail=ignore]{esc(audio_copy)}"]


class Streamer:
    """The live stream: `start(size)`, `push(surface)` every frame, `stop()`. `status` for the HUD."""

    def __init__(self, settings, log=print):
        self.settings = {**DEFAULTS, **{k: v for k, v in settings.items() if k in DEFAULTS}}
        self.log = log
        self.active = False
        self.error = None
        self.started_at = None
        self.frames = self.dropped = self.repeated = 0
        self.progress = {}             # ffmpeg's last progress block: frame, fps, bitrate, speed, drop_frames
        self.audio_blocks = self.audio_restarts = self.audio_overflows = self.audio_pads = 0
        self.key = None
        self._q = None

    # --- start ---------------------------------------------------------------------
    def start(self, size):
        """size: (w, h) of the surfaces the game will push. The stream is that size scaled to
        stream_height tall (even sizes, never upscaled)."""
        if self.active:
            return False
        self.error = None
        if not ffmpeg_path():
            self.error = "ffmpeg not found"
            return False
        self.key = read_key()
        url = stream_url(self.settings, self.key)
        if not url:
            self.error = f"no stream key in {TWITCH_KEY_PATH}"
            return False
        self.source = "screen" if self.settings.get("stream_source", "screen") != "window" else "window"
        self.fps = STREAM_FPS
        self.started_at = time.perf_counter()
        self._last_slot = -1
        self.frames = self.dropped = self.repeated = 0
        self.progress = {}
        self.audio_blocks = self.audio_restarts = self.audio_overflows = self.audio_pads = 0
        self.stopped_reason = None
        self.tmp = self.fifo = None
        self.feeder = None
        self._audio = None
        self._q = self._writer = self._awriter = None
        if self.source == "screen":
            return self._start_screen(size, url)
        w, h = int(size[0]), int(size[1])
        th = min(h, int(self.settings["stream_height"]))
        tw = int(w * th / h)
        self.in_size = (w // 2 * 2, h // 2 * 2)
        self.out_size = (tw // 2 * 2, th // 2 * 2)
        self.tmp = tempfile.mkdtemp(prefix="drumhero-stream-")
        self.fifo = os.path.join(self.tmp, "audio.pipe")
        os.mkfifo(self.fifo)
        vf = [] if self.out_size == self.in_size else ["-vf", "scale=%d:%d:flags=bilinear" % self.out_size]
        sr = self._audio_rate()
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-thread_queue_size", "1024", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % self.in_size,
               "-r", str(self.fps), "-i", "pipe:0",
               "-thread_queue_size", "1024", "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", self.fifo,
               "-map", "0:v", "-map", "1:a", *vf, *encode_args(self.out_size, self.fps, int(self.settings["stream_kbps"])),
               "-progress", "pipe:1", url]
        self.ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._q = queue.Queue(maxsize=8)
        self._writer = threading.Thread(target=self._write_frames, daemon=True)
        self._writer.start()
        self._progress_thread = threading.Thread(target=self._read_progress, daemon=True)
        self._progress_thread.start()
        # audio: the fifo is opened by a thread (open blocks until ffmpeg opens its end)
        self._aq = queue.Queue()
        self._audio = None
        self._audio_sr = sr
        self._awriter = threading.Thread(target=self._write_audio, daemon=True)
        self._awriter.start()
        dev = audio_device_index(self.settings["capture_audio_device"])
        if dev is not None:
            try:
                self._start_audio(dev, sr)
            except Exception as e:                        # noqa: BLE001
                self.log(f"stream: no audio ({e}), silence")
                self._audio = None
        else:
            self.log(f"stream: audio device '{self.settings['capture_audio_device']}' not found, silence")
        if self._audio is None:                          # keep the audio timeline alive with silence
            self._silence_thread = threading.Thread(target=self._feed_silence, daemon=True)
            self._silence_thread.start()
        self.active = True
        self.log(f"stream: window {self.out_size[0]}x{self.out_size[1]} at {self.fps} fps, {self.settings['stream_kbps']} kbps, to {redact(url, self.key)}")
        return True

    def _start_screen(self, size, url):
        """ffmpeg: the display through avfoundation + the interface's mix from the audio feeder
        process through a named pipe, both on the wall clock; encode; send (and keep the AAC)."""
        display = int(self.settings.get("stream_display", 0))
        video_dev = self.settings.get("stream_video_device") or f"Capture screen {display}"
        dev = self.settings["capture_audio_device"]
        chans = [int(c) for c in self.settings["capture_audio_channels"]][:2]
        th = int(self.settings["stream_height"])
        self.in_size = self.out_size = (0, th)          # the display's size is ffmpeg's business; only the height is known
        sr = self._audio_rate()
        self.tmp = tempfile.mkdtemp(prefix="drumhero-stream-")
        self.fifo = os.path.join(self.tmp, "audio.pipe")
        os.mkfifo(self.fifo)
        if self.settings.get("stream_url"):               # a test: the copy stays with the pipe
            self.audio_copy = os.path.join(self.tmp, "audio.aac")
        else:
            os.makedirs(STREAMS_DIR, exist_ok=True)
            self.audio_copy = os.path.join(STREAMS_DIR, time.strftime("%Y%m%d-%H%M%S") + " stream.aac")
        # not sys.executable: inside the app bundle that is a Python whose startup .pth launches the game itself
        venv_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "python")
        self.feeder = subprocess.Popen([venv_py if os.path.exists(venv_py) else sys.executable, "-m", "drumhero.twitch", "--audio-feed", self.fifo, "--device", dev,
                                        "--channels", ",".join(str(c) for c in chans), "--rate", str(sr)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        t0 = time.time()
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "avfoundation", "-framerate", str(self.fps), "-capture_cursor", "1", "-use_wallclock_as_timestamps", "1",
               "-thread_queue_size", "1024", "-i", f"{video_dev}:none",
               "-f", "f32le", "-ar", str(sr), "-ac", "2", "-use_wallclock_as_timestamps", "1",
               "-thread_queue_size", "1024", "-i", self.fifo,
               "-copyts", "-map", "0:v", "-map", "1:a",
               "-vf", f"setpts=PTS-{t0:.3f}/TB,scale=-2:'min({th},ih)':flags=bilinear", "-fps_mode", "cfr",
               "-af", f"asetpts=PTS-{t0:.3f}/TB,aresample=async=1",
               *encode_args((0, th), self.fps, int(self.settings["stream_kbps"]), container=False),
               "-progress", "pipe:1", *tee_output(url, self.audio_copy)]
        self.ff = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._progress_thread = threading.Thread(target=self._read_progress, daemon=True)
        self._progress_thread.start()
        self.active = True
        self.log(f"stream: {video_dev} + {dev} channels {chans} (feeder pid {self.feeder.pid}) at {self.fps} fps, up to {th}p, "
                 f"{self.settings['stream_kbps']} kbps, to {redact(url, self.key)}, audio copy {self.audio_copy}")
        return True

    def _audio_rate(self):
        dev = audio_device_index(self.settings["capture_audio_device"])
        if dev is None:
            return AUDIO_SR
        return int(dev[1]["default_samplerate"]) or AUDIO_SR

    # --- audio ---------------------------------------------------------------------
    def _start_audio(self, dev, sr):
        import sounddevice as sd
        idx, info = dev
        chans = [int(c) - 1 for c in self.settings["capture_audio_channels"]]
        nin = int(info["max_input_channels"])
        chans = [c for c in chans if 0 <= c < nin] or [0, min(1, nin - 1)]
        if len(chans) == 1:
            chans = chans * 2
        self._audio_spec = (idx, nin, sr, chans[:2])
        self.audio_samples = 0           # samples sent so far, on the stream's clock (pads included)
        self._audio_last = None
        self._audio_restarting = False
        self._open_audio()
        self.log(f"stream: audio {info['name']} channels {[c + 1 for c in chans[:2]]} at {sr} Hz")

    def _open_audio(self):
        import sounddevice as sd
        idx, nin, sr, chans = self._audio_spec

        def cb(indata, frames, t, status):
            now = time.perf_counter()
            if status.input_overflow:
                self.audio_overflows += 1
            # where this block belongs on the stream's clock: it was captured `frames` ago, plus the input latency
            expected = int((now - self.started_at - frames / sr - float(self._audio.latency or 0)) * sr)
            lag = expected - self.audio_samples
            if lag > AUDIO_LAG_PAD_S * sr or (self.audio_samples == 0 and lag > 0):
                self._aq.put(np.zeros((lag, 2), dtype="float32"))
                self.audio_samples += lag
                self.audio_pads += 1
            self.audio_blocks += 1
            self._audio_last = now
            self.audio_samples += frames
            self._aq.put(np.ascontiguousarray(indata[:, chans], dtype="float32"))

        self._audio = sd.InputStream(device=idx, channels=nin, samplerate=sr, dtype="float32", callback=cb,
                                     **input_stream_kwargs(sr))
        self._audio.start()
        self._audio_started_pc = time.perf_counter()

    def _watch_audio(self):
        """Main thread, every frame: an input that stops delivering is reopened, at most 3 times
        (the callback pads the gap with silence when blocks come back)."""
        if self._audio is None or self._audio_restarting or self.audio_restarts >= 3:
            return
        last = self._audio_last or self._audio_started_pc
        if time.perf_counter() - last < AUDIO_STALL_S:
            return
        self._audio_restarting = True
        self.audio_restarts += 1

        def restart():
            try:
                try:
                    self._audio.abort(); self._audio.close()
                except Exception:                           # noqa: BLE001
                    pass
                self._open_audio()
                self.log(f"stream: audio stream stalled after {self.audio_blocks} blocks, reopened (restart {self.audio_restarts})")
            except Exception as e:                          # noqa: BLE001
                self.log(f"stream: audio reopen failed ({e})")
                self._audio = None
            finally:
                self._audio_restarting = False

        threading.Thread(target=restart, daemon=True).start()

    def _feed_silence(self):
        """No audio device: silence on the wall clock, so ffmpeg still has an audio track."""
        sr = self._audio_sr
        sent = 0
        while self.active or sent == 0:
            due = int((time.perf_counter() - self.started_at) * sr)
            if due > sent:
                self._aq.put(np.zeros((due - sent, 2), dtype="float32"))
                sent = due
            time.sleep(0.05)
            if not self.active:
                break

    def _write_audio(self):
        try:
            fd = open(self.fifo, "wb", buffering=0)
        except OSError:
            return
        with fd:
            while True:
                block = self._aq.get()
                if block is None:
                    break
                try:
                    fd.write(block.tobytes())
                except (BrokenPipeError, ValueError, OSError):
                    break

    # --- frames from the main loop ----------------------------------------------------
    def push(self, surface):
        """Call every frame after everything is drawn; takes a copy once per 1/fps slot."""
        if not self.active:
            return
        if self.ff.poll() is not None:                       # ffmpeg is gone: Twitch closed the connection, or an error
            self._ended()
            return
        if self.source == "screen":
            return
        now = time.perf_counter()
        slot = int((now - self.started_at) * self.fps + 0.5)
        if slot <= self._last_slot:
            return
        if self._audio is not None:
            self._watch_audio()
        self._last_slot = slot
        try:
            self._q.put_nowait((slot, surface.copy()))
        except queue.Full:
            self.dropped += 1

    def _write_frames(self):
        import pygame
        last_rgb = None
        while True:
            item = self._q.get()
            if item is None:
                break
            slot, surf = item
            if surf.get_size() != self.in_size:
                surf = pygame.transform.smoothscale(surf, self.in_size)
            rgb = pygame.image.tobytes(surf, "RGB")
            repeats = slot - self.frames
            if repeats > 0:
                self.repeated += repeats
            for r in [last_rgb or rgb] * max(0, repeats) + [rgb]:
                try:
                    self.ff.stdin.write(r)
                    self.frames += 1
                except (BrokenPipeError, ValueError, OSError):
                    return
            last_rgb = rgb

    def _read_progress(self):
        block = {}
        for line in iter(self.ff.stdout.readline, b""):
            k, _, v = line.decode(errors="replace").strip().partition("=")
            block[k] = v
            if k == "progress":
                self.progress = block
                block = {}

    # --- stop ----------------------------------------------------------------------------
    def _ended(self):
        err = ""
        try:
            err = self.ff.stderr.read().decode(errors="replace").strip().splitlines()
            err = err[-1] if err else ""
        except (OSError, ValueError):
            pass
        self.stopped_reason = redact(err, self.key) or f"ffmpeg exited ({self.ff.returncode})"
        self.log(f"stream: ended, {self.stopped_reason}")
        self.stop()
        self.error = self.stopped_reason

    def stop(self):
        if not self.active:
            return
        self.active = False
        if self.source == "screen":
            if self.ff.poll() is None:
                self.ff.terminate()                   # ffmpeg flushes and closes the rtmp session on SIGTERM
            try:
                self.ff.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.ff.kill()
            feed = ""
            if self.feeder is not None:               # the feeder ends on the broken pipe and reports its tally
                try:
                    os.close(os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK))   # releases a feeder still blocked opening the pipe
                except OSError:
                    pass
                try:
                    _, err = self.feeder.communicate(timeout=5)
                    feed = err.decode(errors="replace").strip().splitlines()[-1:] or [""]
                    feed = feed[0]
                except subprocess.TimeoutExpired:
                    self.feeder.kill()
                    feed = "feeder killed"
            try:
                os.unlink(self.fifo)
                if self.audio_copy.startswith(self.tmp):
                    os.unlink(self.audio_copy)
                os.rmdir(self.tmp)
            except OSError:
                pass
            s = int(time.perf_counter() - self.started_at)
            p = self.progress
            self.log(f"stream: stopped after {s // 60:02d}:{s % 60:02d}, ffmpeg frame {p.get('frame', '?')}, dropped {p.get('drop_frames', '?')}, "
                     f"last speed {p.get('speed', '?')}; {feed}")
            return
        self._q.put(None)
        self._writer.join(timeout=5)
        if self._audio is not None:
            try:
                self._audio.stop(); self._audio.close()
            except Exception:                           # noqa: BLE001
                pass
            self._audio = None
        self._aq.put(None)
        try:                                              # a writer still blocked opening the fifo (ffmpeg never read it) is released
            os.close(os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
        try:
            self.ff.stdin.close()
        except OSError:
            pass
        try:
            self.ff.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.ff.kill()
        self._awriter.join(timeout=2)
        try:
            os.unlink(self.fifo); os.rmdir(self.tmp)
        except OSError:
            pass
        s = int(time.perf_counter() - self.started_at)
        self.log(f"stream: stopped after {s // 60:02d}:{s % 60:02d}, {self.frames} frames ({self.repeated} repeated, {self.dropped} dropped), "
                 f"audio {self.audio_blocks} blocks, {self.audio_pads} pads, {self.audio_restarts} restarts")

    @property
    def status(self):
        """Short text for the HUD: LIVE mm:ss with the bitrate and speed, or the last error."""
        if self.active:
            s = int(time.perf_counter() - self.started_at)
            p = self.progress
            extra = ""
            if p.get("bitrate", "N/A") not in ("N/A", ""):
                extra += f" · {p['bitrate'].replace('kbits/s', 'kbps')}"
            if p.get("speed", "N/A") not in ("N/A", ""):
                extra += f" · {p['speed']}"
            if p.get("drop_frames", "0") not in ("0", ""):
                extra += f" · dropped {p['drop_frames']}"
            return f"LIVE {s // 60:02d}:{s % 60:02d}{extra}"
        return self.error or ""


# ---------------------------------------------------------------------------
def audio_feed(fifo, device, channels, rate):
    """The audio feeder process: the interface's two channels to `fifo` as float32 stereo, from
    the moment ffmpeg opens the pipe (so no backlog: the first block written is live). Blocks
    are queued by the callback and written by the main thread; the pipe closing (ffmpeg gone)
    ends it. The last stderr line is the tally: blocks, overflows, and the audio the device
    delivered against the wall clock (lost ms = buffers PortAudio never gave us)."""
    import sounddevice as sd
    dev = audio_device_index(device)
    if dev is None:
        sys.exit(f"audio feed: device '{device}' not found")
    idx, info = dev
    nin = int(info["max_input_channels"])
    chans = [c - 1 for c in channels if 0 < c <= nin] or [0, min(1, nin - 1)]
    if len(chans) == 1:
        chans = chans * 2
    q = queue.Queue()
    stats = {"blocks": 0, "overflows": 0, "frames": 0}

    def cb(indata, frames, t, status):
        if status.input_overflow:
            stats["overflows"] += 1
        stats["blocks"] += 1
        stats["frames"] += frames
        q.put(np.ascontiguousarray(indata[:, chans], dtype="float32").tobytes())

    out = open(fifo, "wb", buffering=0)                  # blocks until ffmpeg opens its end
    stream = sd.InputStream(device=idx, channels=nin, samplerate=rate, dtype="float32", callback=cb, **input_stream_kwargs(rate))
    stream.start()
    t0 = time.perf_counter()
    try:
        while True:
            block = q.get()
            try:
                out.write(block)
            except (BrokenPipeError, OSError):
                break
    finally:
        elapsed = time.perf_counter() - t0
        stream.stop(); stream.close()
        lost = max(0.0, elapsed - stats["frames"] / rate) * 1000
        print(f"audio feed: {stats['blocks']} blocks, {stats['overflows']} overflows, {elapsed:.1f} s, lost {lost:.0f} ms", file=sys.stderr)


# ---------------------------------------------------------------------------
_TAG = re.compile(r"^@([^ ]*) ")
_PRIVMSG = re.compile(r"^:(\w+)!\S+ PRIVMSG #\S+ :(.*)$")


def parse_line(line):
    """An IRC line -> ("msg", name, text, colour) for a chat message, ("ping", payload) for a PING,
    else None."""
    if line.startswith("PING"):
        return ("ping", line.partition(" ")[2])
    tags = {}
    m = _TAG.match(line)
    if m:
        for kv in m.group(1).split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
        line = line[m.end():]
    m = _PRIVMSG.match(line)
    if not m:
        return None
    name = tags.get("display-name") or m.group(1)
    text = m.group(2)
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]
    colour = None
    c = tags.get("color") or ""
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", c):
        colour = tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
    return ("msg", name, text, colour)


class Chat:
    """The channel's chat, read anonymously. `messages`: the last CHAT_KEEP (t, name, text, colour).
    `connected`, `error`. Reconnects until `stop()`."""

    def __init__(self, channel, log=print, on_message=None):
        self.channel = channel.lstrip("#").lower()
        self.log = log
        self.on_message = on_message
        self.messages = []
        self.connected = False
        self.error = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sock = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        s = self._sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def add(self, name, text, colour=None):
        with self._lock:
            self.messages.append((time.perf_counter(), name, text, colour))
            del self.messages[:-CHAT_KEEP]
        if self.on_message:
            self.on_message(name, text)

    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                self._session()
                attempt = 0
            except (OSError, ssl.SSLError) as e:
                self.error = str(e) or type(e).__name__
                self.connected = False
                if self._stop.is_set():
                    break
                wait = CHAT_RECONNECT_S[min(attempt, len(CHAT_RECONNECT_S) - 1)]
                self.log(f"chat: {self.error}, reconnecting in {wait} s")
                attempt += 1
                self._stop.wait(wait)
        self.connected = False

    def _session(self):
        ctx = ssl.create_default_context()
        raw = socket.create_connection((CHAT_HOST, CHAT_PORT), timeout=10)
        s = ctx.wrap_socket(raw, server_hostname=CHAT_HOST)
        self._sock = s
        nick = f"justinfan{random.randint(10000, 99999)}"
        s.sendall(f"CAP REQ :twitch.tv/tags twitch.tv/commands\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
        s.settimeout(5.0)          # recv, not select: TLS keeps decrypted records in its own buffer, select never sees them
        buf = b""
        last_seen = time.monotonic()
        while not self._stop.is_set():
            try:
                data = s.recv(4096)
            except TimeoutError:
                if time.monotonic() - last_seen > 360:           # Twitch pings every ~5 min; nothing for 6: the link is dead
                    raise OSError("no traffic for 6 minutes")
                continue
            if not data:
                raise OSError("connection closed")
            last_seen = time.monotonic()
            buf += data
            while b"\r\n" in buf:
                line, _, buf = buf.partition(b"\r\n")
                line = line.decode("utf-8", errors="replace")
                if not self.connected and (" 366 " in line or " JOIN " in line):
                    self.connected = True
                    self.error = None
                    self.log(f"chat: joined #{self.channel}")
                p = parse_line(line)
                if p is None:
                    continue
                if p[0] == "ping":
                    s.sendall(f"PONG {p[1]}\r\n".encode())
                else:
                    self.add(p[1], p[2], p[3])


# ---------------------------------------------------------------------------
def selftest(seconds=8, source="window", video_device=None, audio_device=None):
    """The stream pipeline against a local rtmp server (ffmpeg listening), no Twitch: synthetic
    frames (window) or the display (screen), the interface's audio, the received FLV probed
    at the end, with the audio's level so silence is caught."""
    import pygame
    out = os.path.join(tempfile.mkdtemp(prefix="drumhero-selftest-"), "received.flv")
    port = 19350
    server = subprocess.Popen([ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-listen", "1",
                               "-i", f"rtmp://127.0.0.1:{port}/live/test", "-c", "copy", out],
                              stdin=subprocess.DEVNULL)
    time.sleep(1.0)
    pygame.display.init()
    pygame.font.init()
    size = (1280, 720)
    st = Streamer({"stream_url": f"rtmp://127.0.0.1:{port}/live/test", "stream_height": 720, "stream_kbps": 3000, "stream_source": source,
                   "stream_video_device": video_device,
                   **({"capture_audio_device": audio_device, "capture_audio_channels": [1, 2]} if audio_device else {})})
    surf = pygame.Surface(size)
    font = pygame.font.SysFont(None, 80)
    if not st.start(size):
        server.kill()
        sys.exit(f"could not start: {st.error}")
    t0 = time.perf_counter()
    n = 0
    while time.perf_counter() - t0 < seconds and st.active:
        n += 1
        surf.fill((20 + (n % 40), 10, 60))
        surf.blit(font.render(f"drumhero selftest {n}", True, (240, 240, 240)), (60, 300))
        st.push(surf)
        time.sleep(1 / 120)
    print("status:", st.status)
    st.stop()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    from .capture import ffprobe_path
    r = subprocess.run([ffprobe_path(), "-v", "error", "-show_entries", "stream=codec_name,width,height,r_frame_rate,sample_rate,channels:format=duration",
                        "-of", "default=nw=1", out], capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    r = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", out, "-af", "volumedetect", "-vn", "-f", "null", "-"], capture_output=True, text=True)
    print("\n".join(l.split("] ", 1)[-1] for l in r.stderr.splitlines() if "mean_volume" in l or "max_volume" in l))
    print("received:", out, os.path.getsize(out) if os.path.exists(out) else "missing")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chat", metavar="CHANNEL", help="print the channel's chat until Ctrl-C")
    ap.add_argument("--selftest", action="store_true", help="stream synthetic frames to a local rtmp server and probe the result")
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--source", choices=["window", "screen"], default="window")
    ap.add_argument("--video-device", help="screen source selftest: an avfoundation video device instead of the display")
    ap.add_argument("--audio-device", help="selftest: another input device (channels 1-2) instead of the interface")
    ap.add_argument("--audio-feed", metavar="FIFO", help="(internal) the stream's audio feeder process")
    ap.add_argument("--device", default="X18/XR18")
    ap.add_argument("--channels", default="17,18")
    ap.add_argument("--rate", type=int, default=AUDIO_SR)
    args = ap.parse_args(argv)
    if args.audio_feed:
        audio_feed(args.audio_feed, args.device, [int(c) for c in args.channels.split(",")], args.rate)
    elif args.chat:
        c = Chat(args.chat, on_message=lambda name, text: print(f"{name}: {text}"))
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            c.stop()
    elif args.selftest:
        selftest(args.seconds, args.source, args.video_device, args.audio_device)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
