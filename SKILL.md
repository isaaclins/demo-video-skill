---
name: demo-video
description: Turn a website URL into a polished product demo video (60 seconds or less, 1920x1080 H.264/AAC mp4) in the Screen Studio style, with smooth zooms, a big smoothed cursor, click ripples, captions, an optional ElevenLabs voice-over and a quiet music bed. Use it when the user asks to "make a demo video of <url>", "record a product demo / walkthrough / promo video of my site", "turn my landing page or web app into a video", or wants a short screencast of a live website. It makes product demo videos only, not team or technical videos.
---

# demo-video: website URL -> product demo video

You turn a live website into a demo video of 60 seconds or less. The site is driven by a real browser (Playwright),
captured frame by frame at 2560x1440, then composited: rounded window on a soft gradient wallpaper, eased zoom and
pan, a large smoothed cursor, click ripples, burned-in captions, a progress rail, a synthesized click and typing sound,
an optional quiet music bed and an ElevenLabs voice. You write one `scenes.json` and the build does the rest.

`$SKILL` below means this skill's folder (the "Base directory for this skill" shown when it loads). Every command
goes through `"$SKILL/dv"`.

## 0. Setup check (once)

Run `"$SKILL/dv" --help`. It needs **Node 18+**, **ffmpeg/ffprobe**, **uv** (Python is installed by uv). On the first
run it does `npm install` in `$SKILL` by itself. If the recorder reports "no Chromium found", run
`cd "$SKILL" && npx playwright install chromium` (about 150 MB; an installed Google Chrome also works). If a tool
is missing, tell the user the one command for their OS (macOS: `brew install node ffmpeg uv`), do not work around it.

Voice: if `ELEVENLABS_API_KEY` is set in the environment, the final build is narrated by ElevenLabs (a calm,
confident stock voice, Sarah by default, and the best TTS model the API lists). If it is not set, the build is
captions-only (no voice) and says so. Never print, echo or write the key anywhere.

## 1. Explore the site and find the one flow

Make a working folder in the current directory: `demo-video/<site-name>/` (for example `demo-video/acme/`).

1. `"$SKILL/dv" explore <url> --out demo-video/<site>/explore` prints the title, description, headings, the
   visible copy, every clickable element and input with a selector you can paste, internal links and what
   robots.txt says. It saves `viewport.png` (first screen) and `full.png` (whole page): **look at both.**
   Explore 2-4 more pages the same way (pricing, the app, a product page) if the home page is only marketing.
   To see how the site reacts to clicks and typing, write a scratch `scenes.json` with those actions and run
   `"$SKILL/dv" try scratch.json --every`: it saves a screenshot after every action (`build/try/screens/`).
2. Read the copy to get the **value proposition** in the site's own words: who it is for, the problem, the promise.
3. Pick **one core flow** that shows the "aha" in under 60 seconds: the thing a visitor would want to see working
   (search -> result, configure -> price, upload -> output, pick -> compare). A demo is a story with one thread,
   not a tour of every menu. Prefer flows that work without an account.
4. Ask the user **only** for what you truly cannot infer: login details for a demo account if the core flow is
   behind a login, or which flow to show if there are several equally good ones. Otherwise decide yourself.
5. **Respect the site**: if robots.txt disallows the pages you need or the terms forbid automation, stop and tell
   the user (unless it is clearly their own site). Visit like a normal user: no load tests, no form spam, no
   purchases, no submitting real data. Never record a third party's private data.

**Privacy:** the video shows whatever the browser shows. Use a demo account and demo data, never real customer data,
real names, emails or keys. If the flow needs a login, tell the user this before recording.

## 2. Write the script (5-8 scenes)

- 5-8 short scenes, one idea each. About **15 characters per second** of speech: a 4-second scene has about
  60 characters of voice. The whole script: **550-750 characters** for a 45-57 s video (scenes also need time for their actions). Hard limit 60 s.
- Structure: hook (the problem or promise, while the first screen shows), 3-5 scenes of the flow (each line says
  what the viewer sees happen and why it matters), close (product name + promise, on the end card).
- **Every number, name and claim must come from what the site really shows** (copy, results on screen). Never invent
  customers, stats, prices or features. If you say "in 3 seconds", the site must say or show it.
- Plain spoken English, short sentences, no jargon, no "Welcome to". Write numbers the way they should be spoken
  when it is ambiguous ("2,400 dollars" is safe; "$2.4k" is not).
- The line plays while the scene's actions run, so actions must fit the line. Use `"at": "<word>"` to sync a
  click with the word that names it.

## 3. Write scenes.json

Put it at `demo-video/<site>/scenes.json`. Full reference:

