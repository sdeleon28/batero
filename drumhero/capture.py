"""Recording: the game's own frames, the sound card's output and an optional camera
(the iPhone) into one edited file with the camera picture-in-picture.

    V in the game starts and stops a take. Files land in ~/Movies/drumhero/.

How it works, and why not a screen recorder:
- Video: the game hands a copy of its surface to a writer thread 30 times a second; the
  thread converts it and pipes raw frames into ffmpeg (h264 by VideoToolbox). No screen
  recording permission, no display to pick, and the main loop only pays for one blit.
- Audio: a sounddevice input stream on the interface (default "X18/XR18"), two of its
  input channels (default 17-18: set the XR18's USB sends 17/18 to Main L/R in X-AIR
  Edit and they carry exactly the mix you hear, the game and Bitwig included). Written
  to a WAV by a thread. `python -m drumhero.capture --check` shows which channels have
  signal.
- Camera (optional): ffmpeg's avfoundation records the first video device whose name
  matches the camera setting ("iPhone" by default, so Continuity Camera or Camo) to its
  own file, with wall-clock timestamps.
- Stop: a compose pass muxes video and audio and overlays the camera in the corner,
  aligned by wall clock. The result is the only file left.
"""
import glob
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

OUT_DIR = os.path.expanduser("~/Movies/drumhero")
FPS = 30
DEFAULTS = {"capture_audio_device": "X18/XR18", "capture_audio_channels": [17, 18],
            "capture_camera": "iPhone", "capture_pip": 0.28, "capture_corner": "br"}
CORNERS = ["br", "bl", "tr", "tl"]
PREVIEW_SIZE = (640, 360)
CAMERA_FPS = 30              # what cameras accept (Continuity Camera: 30 or 60)


def overlay_xy(corner, margin=24):
    """ffmpeg overlay expression for a corner."""
    x = f"W-w-{margin}" if corner in ("br", "tr") else f"{margin}"
    y = f"H-h-{margin}" if corner in ("br", "bl") else f"{margin}"
    return f"{x}:{y}"
AUDIO_SR = 44100


def ffmpeg_path():
    return shutil.which("ffmpeg")


