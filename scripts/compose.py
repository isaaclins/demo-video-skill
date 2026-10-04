"""Screen-Studio-style compositor: raw scene recording (2560x1440) + event log -> framed 1920x1080 scene.

Per frame: gradient background with the window shadow (pre-rendered once), the recording in a rounded window,
a camera that zooms and pans from the event log (clicks, typing, explicit focus; eased in-out, 0.6-0.9 s), a smoothed
large cursor, click ripples, the caption under the window and a thin progress rail. Scenes are composed in parallel
and concatenated by build.py.
"""
from __future__ import annotations

import json
import math
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FPS = 30
W, H = 1920, 1080
WIN = (176, 34, 1568, 882)          # x, y, w, h (16:9)
RADIUS = 20
CAP_CY = 972                         # caption centre line (below the window)
RAIL = (176, 1046, 1568, 5)          # progress rail
FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def ease_io(t):
    t = min(max(t, 0.0), 1.0)
    return 4 * t ** 3 if t < .5 else 1 - (-2 * t + 2) ** 3 / 2


# ------------------------------------------------------------------ palette
def _lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _mix(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def palette(hexcol: str | None) -> dict:
    hexcol = (hexcol or "#4f46e5").lstrip("#")
    if len(hexcol) == 3:
        hexcol = "".join(c * 2 for c in hexcol)
    b = tuple(int(hexcol[i:i + 2], 16) for i in (0, 2, 4))
    ink = (15, 23, 42)
    cap = b
    t = 0.0
    while (1.05) / (_lum(cap) + 0.05) < 7.0 and t < 1:  # white caption text needs >= 7:1
        t += 0.05
        cap = _mix(b, ink, t)
    ripple = b if _lum(b) < 0.6 else _mix(b, ink, 0.35)
    import colorsys
    h, l, sat = colorsys.rgb_to_hls(*(v / 255 for v in b))
    sat = max(sat, 0.45)

    def hue(dh, light, s_):
        return tuple(int(v * 255) for v in colorsys.hls_to_rgb((h + dh) % 1, light, min(1, s_)))
    # soft wallpaper: the brand hue and two neighbours, light enough that the white window still pops
    return {"brand": b, "cap": cap, "ripple": ripple, "tint1": hue(0, 0.80, sat * 0.9), "tint2": hue(0.09, 0.84, sat * 0.8),
            "tint3": hue(-0.07, 0.86, sat * 0.7), "base": hue(0.03, 0.93, sat * 0.35), "shadow": _mix(ink, b, 0.25)}


# ------------------------------------------------------------------ static layers
def background(pal: dict, out: Path) -> Path:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    base = np.zeros((H, W, 3), np.float32)
    top = np.array(_mix(pal["base"], (255, 255, 255), 0.35), np.float32)
    bot = np.array(pal["base"], np.float32)
    g = ((xx / W) * 0.4 + (yy / H) * 0.6)[..., None]
    base[:] = top * (1 - g) + bot * g

    def blob(cx, cy, rx, ry, col, a):
        d = np.clip(1 - np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2), 0, 1) ** 1.6
        return d[..., None] * a, np.array(col, np.float32)
    for cx, cy, rx, ry, col, a in ((W * 0.02, -H * 0.12, 1300, 900, pal["tint1"], 1.0),
                                   (W * 1.04, H * 1.1, 1300, 900, pal["tint2"], 1.0),
                                   (W * 1.0, -H * 0.1, 800, 600, pal["tint3"], 0.8),
                                   (-W * 0.02, H * 1.05, 800, 560, pal["tint3"], 0.6)):
        m, c = blob(cx, cy, rx, ry, col, a)
        base = base * (1 - m) + c * m
    rng = np.random.default_rng(3)
    base += rng.normal(0, 1.4, (H, W, 1)).astype(np.float32)  # faint grain against banding
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB")
    # window shadow: two blurred rounded rectangles
    wx, wy, ww, wh = WIN
    for off, blur, alpha in ((26, 44, 0.30), (8, 14, 0.16)):
        sh = Image.new("L", (W, H), 0)
        ImageDraw.Draw(sh).rounded_rectangle([wx + 10, wy + off, wx + ww - 10, wy + wh + off * 0.6], RADIUS, fill=int(255 * alpha))
        sh = sh.filter(ImageFilter.GaussianBlur(blur))
        img.paste(Image.new("RGB", (W, H), pal["shadow"]), (0, 0), sh)
    ImageDraw.Draw(img).rounded_rectangle([wx - 1, wy - 1, wx + ww, wy + wh], RADIUS + 1, outline=(205, 210, 220), width=1)
    img.save(out)
    return out