```json
{
  "url": "https://example.com/",
  "brand": "auto",
  "hide": ["#cookie-banner", ".intercom-launcher"],
  "scenes": [
    {
      "id": "hook",
      "voice": "Planning a trip? Type where you want to go, and the whole plan is ready.",
      "min": 5.0,
      "actions": [
        {"goto": "/"},
        {"wait_for": "#search"},
        {"pause": 0.6},
        {"type": "Lisbon in May", "into": "#search"},
        {"press": "Enter", "at": "ready"},
        {"wait_for": {"text": "Your plan"}},
        {"focus": ".results", "zoom": 1.5}
      ]
    },
    {
      "id": "details",
      "voice": "Open any day to see the route, the times and the prices.",
      "actions": [
        {"unfocus": true},
        {"scroll": ".day-2"},
        {"click": {"role": "button", "name": "Day 2"}, "at": "route"},
        {"hover": {"text": "Total"}}
      ]
    },
    {
      "id": "end",
      "voice": "Example. Your next trip, planned in seconds.",
      "card": {"title": "Example", "subtitle": "Your next trip, planned in seconds.", "url": "example.com"}
    }
  ]
}
```

Top level: `url` (required; `goto` paths resolve against it like links do: `"/pricing"` is the site root +
/pricing, `"pricing"` is relative to `url`, `"."` or `""` is `url` itself), `brand` (`"auto"` = taken from the site's
CSS: theme-color, CSS variables, button colours; or a hex like `"#0f62fe"`), `hide` (CSS selectors hidden in every
scene: cookie banners, chat widgets, promo bars), optional `css` (extra CSS injected into every page), `locale`
(default `en-US`), `timezone`, `color_scheme` (`light`/`dark`), `voice` (`{"voice_id": "...", "model": "..."}`
to override the ElevenLabs choice), `music` (`false` to drop the music bed), `camera` (see below).

