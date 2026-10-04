// Deterministic frame-by-frame recorder for demo-video.
//
//   node record.mjs <plan.json>
//
// Reads the plan written by build.py (scenes with their DSL actions and planned lengths), drives one Chromium page
// through all scenes and captures every video frame with CDP Page.captureScreenshot (viewport 1280x720 at scale 2 =
// 2560x1440, so the compositor can zoom to ~1.8x and stay sharp). Video time only advances when a frame is captured,
// so slow pages never show up as stutter: waiting for the network costs no video time. CSS animations are slowed
// with CDP Animation.setPlaybackRate so they roughly play at video speed.
//
// Nothing cosmetic is drawn here. Every cursor position, click, keystroke and focus request goes to events.json; the
// compositor (compose.py) adds window, camera, cursor, ripples and captions from that log.
//
// Output per scene: <outdir>/<id>/raw.mp4 + events.json. Also <outdir>/../brand.json and record_report.json.
// plan.dry = true runs every action without capturing (fast) and saves one screenshot per scene into <build>/screens/.

import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
let chromium;
try {
  ({ chromium } = await import('playwright'));
} catch {
  try {
    ({ chromium } = await import('playwright-core'));
  } catch {
    console.error('ERROR: Node package "playwright" is missing. Run: (cd ' + path.dirname(HERE) + ' && npm install)');
    process.exit(3);
  }
}

const plan = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const FPS = plan.fps || 30;
const [VW, VH] = plan.viewport || [1280, 720];
const SCALE = plan.scale || 2;
const DRY = !!plan.dry;
const OUT = plan.outdir;
const BUILD = path.dirname(OUT);
const BASE = plan.url;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toTimeString().slice(0, 8), ...a);

const ease = (t) => { t = Math.min(Math.max(t, 0), 1); return t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2; };

// ------------------------------------------------------------------ page-side helpers
const INIT_JS = `(() => {
  if (window.__dv) return; window.__dv = true;
  // the capture slows the animation clock (CDP Animation.setPlaybackRate), which also skews rAF timestamps against
  // performance.now(); give rAF callbacks performance.now() so JS animations do not compute negative progress.
  const r = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => r(() => cb(performance.now()));
  const css = ${JSON.stringify(
    'html{scroll-behavior:auto!important} ::-webkit-scrollbar{display:none!important} *{scrollbar-width:none!important}' +
    (plan.hide && plan.hide.length ? plan.hide.join(',') + '{display:none!important}' : '') + (plan.css || ''))};
  const add = () => { if (document.getElementById('__dvcss')) return; const s = document.createElement('style');
    s.id = '__dvcss'; s.textContent = css; (document.head || document.documentElement).appendChild(s); };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', add); else add();
  new MutationObserver(add).observe(document, {childList: true, subtree: false});
})();`;