def caption_png(text: str, pal: dict, out: Path) -> tuple[Path, int, int]:
    font = ImageFont.truetype(str(FONTS / "Inter-SemiBold.ttf"), 34)
    l, t, r, b = font.getbbox(text)
    tw, th = r - l, 34
    px, py = 28, 13
    pw, ph = tw + 2 * px, th + 2 * py + 4
    pad = 24
    im = Image.new("RGBA", (pw + 2 * pad, ph + 2 * pad), (0, 0, 0, 0))
    sh = Image.new("L", im.size, 0)
    ImageDraw.Draw(sh).rounded_rectangle([pad, pad + 6, pad + pw, pad + ph + 6], 16, fill=70)
    sh = sh.filter(ImageFilter.GaussianBlur(10))
    im.paste(Image.new("RGBA", im.size, (0, 0, 0, 255)), (0, 0), sh)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([pad, pad, pad + pw, pad + ph], 16, fill=pal["cap"] + (242,))
    d.text((pad + px - l, pad + py + 1), text, font=font, fill=(255, 255, 255, 255))
    im.save(out)
    x = (W - im.width) // 2
    y = CAP_CY - im.height // 2
    return out, x, y


@lru_cache(maxsize=64)
def cursor_sprite(h: int) -> Image.Image:
    """macOS-like arrow, black with white outline and a soft shadow; hotspot at (pad, pad)."""
    ss = 4
    pts = [(2, 2), (2, 24), (7.6, 18.6), (11.4, 27.4), (15, 25.8), (11.3, 17.2), (19, 17.2)]
    sc = h / 28.0
    pad = int(h * 0.35)
    size = (int(22 * sc) + 2 * pad, int(30 * sc) + 2 * pad)
    big = Image.new("RGBA", (size[0] * ss, size[1] * ss), (0, 0, 0, 0))
    P = [((x - 2) * sc * ss + pad * ss, (y - 2) * sc * ss + pad * ss) for x, y in pts]
    sh = Image.new("RGBA", big.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon([(x + 1.2 * sc * ss, y + 2.2 * sc * ss) for x, y in P], fill=(0, 0, 0, 120))
    sh = sh.filter(ImageFilter.GaussianBlur(2.2 * sc * ss))
    big = Image.alpha_composite(big, sh)
    d = ImageDraw.Draw(big)
    d.polygon(P, fill=(255, 255, 255, 255))
    cx = sum(p[0] for p in P) / len(P)
    cy = sum(p[1] for p in P) / len(P)
    inner = [(cx + (x - cx) * 0.80 - 0.25 * sc * ss, cy + (y - cy) * 0.82 - 0.6 * sc * ss) for x, y in P]
    d.polygon(inner, fill=(17, 17, 17, 255))
    return big.resize(size, Image.LANCZOS)


@lru_cache(maxsize=256)
def ripple_sprite(r: int, a: int, col: tuple) -> Image.Image:
    ss = 3
    s = 2 * r + 8
    im = Image.new("RGBA", (s * ss, s * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = s * ss / 2
    d.ellipse([c - r * ss, c - r * ss, c + r * ss, c + r * ss], fill=col + (int(a * 0.22),),
              outline=(255, 255, 255, a), width=int(3 * ss))
    return im.resize((s, s), Image.LANCZOS)


def rounded_mask(w, h, r):
    ss = 4
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], r * ss, fill=255)
    return m.resize((w, h), Image.LANCZOS)


# ------------------------------------------------------------------ camera
def camera_track(ev: dict, opts: dict) -> list[tuple[float, float, float]]:
    n, VW, VH = ev["frames"], ev["vw"], ev["vh"]
    lead, tail = int(opts.get("lead", 0.8) * FPS), int(opts.get("tail", 0.9) * FPS)
    zc, zk, zmax = opts.get("click_zoom", 1.5), opts.get("type_zoom", 1.6), opts.get("max_zoom", 1.8)
    cur = ev["cursor"]
    segs = []  # [start, end, z, cx, cy, prio]
    for c in ev["clicks"]:
        f, x, y = c[0], c[1], c[2]
        if len(c) > 3 and not c[3]:
            continue
        z = c[4] if len(c) > 4 and c[4] else zc
        if z > 1.001:
            segs.append([f - lead, f + tail, z, x, y, 1])
    keys = sorted(ev["keys"])
    if keys and zk > 1.001:
        groups = [[keys[0], keys[0]]]
        for k in keys[1:]:
            if k - groups[-1][1] < FPS:
                groups[-1][1] = k
            else:
                groups.append([k, k])
        fields = ev.get("fields", [])
        for a, b in groups:
            fld = next((fl for fl in reversed(fields) if a - FPS <= fl[0] <= b), None)
            if fld:  # typing into a known field: centre on the field and keep all of it in view
                segs.append([a - lead, b + tail, max(1.0, min(zk, VW * 0.92 / max(fld[3], 1))), fld[1], fld[2], 1])
            else:
                p = next((cur[i] for i in range(min(a, len(cur) - 1), -1, -1) if cur[i]), [VW / 2, VH / 2])
                segs.append([a - lead, b + tail, zk, p[0] + 140, p[1], 1])
    for fo in ev["focus"]:
        segs.append([fo["f"], fo["until"] if fo["until"] is not None else n + 99, fo["z"], fo["cx"], fo["cy"], 2])
    navs = sorted(ev.get("navs", []))
    for sg in segs:
        sg[2] = min(sg[2], zmax)
        nxt = next((v for v in navs if sg[0] < v < sg[1]), None)
        if nxt is not None:  # a new page: whatever was zoomed is gone
            sg[1] = nxt
    segs.sort(key=lambda s: s[0])
    # bridge short gaps so the camera pans instead of zooming out and back in
    for a, b in zip(segs, segs[1:]):
        if 0 < b[0] - a[1] < int(1.1 * FPS) and a[5] == b[5]:
            a[1] = b[0]
    default = (1.0, VW / 2, VH / 2)
    targets = []
    for f in range(n):
        act = [s for s in segs if s[0] <= f < s[1]]
        targets.append((lambda s: (s[2], s[3], s[4]))(max(act, key=lambda s: (s[5], s[0]))) if act else default)
    out = []
    frm = to = default  # a scene never starts zoomed in abruptly
    k0, dur, last_t = 0, 1, default
    navset = set(navs)
    for f, tg in enumerate(targets):
        if f in navset and tg == default:  # page change = picture cut: the camera cuts back to the full page too
            frm = to = last_t = default
            k0 = f
        if tg != last_t:
            val = _lerp(frm, to, ease_io((f - k0) / dur))
            frm, to, k0 = val, tg, f
            dur = int((0.8 if tg[0] > val[0] + 1e-3 else 0.9 if tg[0] < val[0] - 1e-3 else 0.6) * FPS)
            last_t = tg
        out.append(_lerp(frm, to, ease_io((f - k0) / dur)))
    return out


def _lerp(a, b, e):
    za, zb = math.log(a[0]), math.log(b[0])
    return (math.exp(za + (zb - za) * e), a[1] + (b[1] - a[1]) * e, a[2] + (b[2] - a[2]) * e)


def smooth_cursor(cur: list) -> list:
    """forward-backward exponential smoothing; None (hidden) breaks the chain."""
    out = [None if c is None else list(c) for c in cur]
    for rng in (range(1, len(out)), range(len(out) - 2, -1, -1)):
        for i in rng:
            j = i - 1 if rng.step == 1 else i + 1
            if out[i] is not None and out[j] is not None:
                out[i] = [out[j][0] + (out[i][0] - out[j][0]) * 0.55, out[j][1] + (out[i][1] - out[j][1]) * 0.55]
    return out


def raw_size(raw: Path) -> tuple[int, int]:
    s = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                        str(raw)], capture_output=True, text=True).stdout.strip().split(",")
    return int(s[0]), int(s[1])