def list_video_devices():
    """[(index, name)] of avfoundation video devices, or [] without ffmpeg."""
    ff = ffmpeg_path()
    if not ff:
        return []
    try:
        r = subprocess.run([ff, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out, video = [], False
    for line in r.stderr.splitlines():
        if "AVFoundation video devices" in line:
            video = True
        elif "AVFoundation audio devices" in line:
            video = False
        elif video:
            m = re.search(r"\[(\d+)\] (.+)$", line)
            if m:
                out.append((int(m.group(1)), m.group(2).strip()))
    return out


def find_camera(substring):
    if not substring:
        return None
    for idx, name in list_video_devices():
        if substring.lower() in name.lower() or ("camo" in name.lower() and "iphone" in substring.lower()):
            return idx, name
    return None


def audio_device_index(substring):
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and substring.lower() in d["name"].lower():
            return i, d
    return None


class Recorder:
    """One take at a time. push(surface) from the main loop; start/stop from anywhere."""

    def __init__(self, settings, log=print):
        self.settings = {**DEFAULTS, **{k: v for k, v in settings.items() if k in DEFAULTS}}
        self.log = log
        self.active = False
        self.composing = None          # (thread, final path) while the compose pass runs
        self.error = None
        self.started_at = None
        self._next_frame = 0.0
        self._q = None

    # --- start ---------------------------------------------------------------------
    def start(self, size, name="take"):
        if self.active or not ffmpeg_path():
            self.error = None if self.active else "ffmpeg not found"
            return False
        self.error = None
        self.size = size
        self.tmp = tempfile.mkdtemp(prefix="drumhero-take-")
        self.name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip() or "take"
        self.t0 = time.time()
        self.started_at = time.perf_counter()
        # video: raw frames piped to ffmpeg
        w, h = size
        self.video_path = os.path.join(self.tmp, "screen.mp4")
        self.ff_video = subprocess.Popen(
            [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(FPS), "-i", "pipe:0",
             "-c:v", "h264_videotoolbox", "-b:v", "14M", "-pix_fmt", "yuv420p", self.video_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self._q = queue.Queue(maxsize=8)
        self.frames = 0
        self.dropped = 0
        self._writer = threading.Thread(target=self._write_frames, daemon=True)
        self._writer.start()
        # audio: sounddevice input -> wav
        self.audio_path = None
        self._audio = None
        dev = audio_device_index(self.settings["capture_audio_device"])
        if dev is not None:
            try:
                self._start_audio(dev)
            except Exception as e:                        # noqa: BLE001
                self.log(f"recording: no audio ({e})")
                self._audio = None
        else:
            self.log(f"recording: audio device '{self.settings['capture_audio_device']}' not found, video only")
        # camera: ffmpeg avfoundation to its own file
        self.cam_path = None
        self.ff_cam = None
        cam = find_camera(self.settings["capture_camera"])
        if cam is not None:
            self.cam_path = os.path.join(self.tmp, "camera.mp4")
            self.cam_t0 = time.time()
            self.ff_cam = subprocess.Popen(
                [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "avfoundation", "-framerate", str(CAMERA_FPS), "-pixel_format", "uyvy422", "-video_size", "1280x720",
                 "-use_wallclock_as_timestamps", "1", "-i", f"{cam[0]}:none",
                 "-c:v", "h264_videotoolbox", "-b:v", "8M", "-pix_fmt", "yuv420p", self.cam_path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            self.log(f"recording: camera {cam[1]}")
        self.active = True
        return True

    def _start_audio(self, dev):
        import sounddevice as sd
        import soundfile as sf
        idx, info = dev
        chans = [int(c) - 1 for c in self.settings["capture_audio_channels"]]
        nin = int(info["max_input_channels"])
        chans = [c for c in chans if 0 <= c < nin] or [0, min(1, nin - 1)]
        sr = int(info["default_samplerate"]) or AUDIO_SR
        self.audio_path = os.path.join(self.tmp, "audio.wav")
        self._aq = queue.Queue()
        self._afile = sf.SoundFile(self.audio_path, "w", samplerate=sr, channels=len(chans), subtype="PCM_16")

        def cb(indata, frames, t, status):
            self._aq.put(indata[:, chans].copy())

        self._audio = sd.InputStream(device=idx, channels=nin, samplerate=sr, blocksize=1024, dtype="float32", callback=cb)
        self._audio.start()
        self.audio_t0 = time.time()
        self._awriter = threading.Thread(target=self._write_audio, daemon=True)
        self._awriter.start()
        self.log(f"recording: audio {info['name']} channels {[c + 1 for c in chans]} at {sr} Hz")

    # --- frames from the main loop ----------------------------------------------------
    def push(self, surface):
        """Call every frame; takes a copy FPS times a second. Cheap when it is not time yet."""
        if not self.active:
            return
        now = time.perf_counter()
        if now < self._next_frame:
            return
        self._next_frame = max(self._next_frame + 1 / FPS, now - 0.5 / FPS)
        try:
            self._q.put_nowait(surface.copy())
        except queue.Full:
            self.dropped += 1

    def _write_frames(self):
        import pygame
        while True:
            surf = self._q.get()
            if surf is None:
                break
            if surf.get_size() != self.size:
                surf = pygame.transform.smoothscale(surf, self.size)
            try:
                self.ff_video.stdin.write(pygame.image.tobytes(surf, "RGB"))
                self.frames += 1
            except (BrokenPipeError, ValueError, OSError):
                break

    def _write_audio(self):
        while True:
            block = self._aq.get()
            if block is None:
                break
            self._afile.write(block)

    # --- stop and compose ---------------------------------------------------------------
    def stop(self):
        if not self.active:
            return None
        self.active = False
        duration = time.perf_counter() - self.started_at
        self._q.put(None)
        self._writer.join(timeout=10)
        try:
            self.ff_video.stdin.close()
        except OSError:
            pass
        if self._audio is not None:
            self._audio.stop(); self._audio.close()
            self._aq.put(None); self._awriter.join(timeout=10); self._afile.close()
        if self.ff_cam is not None:
            try:
                self.ff_cam.stdin.write(b"q"); self.ff_cam.stdin.flush()
            except OSError:
                pass
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.t0))
        os.makedirs(OUT_DIR, exist_ok=True)
        final = os.path.join(OUT_DIR, f"{stamp} {self.name}.mp4")
        t = threading.Thread(target=self._compose, args=(final, duration), daemon=True)
        self.composing = (t, final)
        t.start()
        return final

    def _compose(self, final, duration):
        err = self.ff_video.wait(timeout=60)
        if self.ff_cam is not None:
            try:
                self.ff_cam.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.ff_cam.kill()
        cam_ok = self.cam_path and os.path.exists(self.cam_path) and os.path.getsize(self.cam_path) > 1000
        cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y", "-i", self.video_path]
        maps = []
        if cam_ok:
            offset = self._camera_offset()
            cmd += ["-itsoffset", f"{offset:.3f}", "-i", self.cam_path]
        if self.audio_path and os.path.exists(self.audio_path):
            cmd += ["-itsoffset", f"{self.audio_t0 - self.t0:.3f}", "-i", self.audio_path]
        if cam_ok:
            pip = float(self.settings["capture_pip"])
            cmd += ["-filter_complex", f"[1:v]scale=-2:{int(pip * self.size[1])}[pip];"
                                       f"[0:v][pip]overlay={overlay_xy(self.settings.get('capture_corner', 'br'))}:eof_action=pass[v]", "-map", "[v]"]
            if self.audio_path:
                cmd += ["-map", "2:a"]
            cmd += ["-c:v", "h264_videotoolbox", "-b:v", "14M", "-pix_fmt", "yuv420p"]
        else:
            cmd += ["-map", "0:v", "-c:v", "copy"]
            if self.audio_path:
                cmd += ["-map", "1:a"]
        cmd += ["-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.3f}", "-movflags", "+faststart", final]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            self.error = f"compose failed: {r.stderr.strip()[-300:]}"
            self.log(self.error)
            keep = os.path.join(OUT_DIR, f"{os.path.basename(final)[:-4]} (parts)")
            shutil.copytree(self.tmp, keep, dirs_exist_ok=True)
        else:
            self.log(f"recording saved: {final} ({self.frames} frames, {self.dropped} dropped)")
            self._write_sidecar(final, duration)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.composing = None

    def _write_sidecar(self, final, duration):
        """<take>.json: when it started, how long, and the run logs of levels played meanwhile."""
        from .runlog import RUNS_DIR
        logs = []
        for path in sorted(glob.glob(os.path.join(RUNS_DIR, "*.jsonl"))):
            try:
                with open(path) as f:
                    head = json.loads(f.readline())
                if head.get("ended", 0) >= self.t0 and head.get("started", 0) <= self.t0 + duration:
                    logs.append({"path": path, "chart": head["chart"]["name"], "started": head["started"],
                                 "ended": head.get("ended"), "stats": head.get("stats")})
            except (OSError, ValueError, KeyError):
                continue
        meta = {"take": final, "t0": self.t0, "duration": duration, "fps": FPS, "size": list(self.size),
                "frames": self.frames, "dropped": self.dropped, "camera": bool(self.cam_path), "run_logs": logs}
        with open(final[:-4] + ".json", "w") as f:
            json.dump(meta, f, indent=1)

    def _camera_offset(self):
        """Seconds the camera file starts after the video, from its wall-clock timestamps."""
        try:
            r = subprocess.run([shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries", "format=start_time",
                                "-of", "csv=p=0", self.cam_path], capture_output=True, text=True, timeout=15)
            start = float(r.stdout.strip())
            if start > 1e9:                                   # epoch seconds: aligned by wall clock
                return start - self.t0
        except (ValueError, OSError, subprocess.TimeoutExpired):
            pass
        return self.cam_t0 - self.t0 + 0.4                    # ffmpeg's usual startup lag

    @property
    def status(self):
        """Short text for the HUD: recording time, or composing, or the last error."""
        if self.active:
            s = int(time.perf_counter() - self.started_at)
            return f"REC {s // 60:02d}:{s % 60:02d}"
        if self.composing is not None:
            return "rendering take..."
        return self.error


class CameraPreview:
    """Live frames from the camera through ffmpeg (rawvideo on a pipe), latest frame kept."""

    def __init__(self, camera, size=PREVIEW_SIZE, fps=15):
        self.size = size
        self.frame = None            # bytes of the latest rgb24 frame
        self.error = None
        self.frames = 0
        w, h = size
        try:
            # cameras advertise 30/60 fps modes (the iPhone refuses 15): capture at 30, keep every other frame
            self.proc = subprocess.Popen(
                [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-framerate", str(CAMERA_FPS),
                 "-pixel_format", "uyvy422", "-video_size", "1280x720", "-i", f"{camera[0]}:none",
                 "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except OSError as e:
            self.proc = None
            self.error = str(e)
            return
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        n = self.size[0] * self.size[1] * 3
        while True:
            buf = self.proc.stdout.read(n)
            if len(buf) < n:
                break
            self.frame = buf
            self.frames += 1
        err = self.proc.stderr.read().decode(errors="replace")
        if not self.frames:
            lines = [l for l in err.splitlines() if l.strip() and "deprecated" not in l and "NSKVO" not in l and "Please use" not in l]
            self.error = (lines[-1][-160:] if lines else "the camera sent no frames")

    def surface(self):
        if self.frame is None:
            return None
        import pygame
        return pygame.image.frombuffer(self.frame, self.size, "RGB")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()


class AudioMeter:
    """RMS level per selected channel of the capture device, updated by the input stream."""

    def __init__(self, settings):
        import sounddevice as sd
        self.settings = {**DEFAULTS, **{k: v for k, v in settings.items() if k in DEFAULTS}}
        self.levels = None           # dB per selected channel
        self.error = None
        dev = audio_device_index(self.settings["capture_audio_device"])
        if dev is None:
            self.error = f"audio device '{self.settings['capture_audio_device']}' not found"
            self.stream = None
            return
        idx, info = dev
        nin = int(info["max_input_channels"])
        self.chans = [c - 1 for c in self.settings["capture_audio_channels"] if 0 <= c - 1 < nin]
        self.stream = sd.InputStream(device=idx, channels=nin, samplerate=int(info["default_samplerate"]), blocksize=2048,
                                     dtype="float32", callback=self._cb)
        self.stream.start()

    def _cb(self, indata, frames, t, status):
        rms = np.sqrt(np.mean(indata[:, self.chans] ** 2, axis=0)) if self.chans else np.zeros(0)
        self.levels = [float(20 * np.log10(v + 1e-9)) for v in rms]

    def stop(self):
        if self.stream is not None:
            self.stream.stop(); self.stream.close()


def check(settings=None, seconds=2.0):
    """Record a moment from the audio device and report each channel's level, so the
    routing (XR18 USB sends) can be verified. Also lists cameras."""
    import sounddevice as sd
    settings = {**DEFAULTS, **(settings or {})}
    dev = audio_device_index(settings["capture_audio_device"])
    if dev is None:
        print(f"audio device '{settings['capture_audio_device']}' not found. Inputs:")
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"  {d['name']} ({d['max_input_channels']} in)")
    else:
        idx, info = dev
        n = int(info["max_input_channels"]); sr = int(info["default_samplerate"])
        print(f"{info['name']}: {n} inputs at {sr} Hz; recording {seconds:.0f} s, play something...")
        rec = sd.rec(int(seconds * sr), samplerate=sr, channels=n, device=idx, dtype="float32"); sd.wait()
        rms = np.sqrt(np.mean(rec ** 2, axis=0))
        want = settings["capture_audio_channels"]
        for c in range(n):
            db = 20 * np.log10(rms[c] + 1e-9)
            mark = " <- selected" if c + 1 in want else ""
            bar = "#" * int(max(0, (db + 60) / 2))
            print(f"  in {c + 1:2d}: {db:6.1f} dBFS {bar}{mark}")
        print("Signal on the selected channels means the mix is coming back over USB. If they are silent,"
              " set USB sends 17/18 to Main L/R in X-AIR Edit, or pick other channels in settings.")
    cams = list_video_devices()
    print("cameras:", ", ".join(f"[{i}] {nm}" for i, nm in cams) or "none (ffmpeg missing or no devices)")
    cam = find_camera(settings["capture_camera"])
    print(f"camera for the take: {cam[1] if cam else 'none found matching ' + repr(settings['capture_camera'])}")


if __name__ == "__main__":
    check()
