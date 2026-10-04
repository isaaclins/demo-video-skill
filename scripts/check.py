"""Checks for a finished demo video. Hard failures make `dv build` / `dv check` exit non-zero.

- format: <= 60 s, H.264 yuv420p 1920x1080 30 fps, AAC 48 kHz stereo (hard)
- loudness: integrated loudness about -16 LUFS with a voice (-20 without) (hard if off by > 2 LU)
- caption/voice sync: speech onset of every scene vs its first caption (hard if > 0.35 s apart)
- voice at cuts: every voice take ends inside its own scene and ends in silence (hard)
- picture: scenes that never change or look blank (warning: look at the contact sheet)
- recorder warnings (e.g. an "at" word that came too late)
- speech-to-text (only with --stt and faster-whisper installed): transcript vs script
Writes out/<name>_sheet.png (12 frames), out/<name>_scenes.png (one labelled frame per scene), out/<name>_check.txt.
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Inter-SemiBold.ttf"


def probe(p: Path) -> dict:
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                      "format=duration,size:stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,sample_rate,channels",
                                      "-of", "json", str(p)], capture_output=True, text=True).stdout)


def pcm(p: Path, sr=16000, start=None, dur=None) -> np.ndarray:
    cmd = ["ffmpeg", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(p)]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    raw = subprocess.run(cmd + ["-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, np.float32)


def frame(p: Path, t: float, w=640) -> Image.Image:
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(p), "-frames:v", "1", "-vf", f"scale={w}:-2",
                          "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
    import io
    return Image.open(io.BytesIO(raw)).convert("RGB")


def loudness(p: Path) -> float | None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(p), "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else None


def run(video: Path, timeline: Path | None, stt: bool = False) -> int:
    video = Path(video)
    lines, bad = [], 0

    def say(ok: bool | None, msg: str):
        nonlocal bad
        tag = "OK  " if ok else ("WARN" if ok is None else "FAIL")
        bad += ok is False
        lines.append(f"{tag} {msg}")
        print(f"  {tag} {msg}", flush=True)

    print(f"checks for {video.name}:")
    j = probe(video)
    d = float(j["format"]["duration"])
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    au = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    say(d <= 60.0, f"duration {d:.2f}s (limit 60 s), {int(j['format']['size']) / 1e6:.1f} MB")
    say(v.get("codec_name") == "h264" and v.get("pix_fmt") == "yuv420p" and (v.get("width"), v.get("height")) == (1920, 1080)
        and v.get("r_frame_rate") == "30/1", f"video {v.get('codec_name')} {v.get('width')}x{v.get('height')} {v.get('pix_fmt')} {v.get('r_frame_rate')}")
    say(bool(au) and au["codec_name"] == "aac" and au["sample_rate"] == "48000",
        f"audio {au['codec_name'] + ' ' + au['sample_rate'] + ' Hz ' + str(au.get('channels')) + ' ch' if au else 'missing'}")
    tl = json.loads(Path(timeline).read_text()) if timeline and Path(timeline).exists() else None
    voiced = bool(tl and any(s.get("audio") for s in tl["scenes"]))
    lu = loudness(video)
    target = -16.0 if voiced or not tl else -20.0
    if lu is not None:
        say(abs(lu - target) <= 2.0, f"loudness {lu:.1f} LUFS (target {target:.0f})")

    if tl:
        caps = tl["captions"]
        if voiced:
            a = pcm(video)
            hop = 160
            rms = np.sqrt(np.convolve(a ** 2, np.ones(400) / 400, "same")[::hop])
            db = 20 * np.log10(rms + 1e-6)
            worst = 0.0
            for s in tl["scenes"]:
                c = [x for x in caps if x["scene"] == s["id"]]
                if not c or not s.get("audio"):
                    continue
                t0 = c[0]["t0"] + 0.05
                lo, hi = int(s["start"] * 100), int((s["start"] + s["dur"]) * 100)
                seg = db[lo:hi]
                if not len(seg):
                    continue
                base = np.percentile(seg, 10)
                on = next((i / 100 for i in range(lo, hi) if i < len(db) and db[i] > base + 18), None)
                if on is not None:
                    worst = max(worst, abs(on - t0))
            say(worst <= 0.35, f"captions vs voice: worst scene offset {worst:.2f}s (limit 0.35 s)")
            # voice takes end inside their scene and in silence
            probs = []
            for s in tl["scenes"]:
                if not s.get("audio"):
                    continue
                x = pcm(Path(s["audio"]), 48000)
                end = s["start"] + s["pre"] + len(x) / 48000
                pk = np.abs(x).max() + 1e-9
                tail = 20 * np.log10(np.sqrt(np.mean(x[-2400:] ** 2)) / pk + 1e-9)
                if end > s["start"] + s["dur"] - 0.05:
                    probs.append(f"{s['id']}: voice runs {end - s['start'] - s['dur']:.2f}s past its cut")
                if tail > -40:
                    probs.append(f"{s['id']}: voice take ends abruptly ({tail:.0f} dB in the last 50 ms)")
            say(not probs, "voice at cuts: " + ("every line ends in a pause before its cut" if not probs else "; ".join(probs)))
        for r in tl.get("record", []):
            for w in r.get("warnings", []):
                say(None, f"recorder {r['id']}: {w}")

        # picture: frozen or blank scenes
        for s in tl["scenes"]:
            ts = [s["start"] + s["dur"] * k for k in (0.15, 0.5, 0.85)]
            fr = [np.asarray(frame(video, t, 320), np.float32) for t in ts]
            win = [f[10:-25, 30:-30] for f in fr]
            if max(np.abs(win[0] - w).mean() for w in win[1:]) < 0.3 and not s["id"].startswith(("end", "outro", "card")):
                say(None, f"scene {s['id']}: the picture does not change (static page? check the sheet)")
            if all(w.std() < 4 for w in win):
                say(None, f"scene {s['id']}: looks blank (page not loaded?)")

    # contact sheets
    out_dir = video.parent
    n = 12
    ims = [frame(video, d * (i + 0.5) / n) for i in range(n)]
    w, h = ims[0].size
    sheet = Image.new("RGB", (w * 4, h * 3), "white")
    for i, im in enumerate(ims):
        sheet.paste(im, ((i % 4) * w, (i // 4) * h))
    sp = out_dir / f"{video.stem}_sheet.png"
    sheet.save(sp)
    if tl:
        sc = tl["scenes"]
        cols = 4
        rows = (len(sc) + cols - 1) // cols
        S = Image.new("RGB", (w * cols, (h + 40) * rows), "white")
        dr = ImageDraw.Draw(S)
        font = ImageFont.truetype(str(FONT), 20)
        for i, s in enumerate(sc):
            im = frame(video, s["start"] + s["dur"] * 0.7)
            x, y = (i % cols) * w, (i // cols) * (h + 40)
            S.paste(im, (x, y + 40))
            dr.text((x + 10, y + 9), f"{i + 1}. {s['id']}  {s['start']:.1f}-{s['start'] + s['dur']:.1f}s", fill=(15, 23, 42), font=font)
        S.save(out_dir / f"{video.stem}_scenes.png")
        say(True, f"contact sheets: {sp} and {out_dir / (video.stem + '_scenes.png')} (look at them)")
    else:
        say(True, f"contact sheet: {sp} (look at it)")

    if stt and voiced:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            say(None, "speech-to-text skipped: faster-whisper is not installed (dv build --stt installs it via the 'stt' extra)")
        else:
            m = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=2)
            segs, _ = m.transcribe(pcm(video), language="en", beam_size=5, vad_filter=False)
            heard = " ".join(s.text for s in segs)
            norm = lambda t: [re.sub(r"[^a-z0-9$%]", "", w.lower()) for w in re.split(r"[\s\-–—]+", t) if re.sub(r"[^a-z0-9$%]", "", w.lower())]
            want, got = norm(" ".join(s["voice"] for s in tl["scenes"])), norm(heard)
            ratio = difflib.SequenceMatcher(a=want, b=got, autojunk=False).ratio()
            say(ratio >= 0.85 or None, f"speech-to-text matches the script {ratio:.0%} (numbers may be heard as words); heard: {heard.strip()[:300]}")
    (out_dir / f"{video.stem}_check.txt").write_text("\n".join(lines) + "\n")
    print(f"  {'ALL CHECKS PASSED' if not bad else f'{bad} CHECK(S) FAILED'}  (report: {out_dir / (video.stem + '_check.txt')})")
    return bad


if __name__ == "__main__":
    vid = Path(sys.argv[1])
    tlp = Path(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else None
    sys.exit(1 if run(vid, tlp, "--stt" in sys.argv) else 0)
