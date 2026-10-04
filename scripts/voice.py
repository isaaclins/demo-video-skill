"""Voice-over for demo-video: ElevenLabs (with word timestamps), an optional local TTS, or estimated timing (silent).

ElevenLabs: key from the ELEVENLABS_API_KEY environment variable (never printed, never written anywhere).
Every take is cached by a hash of its text + voice + model in <project>/cache/tts/, so a line is only paid for once,
and every paid call is logged in <project>/cache/tts/ledger.jsonl (characters per take). A per-project character
budget (default 4000, DEMO_VIDEO_TTS_BUDGET or --tts-budget) stops runaway spending.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.elevenlabs.io/v1"
# calm, confident stock ("premade") voices, in order of preference
VOICES = [("EXAVITQu4vr4xnSDxMaL", "Sarah"), ("cjVigY5qzO86Huf0OWal", "Eric"), ("SAz9YHcvj6GT2YYXdXww", "River"),
          ("nPczCjzI2devNBz1zQrb", "Brian"), ("Xb7hH8MSUJpSbSDYk0k2", "Alice")]
# best first; the first one the API lists (and that can do TTS) wins
MODELS = ["eleven_v4", "eleven_multilingual_v2", "eleven_turbo_v2_5", "eleven_flash_v2_5", "eleven_turbo_v2"]
EST_CPS = 15.8  # typical ElevenLabs pace; drafts use it so their timing matches the voiced build


def key() -> str | None:
    k = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    return k or None


def _req(method: str, path: str, body: dict | None = None, timeout=180) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"xi-api-key": key() or "", "Content-Type": "application/json",
                                        "User-Agent": "demo-video-skill"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read())


def _err(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        try:
            d = json.loads(e.read())
            det = d.get("detail")
            msg = det.get("message") if isinstance(det, dict) else det
            return f"HTTP {e.code}: {msg or d}"
        except Exception:
            return f"HTTP {e.code}"
    return str(e)


def choose(cache: Path, want_voice: str | None, want_model: str | None, log=print) -> tuple[str, str, str]:
    """(voice_id, voice_name, model_id). Asks the API which models exist; falls back to safe defaults if the key
    may not list models/voices. The choice is cached per project so later builds stay identical."""
    f = cache / "choice.json"
    if f.exists():
        c = json.loads(f.read_text())
        if (not want_voice or c["voice"] == want_voice) and (not want_model or c["model"] == want_model):
            return c["voice"], c.get("voice_name", ""), c["model"]
    model = want_model
    if not model:
        try:
            ms = {m["model_id"] for m in _req("GET", "/models", timeout=30) if m.get("can_do_text_to_speech")}
            model = next((m for m in MODELS if m in ms), None)
        except Exception as e:
            log(f"  (could not list ElevenLabs models: {_err(e)}; using eleven_multilingual_v2)")
        model = model or "eleven_multilingual_v2"
    voice, name = want_voice, ""
    if not voice:
        try:
            have = {v["voice_id"]: v["name"] for v in _req("GET", "/voices", timeout=30).get("voices", [])}
            voice, name = next(((i, n) for i, n in VOICES if i in have), VOICES[0])
        except Exception:
            voice, name = VOICES[0]
    cache.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"voice": voice, "voice_name": name, "model": model}))
    return voice, name, model


def take_id(text: str, voice: str, model: str, settings: dict) -> str:
    return hashlib.sha256(json.dumps([text, voice, model, settings], sort_keys=True).encode()).hexdigest()[:16]


def cached(cache: Path, text, voice, model, settings) -> tuple[Path, Path] | None:
    h = take_id(text, voice, model, settings)
    mp3, aj = cache / f"{h}.mp3", cache / f"{h}.json"
    return (mp3, aj) if mp3.exists() and aj.exists() else None


def spent(cache: Path) -> int:
    led = cache / "ledger.jsonl"
    if not led.exists():
        return 0
    return sum(json.loads(l)["chars"] for l in led.read_text().splitlines() if l.strip())


def synth(cache: Path, text: str, voice: str, model: str, settings: dict) -> tuple[Path, Path]:
    cache.mkdir(parents=True, exist_ok=True)
    h = take_id(text, voice, model, settings)
    mp3, aj = cache / f"{h}.mp3", cache / f"{h}.json"
    body = {"text": text, "model_id": model}
    if settings:
        body["voice_settings"] = settings
    last = None
    for attempt in range(3):
        try:
            d = _req("POST", f"/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128", body)
            break
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 403, 404, 422):
                raise SystemExit(f"ElevenLabs refused the request ({_err(e)}). Check ELEVENLABS_API_KEY, the voice id and "
                                 f"that the key has text-to-speech permission.")
            time.sleep(3 * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    else:
        raise SystemExit(f"ElevenLabs failed: {_err(last)}")
    mp3.write_bytes(base64.b64decode(d["audio_base64"]))
    aj.write_text(json.dumps({"text": text, "voice": voice, "model": model, "alignment": d.get("alignment"),
                              "normalized_alignment": d.get("normalized_alignment")}))
    with (cache / "ledger.jsonl").open("a") as f:
        f.write(json.dumps({"take": h, "chars": len(text), "voice": voice, "model": model, "at": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
    return mp3, aj


def quota() -> tuple[int, int] | None:
    try:
        d = _req("GET", "/user/subscription", timeout=30)
        return d["character_count"], d["character_limit"]
    except Exception:
        return None  # restricted keys may not read the subscription; that is fine


def words_from_alignment(al: dict) -> list[list]:
    chars, st, en = al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]
    words, cur, s0, last = [], "", None, 0.0
    for c, a, b in zip(chars, st, en):
        if c.isspace():
            if cur:
                words.append([cur, round(s0, 3), round(last, 3)])
            cur, s0 = "", None
            continue
        if s0 is None:
            s0 = a
        cur += c
        last = b
    if cur:
        words.append([cur, round(s0, 3), round(last, 3)])
    return words


def estimate_words(text: str, cps: float = EST_CPS, scale_to: float | None = None) -> tuple[list[list], float]:
    words, t = [], 0.0
    for w in text.split():
        d = (len(w) + 1) / cps
        words.append([w, round(t, 3), round(t + d * 0.92, 3)])
        t += d
        if w[-1] in ",;":
            t += 0.08
        elif w[-1] in ".:?!":
            t += 0.2
    end = words[-1][2] if words else 0.0
    if scale_to and end:
        k = scale_to / end
        words = [[w, round(a * k, 3), round(b * k, 3)] for w, a, b in words]
        end = scale_to
    return words, end


def probe_dur(p: Path) -> float:
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                                capture_output=True, text=True).stdout.strip())


def to_wav(src: Path, out: Path, tempo: float = 1.0) -> Path:
    """48 kHz wav, trailing silence trimmed (leading silence kept: the word timestamps count from 0)."""
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        af = "areverse,silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.06,areverse"
        if abs(tempo - 1) > 1e-3:
            af += f",atempo={tempo}"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", af, "-ar", "48000", "-ac", "1", str(out)], check=True)
    return out


# ------------------------------------------------------------------ local TTS (optional, never required)
def local_engine() -> str | None:
    if sys.platform == "darwin" and shutil.which("say"):
        return "say"
    if shutil.which("piper") and os.environ.get("PIPER_MODEL"):
        return "piper"
    return None


def local_synth(cache: Path, text: str) -> Path:
    eng = local_engine()
    cache.mkdir(parents=True, exist_ok=True)
    h = take_id(text, eng or "", os.environ.get("SAY_VOICE", "") + os.environ.get("PIPER_MODEL", ""), {})
    out = cache / f"local_{h}.wav"
    if out.exists():
        return out
    if eng == "say":
        aiff = cache / f"local_{h}.aiff"
        cmd = ["say", "-o", str(aiff)]
        if os.environ.get("SAY_VOICE"):
            cmd += ["-v", os.environ["SAY_VOICE"]]
        subprocess.run(cmd + [text], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-ar", "48000", str(out)], check=True)
        aiff.unlink()
    elif eng == "piper":
        subprocess.run(["piper", "--model", os.environ["PIPER_MODEL"], "--output_file", str(out)], input=text.encode(), check=True)
    else:
        raise SystemExit("no local TTS found (macOS `say`, or `piper` with PIPER_MODEL set)")
    return out