const BRAND_JS = () => {
  const cv = document.createElement('canvas'); cv.width = cv.height = 1;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  const rgb = (c) => {
    if (!c || c === 'transparent' || c === 'none' || /^rgba\([^)]*,\s*0(\.0*)?\)$/.test(c)) return null;
    cx.clearRect(0, 0, 1, 1); cx.fillStyle = '#000'; cx.fillStyle = c; cx.fillRect(0, 0, 1, 1);
    const d = cx.getImageData(0, 0, 1, 1).data; if (d[3] < 128) return null; return [d[0], d[1], d[2]];
  };
  const hsl = ([r, g, b]) => { r /= 255; g /= 255; b /= 255; const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
    const s = mx === mn ? 0 : l > 0.5 ? (mx - mn) / (2 - mx - mn) : (mx - mn) / (mx + mn); return [s, l]; };
  const score = {};
  const add = (c, w) => { const p = rgb(c); if (!p) return; const [s, l] = hsl(p); if (s < 0.28 || l < 0.14 || l > 0.82) return;
    const k = p.join(','); score[k] = (score[k] || 0) + w; };
  const meta = document.querySelector('meta[name="theme-color"]'); if (meta) add(meta.content, 6);
  const rs = getComputedStyle(document.documentElement);
  for (const v of ['--primary', '--brand', '--accent', '--color-primary', '--primary-color', '--brand-color', '--color-brand', '--theme-color'])
    add(rs.getPropertyValue(v).trim(), 5);
  for (const el of document.querySelectorAll('button, a, [role=button], input[type=submit], [class*=btn], [class*=button]')) {
    const r = el.getBoundingClientRect(); if (r.width < 8 || r.height < 8) continue;
    const cs = getComputedStyle(el); add(cs.backgroundColor, 3 * Math.min(1, (r.width * r.height) / 6000)); add(cs.color, 0.6); add(cs.borderTopColor, 0.4);
  }
  for (const el of document.querySelectorAll('h1, h2, [class*=logo], [class*=brand], header svg, header path')) {
    const cs = getComputedStyle(el); add(cs.color, 0.6); add(cs.fill, 0.6);
  }
  const best = Object.entries(score).sort((a, b) => b[1] - a[1])[0];
  const hex = best ? '#' + best[0].split(',').map((n) => (+n).toString(16).padStart(2, '0')).join('') : null;
  const icons = [...document.querySelectorAll('link[rel~="apple-touch-icon"], link[rel~="icon"]')]
    .map((l) => ({ href: l.href, size: parseInt((l.getAttribute('sizes') || '0').split('x')[0]) || (l.rel.includes('apple') ? 180 : 16) }))
    .filter((i) => i.href && !i.href.endsWith('.ico')).sort((a, b) => b.size - a.size);
  const og = (p) => (document.querySelector(`meta[property="${p}"]`) || {}).content || '';
  const site = og('og:site_name') || document.title.split(/\s[·|—–\-:]\s/)[0];
  return { color: hex, icon: icons.length ? icons[0].href : null, site: site.trim(), title: document.title,
           description: (document.querySelector('meta[name="description"]') || {}).content || og('og:description') };
};

// ------------------------------------------------------------------ recorder
class Rec {
  constructor(page, cdp, dir, planned) {
    this.page = page; this.cdp = cdp; this.dir = dir; this.planned = planned;
    this.n = 0; this.cur = null;
    this.ev = { fps: FPS, vw: VW, vh: VH, scale: DRY ? 1 : SCALE, planned, cursor: [], clicks: [], keys: [], keys_t: [], focus: [], navs: [], fields: [], marks: {}, warnings: [] };
    this.rate = 0.25; this.tLast = Date.now();
    if (!DRY) {
      fs.mkdirSync(dir, { recursive: true });
      const enc = plan.draft ? ['-preset', 'ultrafast', '-crf', '20'] : ['-preset', 'fast', '-crf', '8'];
      this.ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-', '-c:v', 'libx264',
        ...enc, '-pix_fmt', 'yuv420p', '-r', String(FPS), '-threads', '2', path.join(dir, 'raw.mp4')], { stdio: ['pipe', 'inherit', 'inherit'] });
      this.ffDone = new Promise((r) => this.ff.on('close', r));
    }
  }
  get t() { return this.n / FPS; }
  async setRate(r) { try { await this.cdp.send('Animation.setPlaybackRate', { playbackRate: r }); } catch {} }
  async start() { try { await this.cdp.send('Animation.enable'); } catch {} await this.setRate(this.rate); this.tLast = Date.now(); }

  async snap() {
    this.ev.cursor.push(this.cur ? [+this.cur[0].toFixed(2), +this.cur[1].toFixed(2)] : null);
    this.n += 1;
    if (DRY) return;
    let d;
    for (let i = 0; i < 5; i++) {
      try {
        d = await this.cdp.send('Page.captureScreenshot', plan.draft ? { format: 'jpeg', quality: 88, optimizeForSpeed: true } : { format: 'png', optimizeForSpeed: true });
        break;
      } catch (e) { await sleep(250); }
    }
    if (!d) throw new Error('screenshot failed');
    const buf = Buffer.from(d.data, 'base64');
    if (!this.ff.stdin.write(buf)) await new Promise((r) => this.ff.stdin.once('drain', r));
    if (this.n % 15 === 0) { // keep CSS animation speed roughly in video time
      const now = Date.now(); const real = (now - this.tLast) / 15000; this.tLast = now;
      const want = Math.max(0.05, Math.min(1, 1 / FPS / Math.max(real, 1e-3)));
      if (Math.abs(want - this.rate) > 0.03) { this.rate = want; await this.setRate(want); }
    }
  }
  async hold(sec) { for (let i = 0, k = Math.round(sec * FPS); i < k; i++) await this.snap(); }
  async until(t) { while (this.n < Math.round(t * FPS)) await this.snap(); }
  async tween(sec, fn) { const k = Math.max(1, Math.round(sec * FPS)); for (let i = 1; i <= k; i++) { await fn(ease(i / k)); await this.snap(); } }
  // real-time wait without video time (network, rendering)
  async settle(ms) { await this.setRate(1); await sleep(ms); await this.setRate(this.rate); this.tLast = Date.now(); }
  async finish(minSec) {
    const minF = Math.round(minSec * FPS);
    const over = this.n - Math.round(this.planned * FPS);
    while (this.n < minF) await this.snap();
    this.ev.frames = this.n;
    this.ev.overrun = Math.max(0, over) / FPS;
    if (!DRY) { this.ff.stdin.end(); await this.ffDone; fs.writeFileSync(path.join(this.dir, 'events.json'), JSON.stringify(this.ev)); }
    return this.ev;
  }
}

