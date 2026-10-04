"""Synthesized audio for demo-video: click and key sounds, a quiet generated music bed with ducking under the voice,
the mixer and a two-pass loudness normalisation. Everything is generated here (no samples, nothing to license)."""
from __future__ import annotations

import subprocess
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

SR = 48000
rng = np.random.default_rng(7)


def _filt(x, kind, f, order=2):
    return sosfilt(butter(order, f, kind, fs=SR, output="sos"), x)


def click_sound(gain=0.55):
    """crisp trackpad/mouse click: a bright tick + a softer release tick 60 ms later."""
    def tick(amp, f0, dec):
        n = int(0.03 * SR)
        t = np.arange(n) / SR
        nz = _filt(rng.standard_normal(n), "bandpass", [1800, 9000]) * np.exp(-t / dec)
        tone = np.sin(2 * np.pi * f0 * t) * np.exp(-t / (dec * 0.8))
        return amp * (0.8 * nz / (np.abs(nz).max() + 1e-9) + 0.5 * tone)
    a = tick(1.0, 3200, 0.0035)
    b = tick(0.45, 2600, 0.003)
    out = np.zeros(int(0.1 * SR))
    out[: len(a)] += a
    o = int(0.062 * SR)
    out[o: o + len(b)] += b
    return gain * out / np.abs(out).max()


def key_sound(gain=0.075):  # soft: typing must never compete with the voice
    n = int(0.025 * SR)
    t = np.arange(n) / SR
    nz = _filt(rng.standard_normal(n), "bandpass", [900 + rng.uniform(-150, 150), 6000]) * np.exp(-t / 0.004)
    thump = np.sin(2 * np.pi * rng.uniform(170, 230) * t) * np.exp(-t / 0.006) * 0.6
    s = nz / (np.abs(nz).max() + 1e-9) + thump
    return gain * s / np.abs(s).max() * rng.uniform(0.75, 1.0)


def music_bed(dur: float, gain=0.1) -> np.ndarray:
    """warm, slow pad (Fmaj9 - Am7 - Dm9 - Bbmaj7), 4 s per chord, soft plucks on top. Stereo."""
    n = int(dur * SR)
    t = np.arange(n) / SR
    L = np.zeros(n)
    R = np.zeros(n)
    chords = [[53, 57, 60, 64, 67], [57, 60, 64, 67, 72], [50, 57, 60, 64, 65], [46, 53, 57, 62, 65]]
    mt = lambda m: 440 * 2 ** ((m - 69) / 12)
    seg = 4.0
    for ci in range(int(dur / seg) + 2):
        notes = chords[ci % 4]
        a = int(ci * seg * SR) - int(0.6 * SR)
        ln = int((seg + 1.6) * SR)
        lo, hi = max(a, 0), min(a + ln, n)
        if hi <= lo:
            continue
        tt = (np.arange(lo, hi) - a) / SR
        env = np.minimum(1, tt / 1.4) * np.minimum(1, np.maximum(0, (seg + 1.6 - tt) / 1.4))
        for k, m in enumerate(notes):
            f = mt(m)
            for det, pan in ((-0.12, -0.6), (0.12, 0.6)):
                ph = 2 * np.pi * f * (1 + det / 100) * tt
                v = (np.sin(ph) + 0.25 * np.sin(2 * ph) + 0.08 * np.sin(3 * ph)) * env / len(notes)
                L[lo:hi] += v * (0.5 - pan * 0.3)
                R[lo:hi] += v * (0.5 + pan * 0.3)
        # plucks: chord tones an octave up, every 0.5 s, very soft
        for j in range(8):
            p0 = int((ci * seg + j * 0.5) * SR)
            if p0 >= n or p0 < 0:
                continue
            pl = int(0.9 * SR)
            pt = np.arange(min(pl, n - p0)) / SR
            f = mt(notes[(j * 2 + ci) % len(notes)] + 12)
            v = np.sin(2 * np.pi * f * pt) * np.exp(-pt / 0.25) * 0.10
            pan = 0.5 + 0.35 * np.sin(j)
            L[p0:p0 + len(pt)] += v * (1 - pan)
            R[p0:p0 + len(pt)] += v * pan
    L, R = _filt(L, "lowpass", 2400), _filt(R, "lowpass", 2400)
    fade = np.minimum(1, t / 1.5) * np.minimum(1, (dur - t) / 2.0)
    st = np.stack([L * fade, R * fade], 1)
    return gain * st / (np.abs(st).max() + 1e-9)


def load_audio(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", str(SR), "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).astype(np.float64)


def write_wav(path: Path, st: np.ndarray):
    st = np.clip(st, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((st * 32767).astype("<i2").tobytes())


def place(buf: np.ndarray, sig: np.ndarray, at: float, gain=1.0):
    i = int(at * SR)
    if i >= len(buf) or i < 0:
        return
    if sig.ndim == 1:
        sig = np.stack([sig, sig], 1)
    m = min(len(sig), len(buf) - i)
    buf[i:i + m] += sig[:m] * gain


def mix(dur: float, voice: list[tuple[float, Path]], clicks: list[float], keys: list[float], music: float, out: Path):
    """voice: [(start_s, wav)]. music: bed gain (0 = none), ducked under the voice."""
    n = int(dur * SR)
    vo = np.zeros((n, 2))
    for at, p in voice:
        place(vo, load_audio(p), at)
    fx = np.zeros((n, 2))
    for at in clicks:
        place(fx, click_sound(), at)
    for at in keys:
        place(fx, key_sound(), at)
    bus = vo + fx
    if music:
        bed = music_bed(dur, music)
        env = np.abs(vo).max(1)
        k = int(0.25 * SR)
        cs = np.concatenate([[0], np.cumsum(env)])
        idx = np.arange(n)
        lo, hi = np.clip(idx - k // 2, 0, n), np.clip(idx + k // 2, 0, n)
        env = (cs[hi] - cs[lo]) / np.maximum(hi - lo, 1)
        duck = 1 - 0.55 * np.clip(env / (env.max() + 1e-9) * 3, 0, 1) if env.max() > 0 else np.ones(n)
        bus += bed[:n] * duck[:, None]
    write_wav(out, bus)
    return out


def loudnorm(src: Path, out: Path, target: float = -16.0, tp: float = -1.5) -> dict:
    """two-pass EBU R128 normalisation to `target` LUFS (pass 1 measures, pass 2 applies linearly where possible)."""
    import json
    import re
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-af",
                        f"loudnorm=I={target}:TP={tp}:LRA=11:print_format=json", "-f", "null", "-"], capture_output=True, text=True)
    m = json.loads(re.findall(r"\{[^{}]*\}", r.stderr)[-1])
    af = (f"loudnorm=I={target}:TP={tp}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", af, "-ar", str(SR), str(out)], check=True)
    return m