# ------------------------------------------------------------------ compose one scene
def compose_scene(job: dict) -> str:
    raw, out = Path(job["raw"]), Path(job["out"])
    ev = json.loads(Path(job["events"]).read_text())
    n = ev["frames"]
    RW, RH = raw_size(raw)
    K = RW / ev["vw"]
    wx, wy, ww, wh = WIN
    mask = rounded_mask(ww, wh, RADIUS)
    bg = Image.open(job["bg"]).convert("RGB")
    card = job.get("card")
    cam = camera_track(ev, job.get("camera") or {}) if not card else [(1.0, ev["vw"] / 2, ev["vh"] / 2)] * n
    curs = smooth_cursor(ev["cursor"]) if not card else [None] * n
    clicks = ev["clicks"] if not card else []
    col = tuple(job["ripple"])
    caps = [(int(a * FPS), int(b * FPS), Image.open(p).convert("RGBA"), x, y) for a, b, p, x, y in job["captions"]]
    rail = job.get("rail")  # {"segs": [[t0, t1], ...], "offset": scene start, "idx": i, "color": rgb}
    draft = job.get("draft")
    dec = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", str(raw), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
                            "-i", "-", "-c:v", "libx264", "-preset", "ultrafast" if draft else "medium", "-crf", "22" if draft else "16",
                            "-pix_fmt", "yuv420p", "-threads", "2", str(out)], stdin=subprocess.PIPE)
    fsz = RW * RH * 3
    last = None
    resample = Image.BILINEAR if draft else Image.BICUBIC
    for f in range(n):
        buf = dec.stdout.read(fsz)
        if len(buf) < fsz:
            buf = last
            if buf is None:
                raise RuntimeError(f"no frames in {raw}")
        last = buf
        src = Image.frombuffer("RGB", (RW, RH), buf, "raw", "RGB", 0, 1)
        z, cx, cy = cam[f]
        cw, ch = RW / z, RH / z
        x0 = min(max(cx * K - cw / 2, 0), RW - cw)
        y0 = min(max(cy * K - ch / 2, 0), RH - ch)
        content = src.resize((ww, wh), resample, box=(x0, y0, x0 + cw, y0 + ch))
        fr = bg.copy()
        fr.paste(content, (wx, wy), mask)
        sx, sy = ww / cw, wh / ch

        def to_screen(px, py):
            return wx + (px * K - x0) * sx, wy + (py * K - y0) * sy

        for cf, px, py, *_ in clicks:  # ripples
            k = (f - cf) / (0.5 * FPS)
            if 0 <= k <= 1:
                X, Y = to_screen(px, py)
                sp = ripple_sprite(int(12 + 34 * ease_io(k) * (z ** 0.5)), int(230 * (1 - k)), col)
                fr.paste(sp, (int(X - sp.width / 2), int(Y - sp.height / 2)), sp)
        c = curs[f] if f < len(curs) else None
        if c is not None:
            X, Y = to_screen(*c)
            press = any(0 <= f - cf < 5 for cf, *_ in clicks)
            h = int(round(36 * (z ** 0.45) * (0.86 if press else 1.0)))
            sp = cursor_sprite(h)
            pad = int(h * 0.35)
            if wx - 40 < X < wx + ww + 40 and wy - 40 < Y < wy + wh + 40:
                fr.paste(sp, (int(X - pad), int(Y - pad)), sp)
        for a, b, im, x, y in caps:
            if a <= f < b:
                fr.paste(im, (x, y), im)
        if rail:
            draw_rail(fr, rail, rail["offset"] + f / FPS)
        enc.stdin.write(fr.tobytes())
    dec.stdout.close()
    dec.wait()
    enc.stdin.close()
    enc.wait()
    return str(out)


def draw_rail(fr: Image.Image, rail: dict, t: float):
    x, y, w, h = RAIL
    segs = rail["segs"]
    total = segs[-1][1]
    gap = 6
    d = ImageDraw.Draw(fr, "RGBA")
    usable = w - gap * (len(segs) - 1)
    xx = x
    for a, b in segs:
        sw = max(4, usable * (b - a) / total)
        d.rounded_rectangle([xx, y, xx + sw, y + h], h / 2, fill=(15, 23, 42, 26))
        p = min(max((t - a) / max(b - a, 1e-6), 0), 1)
        if p > 0:
            d.rounded_rectangle([xx, y, xx + max(h, sw * p), y + h], h / 2, fill=tuple(rail["color"]) + (235,))
        xx += sw + gap