// ------------------------------------------------------------------ targets
function locate(page, t) {
  if (t == null) throw new Error('missing target');
  if (typeof t === 'string') return page.locator(t).filter({ visible: true }).first();
  let l;
  if (t.text) l = page.getByText(t.text, { exact: !!t.exact });
  else if (t.role) l = page.getByRole(t.role, t.name ? { name: t.name, exact: !!t.exact } : {});
  else if (t.label) l = page.getByLabel(t.label, { exact: !!t.exact });
  else if (t.placeholder) l = page.getByPlaceholder(t.placeholder, { exact: !!t.exact });
  else if (t.css || t.selector) l = page.locator(t.css || t.selector);
  else throw new Error('target needs text / role / label / placeholder / css: ' + JSON.stringify(t));
  if (t.has_text) l = l.filter({ hasText: t.has_text });
  l = l.filter({ visible: true });
  return t.nth != null ? l.nth(t.nth) : l.first();
}

async function box(l, timeout = 15000) {
  await l.waitFor({ state: 'visible', timeout });
  const b = await l.boundingBox();
  if (!b) throw new Error('element has no box');
  return b;
}

// bring an element into view with an eased scroll (window or its scrolling ancestor)
async function reveal(rec, l, opts = {}) {
  let b = await box(l);
  const top = opts.offset ?? 120;
  const fits = b.y >= 56 && b.y + Math.min(b.height, VH * 0.6) <= VH - 40;
  if (fits && !opts.force) return b;
  const info = await l.evaluate((el) => {
    let s = el.parentElement;
    while (s && s !== document.body && s !== document.documentElement) {
      const cs = getComputedStyle(s);
      if (/(auto|scroll)/.test(cs.overflowY) && s.scrollHeight > s.clientHeight + 4) break;
      s = s.parentElement;
    }
    if (!s || s === document.body || s === document.documentElement) s = document.scrollingElement || document.documentElement;
    window.__dvScroller = s;
    return { y: s.scrollTop, max: s.scrollHeight - s.clientHeight };
  });
  const want = b.height > VH * 0.6 ? b.y - top : b.y - (opts.offset != null ? top : VH * 0.36);
  const y1 = Math.max(0, Math.min(info.y + want, info.max));
  if (Math.abs(y1 - info.y) > 2) await scrollTween(rec, info.y, y1, opts.sec ?? Math.min(1.2, 0.5 + Math.abs(y1 - info.y) / 1600));
  b = await box(l);
  if (b.y < 0 || b.y > VH - 10) { await l.scrollIntoViewIfNeeded(); b = await box(l); }
  return b;
}

async function scrollTween(rec, y0, y1, sec) {
  await rec.tween(sec, (e) => rec.page.evaluate((y) => {
    const s = window.__dvScroller || document.scrollingElement;
    if (s === document.scrollingElement || s === document.documentElement) window.scrollTo(0, y); else s.scrollTop = y;
  }, y0 + (y1 - y0) * e));
}

