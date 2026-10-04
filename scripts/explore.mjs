// Look at one page the way the recorder will see it (1280x720, light mode, en-US) and print what a demo script needs:
// title, description, headings, visible copy, forms, clickable elements with selectors you can paste into
// scenes.json, internal links, brand colour, and what robots.txt says about the path.
//
//   node explore.mjs <url> <outdir>      -> <outdir>/viewport.png, full.png, report.json (+ a summary on stdout)

import fs from 'node:fs';
import path from 'node:path';

let chromium;
try { ({ chromium } = await import('playwright')); } catch { ({ chromium } = await import('playwright-core')); }

const [url, outdir] = process.argv.slice(2);
if (!url) { console.error('usage: node explore.mjs <url> <outdir>'); process.exit(2); }
fs.mkdirSync(outdir, { recursive: true });

async function robots(u) {
  try {
    const base = new URL(u);
    const r = await fetch(new URL('/robots.txt', base), { signal: AbortSignal.timeout(10000) });
    if (!r.ok) return { found: false, allowed: true };
    const txt = await r.text();
    let applies = false, rules = [];
    for (const raw of txt.split('\n')) {
      const line = raw.replace(/#.*/, '').trim();
      const m = line.match(/^(user-agent|disallow|allow)\s*:\s*(.*)$/i);
      if (!m) continue;
      const k = m[1].toLowerCase(), v = m[2].trim();
      if (k === 'user-agent') applies = v === '*';
      else if (applies && v) rules.push([k, v]);
    }
    const p = base.pathname || '/';
    const match = rules.filter(([, v]) => p.startsWith(v.replace(/\*$/, ''))).sort((a, b) => b[1].length - a[1].length)[0];
    return { found: true, allowed: !match || match[0] === 'allow', rule: match ? match.join(': ') : null };
  } catch (e) { return { found: false, allowed: true, error: String(e) }; }
}

let browser;
for (const t of [{}, { channel: 'chrome' }]) { try { browser = await chromium.launch({ headless: true, ...t }); break; } catch {} }
if (!browser) { console.error('ERROR: no Chromium. Run: npx playwright install chromium'); process.exit(3); }
const ctx = await browser.newContext({ viewport: { width: 1280, height: 720 }, locale: 'en-US', colorScheme: 'light' });
const page = await ctx.newPage();
await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
try { await page.waitForLoadState('networkidle', { timeout: 6000 }); } catch {}
await page.waitForTimeout(800);
await page.screenshot({ path: path.join(outdir, 'viewport.png') });
await page.screenshot({ path: path.join(outdir, 'full.png'), fullPage: true });

const info = await page.evaluate(() => {
  const vis = (e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 3 && r.height > 3 && cs.visibility !== 'hidden' && cs.display !== 'none' && +cs.opacity > 0.05; };
  const txt = (e) => (e.innerText || e.value || e.getAttribute('aria-label') || e.getAttribute('title') || e.placeholder || '').trim().replace(/\s+/g, ' ');
  const sel = (e) => {
    if (e.id && /^[A-Za-z][\w-]*$/.test(e.id) && document.querySelectorAll('#' + e.id).length === 1) return '#' + e.id;
    for (const a of ['data-testid', 'data-test', 'data-cy', 'name', 'aria-label', 'placeholder']) {
      const v = e.getAttribute(a);
      if (v && v.length < 60) { const s = `${e.tagName.toLowerCase()}[${a}="${v.replace(/"/g, '\\"')}"]`; try { if (document.querySelectorAll(s).length === 1) return s; } catch {} }
    }
    const t = txt(e);
    if (t && t.length <= 40) return { text: t };
    return null;
  };
  const els = [...document.querySelectorAll('a, button, input, textarea, select, [role=button], [role=tab], [role=link], [role=menuitem], summary, [contenteditable=true]')]
    .filter(vis).slice(0, 120).map((e) => {
      const r = e.getBoundingClientRect();
      return { tag: e.tagName.toLowerCase(), type: e.type || undefined, text: txt(e).slice(0, 70), selector: sel(e),
               href: e.tagName === 'A' ? e.getAttribute('href') : undefined, y: Math.round(r.y + scrollY), in_first_screen: r.y < innerHeight };
    });
  const heads = [...document.querySelectorAll('h1, h2, h3')].filter(vis).map((h) => `${h.tagName}: ${txt(h).slice(0, 120)}`).slice(0, 40);
  const links = [...new Set([...document.querySelectorAll('a[href]')].map((a) => a.href).filter((h) => h.startsWith(location.origin)))].slice(0, 60);
  const og = (p) => (document.querySelector(`meta[property="${p}"]`) || {}).content || '';
  return { url: location.href, title: document.title, description: (document.querySelector('meta[name="description"]') || {}).content || og('og:description'),
           headings: heads, copy: document.body.innerText.replace(/\n{2,}/g, '\n').slice(0, 6000), elements: els, internal_links: links,
           page_height: document.documentElement.scrollHeight,
           inner_scroller: document.documentElement.scrollHeight <= innerHeight + 4 };
});
info.robots = await robots(url);
fs.writeFileSync(path.join(outdir, 'report.json'), JSON.stringify(info, null, 1));

const o = [];
o.push(`# ${info.title}\n${info.url}\n${info.description || ''}`);
o.push(`robots.txt: ${info.robots.found ? (info.robots.allowed ? 'allows this path' : 'DISALLOWS this path (' + info.robots.rule + ')') : 'none'}`);
o.push(`page height ${info.page_height}px${info.inner_scroller ? ' (window does not scroll: app shell with an inner scroller)' : ''}`);
o.push('\n## Headings\n' + info.headings.join('\n'));
o.push('\n## Clickable / inputs (selector -> paste into scenes.json)');
for (const e of info.elements) o.push(`- ${e.tag}${e.type && e.tag === 'input' ? '[' + e.type + ']' : ''} ${JSON.stringify(e.selector)} ${e.text ? '"' + e.text + '"' : ''}${e.href ? ' -> ' + e.href : ''} @y=${e.y}`);
o.push('\n## Internal links\n' + info.internal_links.join('\n'));
o.push('\n## Visible copy (first 6000 chars)\n' + info.copy);
o.push(`\nScreenshots: ${path.join(outdir, 'viewport.png')} (first screen), ${path.join(outdir, 'full.png')} (whole page)`);
console.log(o.join('\n'));
await browser.close();
