"""demo-video: website -> polished product demo video (<= 60 s, 1920x1080, H.264/AAC), Screen Studio style.

  dv explore <url> [--out DIR]            look at a page: copy, headings, clickable elements + selectors, screenshots
  dv try   <scenes.json> [--every]        run every action fast (no recording): finds broken selectors, shows timing
  dv build <scenes.json> --draft          fast silent draft (estimated voice timing, low-res capture, no TTS spend)
  dv build <scenes.json>                  final: voice -> record -> composite -> mix -> out/demo.mp4 + checks
  dv check <video.mp4> [--stt]            duration, codecs, loudness, caption/voice sync, contact sheet, (speech-to-text)

Everything is written next to scenes.json: build/<name>/ (work files), cache/tts/ (paid voice takes), out/ (videos,
contact sheets, check reports). Voice: ElevenLabs if ELEVENLABS_API_KEY is set, else captions only (or --local-tts).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(HERE))
import audio  # noqa: E402
import compose  # noqa: E402
import voice as V  # noqa: E402

FPS = 30
LIMIT = 60.0
ACTIONS = {"goto", "wait_for", "click", "dblclick", "type", "press", "scroll", "scroll_by", "hover", "focus", "unfocus", "pause", "at", "eval"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fail(msg: str, code=1):
    print(f"\nERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ------------------------------------------------------------------ scenes.json
def load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        fail(f"{path} is not valid JSON: {e}")
    errs = []
    if not isinstance(spec.get("url"), str) or not spec["url"].startswith(("http://", "https://")):
        errs.append('"url" must be the site address, e.g. "https://example.com/"')
    sc = spec.get("scenes")
    if not isinstance(sc, list) or not sc:
        errs.append('"scenes" must be a non-empty list')
        sc = []
    ids = set()
    for i, s in enumerate(sc):
        where = f"scene #{i + 1} ({s.get('id', '?')})"
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", str(s.get("id", ""))):
            errs.append(f"{where}: id must be lowercase letters/digits/-/_")
        if s.get("id") in ids:
            errs.append(f"{where}: duplicate id")
        ids.add(s.get("id"))
        if not isinstance(s.get("voice", ""), str):
            errs.append(f"{where}: voice must be a string")
        for k in ("min", "pre", "post"):
            if k in s and not isinstance(s[k], (int, float)):
                errs.append(f"{where}: {k} must be a number (seconds)")
        if "card" in s and not isinstance(s["card"], dict):
            errs.append(f"{where}: card must be an object")
        acts = s.get("actions", [])
        if not isinstance(acts, list):
            errs.append(f"{where}: actions must be a list")
            continue
        for j, a in enumerate(acts):
            ks = [k for k in a if k in ACTIONS and not (k == "at" and len([x for x in a if x in ACTIONS]) > 1)]
            if len(ks) != 1:
                errs.append(f"{where} action #{j}: needs exactly one of {sorted(ACTIONS)}, got {list(a)}")
    if errs:
        fail("scenes.json has problems:\n  - " + "\n  - ".join(errs))
    return spec


def scene_timing(scenes: list[dict]) -> float:
    t = 0.0
    for i, s in enumerate(scenes):
        last = i == len(scenes) - 1
        s["pre"] = float(s.get("pre", 0.4 if i == 0 else 0.2))
        post = float(s.get("post", 1.0 if last else 0.35))
        s["dur"] = round(round(max(float(s.get("min", 0)), s["pre"] + s["vdur"] + post) * FPS) / FPS, 4)
        s["start"] = round(t, 4)
        t += s["dur"]
    return t


# ------------------------------------------------------------------ voice
def do_voice(spec: dict, scenes: list[dict], proj: Path, mode: str, a) -> dict:
    cache = proj / "cache" / "tts"
    info = {"mode": mode}
    if mode == "elevenlabs":
        vcfg = spec.get("voice") or {}
        vid, vname, model = V.choose(cache, vcfg.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID"),
                                     vcfg.get("model") or os.environ.get("ELEVENLABS_MODEL"), log)
        settings = vcfg.get("settings") or {}
        need = [s for s in scenes if s["voice"].strip() and not V.cached(cache, s["voice"], vid, model, settings)]
        chars = sum(len(s["voice"]) for s in need)
        spent = V.spent(cache)
        budget = int(a.tts_budget or os.environ.get("DEMO_VIDEO_TTS_BUDGET", 4000))
        log(f"voice: ElevenLabs {vname or vid} / {model}; {len(need)} new takes, {chars} characters "
            f"(this project spent {spent} so far, budget {budget})")
        if spent + chars > budget:
            fail(f"ElevenLabs budget: {spent} spent + {chars} needed > {budget} characters. Shorten the lines, reuse "
                 f"unchanged lines, or raise the budget with --tts-budget N (or DEMO_VIDEO_TTS_BUDGET).")
        if need:
            q = V.quota()
            if q and q[0] + chars > q[1]:
                fail(f"not enough ElevenLabs characters left on the account ({q[1] - q[0]} left, {chars} needed)")
        for s in scenes:
            if not s["voice"].strip():
                s.update(audio=None, words=[], vdur=0.0)
                continue
            mp3, aj = V.cached(cache, s["voice"], vid, model, settings) or V.synth(cache, s["voice"], vid, model, settings)
            al = json.loads(aj.read_text())
            al = al.get("alignment") or al.get("normalized_alignment")
            wav = V.to_wav(mp3, cache / "wav" / f"{mp3.stem}.wav")
            s.update(audio=str(wav), words=V.words_from_alignment(al), vdur=V.probe_dur(wav))
        info.update(voice=vid, voice_name=vname, model=model, new_chars=chars, project_chars=V.spent(cache))
    elif mode == "local":
        for s in scenes:
            if not s["voice"].strip():
                s.update(audio=None, words=[], vdur=0.0)
                continue
            wav = V.to_wav(V.local_synth(cache, s["voice"]), cache / "wav" / f"local_{V.take_id(s['voice'], 'l', 'l', {})}.wav")
            d = V.probe_dur(wav)
            s["words"], s["vdur"] = V.estimate_words(s["voice"], scale_to=d * 0.97)
            s.update(audio=str(wav), vdur=d)
        info["engine"] = V.local_engine()
    else:
        for s in scenes:
            s["audio"] = None
            s["words"], s["vdur"] = V.estimate_words(s["voice"]) if s["voice"].strip() else ([], 0.0)
    return info


# ------------------------------------------------------------------ captions
def caption_chunks(scenes: list[dict], max_chars=56) -> list[dict]:
    caps = []
    for s in scenes:
        ws = [[w, s["start"] + s["pre"] + a, s["start"] + s["pre"] + b] for w, a, b in s["words"]]
        chunk = []
        for i, w in enumerate(ws):
            chunk.append(w)
            text = " ".join(x[0] for x in chunk)
            nxt = ws[i + 1][0] if i + 1 < len(ws) else None
            brk = nxt is None or len(text) + 1 + len(nxt) > max_chars or (w[0][-1] in ".?!:" and len(text) > 18) \
                or (w[0][-1] in ",;" and len(text) > 34)
            if brk:
                caps.append({"text": text, "t0": chunk[0][1] - 0.05, "t1": chunk[-1][2] + 0.25, "scene": s["id"]})
                chunk = []
    merged = []  # never leave a tiny orphan line on its own
    for c in caps:
        if merged and merged[-1]["scene"] == c["scene"] and len(c["text"]) < 16 and len(merged[-1]["text"]) + len(c["text"]) < 70:
            merged[-1]["text"] += " " + c["text"]
            merged[-1]["t1"] = c["t1"]
        else:
            merged.append(c)
    for a, b in zip(merged, merged[1:]):  # no overlap, no flicker gaps < 0.3 s
        if b["t0"] - a["t1"] < 0.3:
            a["t1"] = b["t0"]
    for c in merged:
        s = next(x for x in scenes if x["id"] == c["scene"])
        c["t1"] = min(c["t1"], s["start"] + s["dur"])
        c["t0"] = max(c["t0"], s["start"])
    return merged


# ------------------------------------------------------------------ recorder
def node_bin() -> str:
    n = shutil.which("node")
    if not n:
        fail("Node.js is not installed (needed for the Playwright recorder). Install Node 18+ and run `npm install` in "
             f"{SKILL}")
    if not (SKILL / "node_modules" / "playwright").exists() and not (SKILL / "node_modules" / "playwright-core").exists():
        log("installing the Playwright Node package (one time) ...")
        subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--silent"], cwd=SKILL, check=True)
    return n


def record(plan: dict, build: Path) -> dict:
    pf = build / ("try_plan.json" if plan.get("dry") else "plan.json")
    pf.write_text(json.dumps(plan, indent=1))
    r = subprocess.run([node_bin(), str(HERE / "record.mjs"), str(pf)], cwd=SKILL)
    rep_f = build / ("try_report.json" if plan.get("dry") else "record_report.json")
    rep = json.loads(rep_f.read_text()) if rep_f.exists() else {"ok": False, "scenes": []}
    if r.returncode == 3:
        fail("the browser could not start (see above)")
    rep["returncode"] = r.returncode
    return rep


def make_plan(spec: dict, scenes: list[dict], build: Path, draft: bool, dry: bool) -> dict:
    return {"url": spec["url"], "viewport": [1280, 720], "scale": 1 if draft else 2, "fps": FPS, "draft": draft, "dry": dry,
            "outdir": str(build / "scenes"), "hide": spec.get("hide", []), "css": spec.get("css", ""),
            "locale": spec.get("locale", "en-US"), "timezone": spec.get("timezone"), "color_scheme": spec.get("color_scheme", "light"),
            "brand": spec.get("brand", "auto"),
            "scenes": [{"id": s["id"], "dur": s["dur"], "pre": s["pre"], "words": s["words"], "actions": s.get("actions", []),
                        "card": s.get("card")} for s in scenes]}


# ------------------------------------------------------------------ commands
def cmd_try(a):
    path = Path(a.scenes).resolve()
    spec = load_spec(path)
    proj = path.parent
    build = proj / "build" / "try"
    build.mkdir(parents=True, exist_ok=True)
    scenes = [dict(s, voice=s.get("voice", "")) for s in spec["scenes"]]
    for s in scenes:
        s["words"], s["vdur"] = V.estimate_words(s["voice"]) if s["voice"].strip() else ([], 0.0)
    total = scene_timing(scenes)
    log(f"try: {len(scenes)} scenes, voice-planned length {total:.1f}s (estimate at {V.EST_CPS} chars/s)")
    plan = make_plan(spec, scenes, build, draft=True, dry=True)
    plan["every"] = a.every
    rep = record(plan, build)
    act_total = 0.0
    print(f"\n{'scene':<14}{'voice s':>8}{'actions s':>10}  notes")
    for r in rep["scenes"]:
        s = next(x for x in scenes if x["id"] == r["id"])
        act_total += max(s["dur"], r["action_time"] + 0.35)
        notes = "; ".join(r["warnings"])
        if r["action_time"] > s["dur"]:
            notes = (notes + "; " if notes else "") + "actions take longer than the line: shorten them or add words / min"
        if r["error"]:
            notes = f"ERROR action #{r['error']['action']}: {r['error']['message']}"
        print(f"{r['id']:<14}{s['dur']:>8.2f}{r['action_time']:>10.2f}  {notes}")
    print(f"\nestimated video length {act_total:.1f}s (limit {LIMIT:.0f}s). Screenshots (end of each scene): {build / 'screens'}/<scene>.png")
    if not rep.get("ok"):
        fail(f"a scene failed; see {build / 'errors'} and the list of clickable elements above", 2)
    if act_total > LIMIT - 1:
        fail(f"estimated {act_total:.1f}s is over the limit: cut words or actions", 2)


def cmd_build(a):
    t_start = time.time()
    path = Path(a.scenes).resolve()
    spec = load_spec(path)
    proj = path.parent
    name = a.name or ("draft" if a.draft else "demo")
    build = proj / "build" / name
    if build.exists():
        shutil.rmtree(build / "scenes", ignore_errors=True)
        shutil.rmtree(build / "comp", ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    out_dir = proj / "out"
    out_dir.mkdir(exist_ok=True)
    scenes = [dict(s, voice=s.get("voice", "").strip()) for s in spec["scenes"]]

    if a.draft or a.no_voice:
        mode = "none"
    elif V.key():
        mode = "elevenlabs"
    elif a.local_tts:
        if not V.local_engine():
            fail("--local-tts: no local TTS found (macOS `say`, or `piper` with PIPER_MODEL set)")
        mode = "local"
    else:
        mode = "none"
    note = None
    if mode == "none" and not a.draft:
        note = ("NOTE: no ELEVENLABS_API_KEY set, so this video has NO VOICE-OVER: the script is burned in as captions "
                "(music and click sounds only). Set ELEVENLABS_API_KEY and run the same build again for a narrated video.")
        if V.local_engine():
            note += f" A local voice ({V.local_engine()}) is available too: add --local-tts."
        log(note)
    vinfo = do_voice(spec, scenes, proj, mode, a)
    planned = scene_timing(scenes)
    log(f"{name}: {len(scenes)} scenes, planned {planned:.2f}s, voice: {mode}")
    for s in scenes:
        log(f"  {s['id']:<12} {s['dur']:5.2f}s  {s['voice'][:80]}")
    if planned > LIMIT - 0.5:
        fail(f"the script alone needs {planned:.1f}s (limit {LIMIT:.0f}s): shorten the voice lines")

    # record
    log(f"recording ({'draft, 1280x720' if a.draft else '2560x1440'}) ...")
    rep = record(make_plan(spec, scenes, build, a.draft, False), build)
    if not rep.get("ok"):
        fail(f"recording stopped; fix the action above (error screenshot in {build / 'errors'}) and run again", 2)
    t = 0.0
    for s in scenes:
        ev = json.loads((build / "scenes" / s["id"] / "events.json").read_text())
        s["dur"] = ev["frames"] / FPS
        s["start"] = round(t, 4)
        t += s["dur"]
    total = t
    over = [r for r in rep["scenes"] if r["overrun"] > 0.05]
    for r in over:
        log(f"  {r['id']}: actions took {r['overrun']:.2f}s longer than its line (scene extended; voice stays in sync)")
    if total > LIMIT:
        fail(f"video would be {total:.2f}s (limit {LIMIT:.0f}s). Overlong scenes: " +
             ", ".join(f"{r['id']} +{r['overrun']:.1f}s" for r in over) + ". Trim actions or words.")

    # overlays
    pal = compose.palette(json.loads((build / "brand.json").read_text()).get("color") if (build / "brand.json").exists() else None)
    bg = compose.background(pal, build / "bg.png")
    caps = caption_chunks(scenes) if a.captions else []
    (build / "caps").mkdir(exist_ok=True)
    for j, c in enumerate(caps):
        f, x, y = compose.caption_png(c["text"], pal, build / "caps" / f"cap_{j:03d}.png")
        c.update(png=str(f), x=x, y=y)

    # composite
    segs = [[s["start"], s["start"] + s["dur"]] for s in scenes]
    jobs, outs = [], []
    (build / "comp").mkdir(exist_ok=True)
    cam_global = spec.get("camera") or {}
    for i, s in enumerate(scenes):
        out = build / "comp" / f"{i:02d}_{s['id']}.mp4"
        jobs.append({"raw": str(build / "scenes" / s["id"] / "raw.mp4"), "events": str(build / "scenes" / s["id"] / "events.json"),
                     "out": str(out), "bg": str(bg), "card": bool(s.get("card")), "ripple": list(pal["ripple"]), "draft": a.draft,
                     "camera": {**cam_global, **(s.get("camera") or {})},
                     "captions": [(c["t0"] - s["start"], c["t1"] - s["start"], c["png"], c["x"], c["y"]) for c in caps if c["scene"] == s["id"]],
                     "rail": {"segs": segs, "offset": s["start"], "color": list(pal["brand"])} if a.rail else None})
        outs.append(out)
    log(f"compositing {len(jobs)} scenes ({a.jobs} workers) ...")
    with ProcessPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(compose.compose_scene, jobs))
    lst = build / "concat.txt"
    lst.write_text("".join(f"file '{o}'\n" for o in outs))
    video_only = build / "video.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(video_only)], check=True)

    # audio
    clicks, keys = [], []
    for s in scenes:
        ev = json.loads((build / "scenes" / s["id"] / "events.json").read_text())
        clicks += [s["start"] + c[0] / FPS for c in ev["clicks"]]
        keys += [s["start"] + k for k in ev.get("keys_t", [])]
    voices = [(s["start"] + s["pre"], Path(s["audio"])) for s in scenes if s.get("audio")]
    music = 0.0 if not a.music or spec.get("music") is False else (0.07 if voices else 0.09)
    mixf = build / "mix.wav"
    audio.mix(total, voices, clicks if a.sfx else [], keys if a.sfx else [], music, mixf)
    normf = build / "mix_norm.wav"
    audio.loudnorm(mixf, normf, -16.0 if voices else -20.0)

    out = out_dir / f"{name}.mp4"
    fo = max(0.0, total - 0.6)
    enc = ["-preset", "veryfast", "-crf", "23"] if a.draft else ["-preset", "slow", "-crf", "18"]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_only), "-i", str(normf),
                    "-vf", f"fade=t=in:st=0:d=0.3,fade=t=out:st={fo:.2f}:d=0.6", "-af", f"afade=t=out:st={fo:.2f}:d=0.6",
                    "-c:v", "libx264", *enc, "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-t", f"{total:.3f}", "-movflags", "+faststart",
                    "-threads", str(max(2, a.jobs)), str(out)], check=True)
    tl = {"name": name, "total": total, "voice": vinfo, "draft": a.draft,
          "scenes": [{k: s.get(k) for k in ("id", "voice", "start", "dur", "pre", "words", "audio")} for s in scenes],
          "captions": [{k: c[k] for k in ("text", "t0", "t1", "scene")} for c in caps], "clicks": clicks,
          "record": rep["scenes"]}
    (build / "timeline.json").write_text(json.dumps(tl, indent=1))
    if note:
        (out_dir / f"{name}_NOTE.txt").write_text(note + "\n")
    took = time.time() - t_start
    log(f"wrote {out} ({V.probe_dur(out):.2f}s, {out.stat().st_size / 1e6:.1f} MB) in {took / 60:.1f} min")
    if mode == "elevenlabs":
        log(f"ElevenLabs characters: {vinfo['new_chars']} this build, {vinfo['project_chars']} for this project so far")
    import check
    bad = check.run(out, build / "timeline.json", stt=a.stt)
    if note:
        log(note)
    sys.exit(1 if bad else 0)


def cmd_explore(a):
    out = Path(a.out or "explore").resolve()
    out.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([node_bin(), str(HERE / "explore.mjs"), a.url, str(out)], cwd=SKILL)
    sys.exit(r.returncode)


def cmd_check(a):
    import check
    p = Path(a.video).resolve()
    tl = Path(a.timeline) if a.timeline else p.parent.parent / "build" / p.stem / "timeline.json"
    sys.exit(1 if check.run(p, tl if tl.exists() else None, stt=a.stt) else 0)


def main():
    ap = argparse.ArgumentParser(prog="dv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the video")
    b.add_argument("scenes")
    b.add_argument("--draft", action="store_true", help="fast silent draft: no TTS, 1280x720 capture, quick encode -> out/draft.mp4")
    b.add_argument("--no-voice", action="store_true", help="captions only, even if ELEVENLABS_API_KEY is set")
    b.add_argument("--local-tts", action="store_true", help="without ElevenLabs: use macOS `say` or piper if present")
    b.add_argument("--tts-budget", type=int, default=None, help="max ElevenLabs characters for this project (default 4000)")
    b.add_argument("--music", action=argparse.BooleanOptionalAction, default=True, help="quiet generated music bed")
    b.add_argument("--sfx", action=argparse.BooleanOptionalAction, default=True, help="click and typing sounds")
    b.add_argument("--captions", action=argparse.BooleanOptionalAction, default=True)
    b.add_argument("--rail", action=argparse.BooleanOptionalAction, default=True, help="progress rail under the captions")
    b.add_argument("--stt", action="store_true", help="also transcribe the result with faster-whisper (if installed)")
    b.add_argument("--jobs", type=int, default=int(os.environ.get("DEMO_VIDEO_JOBS", 2)))
    b.add_argument("--name", default=None, help="output name (default demo, or draft with --draft)")
    b.set_defaults(fn=cmd_build)
    t = sub.add_parser("try", help="run all actions fast without recording")
    t.add_argument("scenes")
    t.add_argument("--every", action="store_true", help="screenshot after every action (screens/<scene>_<n>.png): explore how the site reacts")
    t.set_defaults(fn=cmd_try)
    e = sub.add_parser("explore", help="inspect a page")
    e.add_argument("url")
    e.add_argument("--out", default=None)
    e.set_defaults(fn=cmd_explore)
    c = sub.add_parser("check", help="check a finished video")
    c.add_argument("video")
    c.add_argument("--timeline", default=None)
    c.add_argument("--stt", action="store_true")
    c.set_defaults(fn=cmd_check)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