async function pickScroller(page) {
  return page.evaluate(() => {
    let s = document.scrollingElement || document.documentElement;
    if (s.scrollHeight <= s.clientHeight + 4) { // app shell with an inner scroller: take the biggest scrollable element
      let best = null, area = 0;
      for (const el of document.querySelectorAll('body *')) {
        const cs = getComputedStyle(el);
        if (!/(auto|scroll)/.test(cs.overflowY) || el.scrollHeight <= el.clientHeight + 4) continue;
        const a = el.clientWidth * el.clientHeight; if (a > area) { area = a; best = el; }
      }
      if (best) s = best;
    }
    window.__dvScroller = s;
    return { y: s.scrollTop, max: s.scrollHeight - s.clientHeight };
  });
}

// ------------------------------------------------------------------ cursor
async function moveTo(rec, x1, y1, sec) {
  if (!rec.cur) rec.cur = [VW * 0.62, VH * 0.78];
  const [x0, y0] = rec.cur;
  const dist = Math.hypot(x1 - x0, y1 - y0);
  if (dist < 1) return;
  sec = sec ?? Math.min(0.9, Math.max(0.45, 0.4 + dist / 1500));
  const arc = Math.min(50, dist * 0.1);
  const px = -(y1 - y0) / dist, py = (x1 - x0) / dist;
  await rec.tween(sec, async (e) => {
    const off = Math.sin(Math.PI * e) * arc;
    rec.cur = [x0 + (x1 - x0) * e + px * off, y0 + (y1 - y0) * e + py * off];
    await rec.page.mouse.move(rec.cur[0], rec.cur[1]);
  });
}

function aim(b, a) {
  return [b.x + b.width * (a.dx ?? 0.5), b.y + b.height * (a.dy ?? 0.5)];
}

// ------------------------------------------------------------------ navigation / waiting
function resolveUrl(u) { return new URL(u, BASE).toString(); }

async function afterLoad(page, ms = 400) {
  try { await page.waitForLoadState('load', { timeout: 20000 }); } catch {}
  try { await page.waitForLoadState('networkidle', { timeout: 3000 }); } catch {}
  try { await page.evaluate(() => document.fonts && document.fonts.ready); } catch {}
  await sleep(ms);
}

let navigated = false;

async function afterInput(rec, a) {
  // let a navigation (if the click/key started one) finish without spending video time
  await sleep(160);
  if (navigated) { navigated = false; rec.ev.navs.push(rec.n); await afterLoad(rec.page, 300); }
  else if (a.wait !== false) { try { await rec.page.waitForLoadState('networkidle', { timeout: a.wait_ms ?? 1500 }); } catch {} }
  rec.tLast = Date.now();
}