Per scene: `id` (lowercase, unique), `voice` (the spoken line; also the captions), `min` (minimum length in
seconds), optional `pre` (silence before the line, default 0.4 s first scene / 0.2 s), `post` (after, default
0.35 s / 1.0 s last), `actions` (list), `card` (instead of a page: title/end card with `title`, `subtitle`, `url`,
optional `cta`, `icon` (image URL or `false`); defaults come from the site's name, description and icon),
`camera` (per-scene camera settings).

Targets (for click, hover, focus, scroll, wait_for, type `into`): a CSS or Playwright selector string
(`"#search"`, `"button.primary"`, `"text=Get started"`, `"nav >> text=Pricing"`), or an object:
`{"text": "Get started"}`, `{"role": "button", "name": "Sign up"}`, `{"label": "Email"}`,
`{"placeholder": "Search"}`, `{"css": "li.result", "has_text": "Lisbon", "nth": 0}`. `"exact": true` for exact
text. Only visible elements match. Prefer ids and roles over long CSS paths.

Actions (one per object; any action can also carry `"at"`):

| Action | What it does |
|---|---|
| `{"goto": "/path"}` | load a page (waits for load; costs no video time) |
| `{"wait_for": target}` | wait until the element is visible (no video time passes; content just appears). Also `{"wait_for": 1500}` (ms) or `"networkidle"`. Use after anything that loads data. |
| `{"click": target}` | eased cursor move on an arc, press, ripple + click sound; waits for navigation. Options: `"zoom": false` (no auto zoom) or a number, `"dx"/"dy"` (0-1, where in the box), `"sec"` (move time), `"hold"` (s after) |
| `{"dblclick": target}` | the same with a double click (e.g. "double-click to edit") |
| `{"type": "text", "into": target}` | human typing (about 11 keys/s, seeded jitter, soft key sounds). `into` is optional (clicks the field first), `"clear": true` empties it, `"enter": true` presses Enter after, `"cps"` sets the speed |
| `{"press": "Enter"}` | a key (Playwright key names: `Tab`, `ArrowDown`, `Escape`, `Meta+K`) |
| `{"scroll": target}` | eased scroll until the element is near the top (`"offset"` px, default 120). `{"scroll": 0}` = to an absolute position. `{"scroll_by": 600}` = relative. Works with inner scrolling containers too |
| `{"hover": target}` | move the cursor there and rest (`"hold"`, default 0.3 s) |
| `{"focus": target, "zoom": 1.6}` | zoom the camera to the element (1.4-1.8 looks best) until `unfocus`, the scene end, or `"for": seconds`. `"fx"/"fy"` pick the point inside the box |
| `{"unfocus": true}` | zoom back out |
| `{"pause": 0.8}` | hold still for that many seconds of video |
| `{"at": "word"}` | wait until that word of this scene's line is spoken (or `"at": 2.5` seconds into the scene) |
| `{"eval": "js"}` | run JavaScript in the page (last resort, e.g. to close a stubborn popup) |

Camera: clicks zoom in automatically (1.5x), typing zooms to the field (1.6x), eased in 0.8 s and out 0.9 s, pans
0.6 s; nearby events are bridged so the camera pans instead of pumping. Tune globally or per scene with
`"camera": {"click_zoom": 1.0, "type_zoom": 1.4, "max_zoom": 1.8}` (1.0 = off). Calm is better than busy: on
dense pages turn click zoom off and use one deliberate `focus` per scene.

Small or narrow apps (a centred column with lots of empty space) read badly at 1080p: scale the page up with
`"css": "body{zoom:1.3}"` instead of zooming the camera all the time.

Timing rules: a scene lasts as long as its voice line (plus `pre`/`post`) or `min`, whichever is longer. If the
actions need longer, the scene is extended (voice stays in sync, the video gets longer). Waiting for pages and data
costs no video time. Open each scene in a defined state (a `goto`, or continue from the last scene on purpose).

## 4. Build

Work in this order; each step is one command, run from the folder that holds `scenes.json`:

1. `"$SKILL/dv" try scenes.json`: runs every action quickly without recording (about 10-30 s). It reports broken
   selectors (with a screenshot in `build/errors/` and a list of the clickable elements on that page) and a table of
   voice length vs action length per scene. Look at `build/try/screens/<scene>.png` (end state of each scene;
   `--every` adds one per action). Fix
   until it runs clean and the estimated length is under 58 s.
2. `"$SKILL/dv" build scenes.json --draft`: silent draft (no voice spend, low-res capture, quick encode,
   2-4 min) -> `out/draft.mp4`, `out/draft_sheet.png`, `out/draft_scenes.png`.
3. `"$SKILL/dv" build scenes.json`: the final -> `out/demo.mp4` (5-10 min for 60 s; recording runs about 2.5x
   slower than real time, compositing is the slow part). With a key, only new or changed lines are sent to
   ElevenLabs; unchanged lines come from `cache/tts/` for free. Add `--stt` to also transcribe the result with
   faster-whisper (installs it on first use; downloads a speech model).

Other flags: `--no-voice` (captions only even with a key), `--local-tts` (no key: use macOS `say` or `piper` if
present; lower quality), `--no-music`, `--no-sfx`, `--no-captions`, `--no-rail`, `--jobs N` (parallel compositing,
default 2), `--tts-budget N` (max ElevenLabs characters for this project, default 4000), `--name NAME`.

## 5. Check, look, fix, rebuild

The build runs the checks itself (`"$SKILL/dv" check out/demo.mp4` re-runs them): duration <= 60 s, H.264
1920x1080 30 fps + AAC 48 kHz, loudness about -16 LUFS, captions in sync with the voice, no voice line cut at a scene
change, frozen or blank scenes, recorder warnings (e.g. an `at` word reached too late). A FAIL means fix and rebuild.

Then **look at `out/demo_scenes.png` and `out/demo_sheet.png` yourself** (read the images) and judge them like a
viewer:
- Does every scene show what its line says, at the moment it says it? Is the result of the flow visible?
- Anything ugly: cookie banner, chat bubble, empty state, spinner, error, 404, half-loaded images, a modal in the
  way? -> `hide`, `wait_for`, or a different path.
- Zoom on the right thing, not on empty space or cut-off text? Text readable?
- Any real customer data or anything private on screen? -> stop and use demo data.

Fix `scenes.json` and rebuild until it is clean. A problem you can fix by changing only actions, camera, `hide` or
`css` costs no voice characters: fix it yourself and rebuild, do not just mention it to the user. Change voice lines
only when needed: every changed line is a new paid take. Typical: 1-2 draft rounds, 1 final, maybe 1 fix.

## 6. Hand over

Tell the user: the path of `out/demo.mp4`, its length, whether it has a voice (and the ElevenLabs characters used,
printed by the build) or is captions-only, and the scene list in one line each. If there is no key, say clearly:
"This version has no voice-over; set ELEVENLABS_API_KEY and run the same build again for a narrated one." Offer one
concrete improvement if you see one. Do not upload or post the video anywhere.

## Limits

- Public pages work best. Logins work only with credentials the user gives you (type them with `type`; they then
  appear in the video, so use a demo account). No CAPTCHAs, no 2FA, no payments.
- Desktop layout only (1280x720 viewport). Content the site animates with JavaScript plays faster than CSS
  animations during capture; heavy WebGL/video backgrounds may stutter.
- Sites that block headless browsers may show a challenge page: tell the user, do not try to evade it.
- English voice by default; for another language write the lines in that language (the default model is
  multilingual) and set `"locale"`.