// ------------------------------------------------------------------ the DSL
function wordTime(sc, w) {
  const norm = (s) => s.toLowerCase().replace(/[^\p{L}\p{N}'$%.-]/gu, '').replace(/[.]$/, '');
  const want = norm(String(w));
  const hit = (sc.words || []).find((x) => norm(x[0]).includes(want));
  if (!hit) return null;
  return sc.pre + hit[1];
}

const ACTIONS = ['goto', 'wait_for', 'click', 'dblclick', 'type', 'press', 'scroll', 'scroll_by', 'hover', 'focus', 'unfocus', 'pause', 'at', 'eval'];

async function run(rec, sc, a, idx) {
  const page = rec.page;
  const kind = ACTIONS.find((k) => k in a);
  if (!kind) throw new Error(`unknown action (use one of ${ACTIONS.join(', ')})`);
  if (a.at != null && kind !== 'at') await waitAt(rec, sc, a.at);
  switch (kind) {
    case 'at': await waitAt(rec, sc, a.at); break;
    case 'goto': {
      const resp = await page.goto(resolveUrl(a.goto), { waitUntil: 'domcontentloaded', timeout: 45000 });
      if (resp && resp.status() >= 400) rec.ev.warnings.push(`goto ${resolveUrl(a.goto)} answered HTTP ${resp.status()} (wrong path? "/x" is the site root, "x" is relative to "url")`);
      navigated = false;
      rec.ev.navs.push(rec.n);
      await afterLoad(page, a.wait_ms ?? 600);
      if (a.scroll_top !== false) await page.evaluate(() => window.scrollTo(0, 0));
      rec.tLast = Date.now();
      break;
    }
    case 'wait_for': {
      const w = a.wait_for;
      if (typeof w === 'number') await rec.settle(w);
      else if (w === 'load' || w === 'networkidle') await afterLoad(page, 200);
      else await box(locate(page, w), a.timeout_ms ?? 20000);
      if (a.settle_ms) await rec.settle(a.settle_ms);
      rec.tLast = Date.now();
      break;
    }
    case 'pause': await rec.hold(a.pause); break;
    case 'hover': {
      const b = await reveal(rec, locate(page, a.hover), a);
      await moveTo(rec, ...aim(b, a), a.sec);
      await rec.hold(a.hold ?? 0.3);
      break;
    }
    case 'click':
    case 'dblclick': {
      const dbl = kind === 'dblclick';
      const l = locate(page, dbl ? a.dblclick : a.click);
      const b = await reveal(rec, l, a);
      await moveTo(rec, ...aim(b, a), a.sec);
      await rec.hold(0.12);
      const [x, y] = rec.cur;
      rec.ev.clicks.push([rec.n, +x.toFixed(2), +y.toFixed(2), a.zoom !== false, a.zoom && a.zoom !== true ? a.zoom : null]);
      navigated = false;
      if (dbl) {
        await page.mouse.dblclick(x, y);
        await rec.snap();
        rec.ev.clicks.push([rec.n, +x.toFixed(2), +y.toFixed(2), false, null]);
        await rec.snap();
      } else {
        await page.mouse.down();
        await rec.snap();
        await page.mouse.up();
        await rec.snap();
      }
      await afterInput(rec, a);
      await rec.hold(a.hold ?? 0.15);
      break;
    }
    case 'type': {
      if (a.into) {
        const l = locate(page, a.into);
        const b = await reveal(rec, l, a);
        await moveTo(rec, ...aim(b, { dx: 0.25, ...a }), a.sec);
        await rec.hold(0.1);
        const [x, y] = rec.cur;
        rec.ev.clicks.push([rec.n, +x.toFixed(2), +y.toFixed(2), false, null]);
        await page.mouse.click(x, y);
        rec.ev.fields.push([rec.n, +(b.x + b.width / 2).toFixed(1), +(b.y + b.height / 2).toFixed(1), +b.width.toFixed(1)]);
        await rec.snap();
        if (a.clear) await l.fill('');
      }
      else {
        const fb = await page.evaluate(() => { const e = document.activeElement; if (!e || e === document.body) return null;
          const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2, r.width]; });
        if (fb) rec.ev.fields.push([rec.n, ...fb.map((v) => +v.toFixed(1))]);
      }
      const text = String(a.type);
      const cps = a.cps ?? 11;
      let seed = 0; for (const ch of text) seed = (seed * 31 + ch.charCodeAt(0)) >>> 0;
      const rnd = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 2 ** 32; };
      let t = rec.t;
      for (const ch of text) {
        await page.keyboard.type(ch);
        rec.ev.keys.push(rec.n); rec.ev.keys_t.push(+t.toFixed(4));
        let gap = 1 / cps + (rnd() - 0.5) * 0.05;
        if (ch === ' ') gap += 0.03 * rnd();
        t += Math.max(0.05, gap);
        while (rec.t < t - 1e-6) await rec.snap();
      }
      if (a.enter) { await rec.hold(0.25); navigated = false; await page.keyboard.press('Enter'); rec.ev.keys.push(rec.n); rec.ev.keys_t.push(+rec.t.toFixed(4)); await rec.snap(); await afterInput(rec, a); }
      break;
    }
    case 'press': {
      navigated = false;
      await page.keyboard.press(a.press);
      rec.ev.keys.push(rec.n); rec.ev.keys_t.push(+rec.t.toFixed(4));
      await rec.snap();
      await afterInput(rec, a);
      break;
    }
    case 'scroll': {
      const s = a.scroll;
      if (typeof s === 'number') {
        const info = await pickScroller(page);
        const y1 = Math.max(0, Math.min(s, info.max));
        await scrollTween(rec, info.y, y1, a.sec ?? 1.0);
      } else {
        await reveal(rec, locate(page, s), { ...a, force: true, offset: a.offset ?? 120 });
      }
      await rec.hold(a.hold ?? 0.1);
      break;
    }
    case 'scroll_by': {
      const info = await pickScroller(page);
      const y1 = Math.max(0, Math.min(info.y + a.scroll_by, info.max));
      await scrollTween(rec, info.y, y1, a.sec ?? Math.min(1.4, 0.6 + Math.abs(a.scroll_by) / 1400));
      await rec.hold(a.hold ?? 0.1);
      break;
    }
    case 'focus': {
      let cx, cy;
      if (Array.isArray(a.focus)) [cx, cy] = a.focus;
      else { const b = await reveal(rec, locate(page, a.focus), a); [cx, cy] = [b.x + b.width * (a.fx ?? 0.5), b.y + b.height * (a.fy ?? 0.5)]; }
      const z = Math.min(2.0, Math.max(1.0, a.zoom ?? 1.6));
      rec.ev.focus.push({ f: rec.n, cx: +cx.toFixed(1), cy: +cy.toFixed(1), z, until: a.for != null ? rec.n + Math.round(a.for * FPS) : null });
      if (a.move !== false && a.cursor !== false && rec.cur) {} // cursor stays where it is
      break;
    }
    case 'unfocus': {
      const f = rec.ev.focus[rec.ev.focus.length - 1];
      if (f && f.until == null) f.until = rec.n;
      break;
    }
    case 'eval': { await page.evaluate(a.eval); await rec.settle(a.settle_ms ?? 200); break; }
  }
}

async function waitAt(rec, sc, at) {
  let t = typeof at === 'number' ? at : wordTime(sc, at);
  if (t == null) { rec.ev.warnings.push(`"at": word ${JSON.stringify(at)} is not in this scene's voice line`); return; }
  if (typeof at !== 'number') t -= 0.15; // act a moment before the word lands
  if (rec.t > t + 0.05) rec.ev.warnings.push(`"at": ${JSON.stringify(at)} wanted at ${t.toFixed(2)}s, actions only got there at ${rec.t.toFixed(2)}s`);
  await rec.until(t);
}

async function candidates(page) {
  try {
    return await page.evaluate(() => [...document.querySelectorAll('a, button, input, textarea, select, [role=button], [role=tab], [role=link]')]
      .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4 && getComputedStyle(e).visibility !== 'hidden'; })
      .slice(0, 60).map((e) => {
        const t = (e.innerText || e.value || e.getAttribute('aria-label') || e.placeholder || '').trim().replace(/\s+/g, ' ').slice(0, 50);
        return `${e.tagName.toLowerCase()}${e.id ? '#' + e.id : ''}${e.name ? `[name=${e.name}]` : ''} ${t ? JSON.stringify(t) : ''}`.trim();
      }));
  } catch { return []; }
}

// ------------------------------------------------------------------ main
async function launch() {
  const args = ['--force-color-profile=srgb', '--hide-scrollbars', '--lang=' + (plan.locale || 'en-US'), '--disable-features=Translate'];
  const tries = [{}, { channel: 'chrome' }, { channel: 'msedge' }];
  let err;
  for (const t of tries) {
    try { return await chromium.launch({ headless: true, args, ...t }); } catch (e) { err = e; }
  }
  console.error('ERROR: no Chromium found. Run: npx playwright install chromium   (in ' + path.dirname(HERE) + ')');
  console.error(String(err).split('\n')[0]);
  process.exit(3);
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await launch();
  const ctx = await browser.newContext({
    viewport: { width: VW, height: VH }, deviceScaleFactor: DRY ? 1 : SCALE, locale: plan.locale || 'en-US',
    timezoneId: plan.timezone || undefined, colorScheme: plan.color_scheme || 'light', reducedMotion: 'no-preference',
    userAgent: plan.user_agent || undefined, ignoreHTTPSErrors: false,
  });
  await ctx.addInitScript(INIT_JS);
  const page = await ctx.newPage();
  page.on('framenavigated', (f) => { if (f === page.mainFrame()) navigated = true; });
  page.setDefaultTimeout(20000);
  const cdp = await ctx.newCDPSession(page);

  // brand + site facts (also warms caches)
  let brand = { color: null };
  try {
    await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await afterLoad(page, 300);
    brand = await page.evaluate(BRAND_JS);
  } catch (e) { log('WARNING: could not open ' + BASE + ': ' + String(e).split('\n')[0]); }
  if (plan.brand && plan.brand !== 'auto') brand.color = plan.brand;
  fs.writeFileSync(path.join(BUILD, 'brand.json'), JSON.stringify(brand, null, 1));
  if (plan.brand_only) { await browser.close(); return; }

  const report = { scenes: [], ok: true };
  let cursor = null;
  for (const sc of plan.scenes) {
    const t0 = Date.now();
    const rec = new Rec(page, cdp, path.join(OUT, sc.id), sc.dur);
    rec.cur = cursor;
    await rec.start();
    let error = null;
    try {
      if (sc.card) {
        rec.cur = null;
        const card = { brand: brand.color || '#4f46e5', icon: brand.icon, title: brand.site, subtitle: brand.description, ...sc.card };
        await page.goto(pathToFileURL(path.join(HERE, 'card.html')).toString(), { waitUntil: 'load' });
        await page.evaluate((d) => window.mount(d), card);
        await afterLoad(page, 300);
        await rec.hold(0.05);
      }
      for (const [i, a] of (sc.actions || []).entries()) {
        try {
          await run(rec, sc, a, i);
          if (DRY && plan.every) {
            fs.mkdirSync(path.join(BUILD, 'screens'), { recursive: true });
            await sleep(400);
            try { await page.screenshot({ path: path.join(BUILD, 'screens', `${sc.id}_${String(i).padStart(2, '0')}.png`) }); } catch {}
          }
        } catch (e) {
          error = { action: i, spec: a, message: String(e.message || e).split('\n')[0] };
          throw e;
        }
      }
    } catch (e) {
      if (!error) error = { action: null, message: String(e.message || e).split('\n')[0] };
      fs.mkdirSync(path.join(BUILD, 'errors'), { recursive: true });
      const shot = path.join(BUILD, 'errors', `${sc.id}.png`);
      try { await page.screenshot({ path: shot }); } catch {}
      error.screenshot = shot;
      error.url = page.url();
      error.clickable = await candidates(page);
    }
    const tail = sc.card ? 0 : 0.35;
    const actT = rec.t;
    const ev = await rec.finish(Math.max(sc.dur, rec.t + tail));
    cursor = sc.card ? cursor : rec.cur;
    if (DRY) {
      fs.mkdirSync(path.join(BUILD, 'screens'), { recursive: true });
      try { await page.screenshot({ path: path.join(BUILD, 'screens', `${sc.id}.png`) }); } catch {}
    }
    const used = (ev.frames / FPS);
    const r = { id: sc.id, planned: sc.dur, actual: +used.toFixed(2), action_time: +actT.toFixed(2), overrun: ev.overrun, warnings: ev.warnings, error,
                wall_s: +((Date.now() - t0) / 1000).toFixed(1) };
    report.scenes.push(r);
    log(`${DRY ? 'tried' : 'recorded'} ${sc.id}: ${used.toFixed(2)}s video (planned ${sc.dur.toFixed(2)}s)${ev.overrun ? `, actions ran ${ev.overrun.toFixed(2)}s longer than the voice line` : ''} in ${r.wall_s}s`);
    for (const w of ev.warnings) log(`  WARNING ${sc.id}: ${w}`);
    if (error) {
      report.ok = false;
      log(`  ERROR ${sc.id} action #${error.action}: ${JSON.stringify(error.spec || {})}: ${error.message}`);
      log(`  page: ${error.url}  screenshot: ${error.screenshot}`);
      log(`  visible clickable elements: ${error.clickable.slice(0, 25).join(' | ')}`);
      break;
    }
  }
  fs.writeFileSync(path.join(BUILD, DRY ? 'try_report.json' : 'record_report.json'), JSON.stringify(report, null, 1));
  await browser.close();
  process.exit(report.ok ? 0 : 2);
}

main().catch((e) => { console.error('ERROR', e); process.exit(1); });
