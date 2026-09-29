# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A collection of self-contained single-file HTML "mini-apps" under `mini-apps/<app-name>/index.html`. There is no build system, package manager, bundler, linter, or test suite — each app is one static HTML file with inline `<style>` and `<script>`, opened directly in a browser.

Current apps:
- `mini-apps/focus-ledger/` — Pomodoro timer + task ledger
- `mini-apps/mirror-bloom/` — kaleidoscope drawing toy (canvas)
- `mini-apps/nova-fold/` — 2048-style tile-merging game
- `mini-apps/cascade-desk/` — judge-cascade simulator (cheap judge + frontier escalation research desk). It's the worked example for the `.claude/skills/judge-cascade` skill; its model constants live in one `MODEL` object, and it must keep its "simulation, not measurements" notice visible

## Working with an app

There is no dev server or build step. Open the file directly, e.g.:

```
start mini-apps/focus-ledger/index.html   # Windows
```

To verify a change works, open the file in a browser and exercise it manually (or use the `run` skill / `claude-in-chrome` to launch and click through it) — there are no automated tests to run instead.

## Conventions shared across every mini-app

Each `index.html` is fully self-contained: no `<html>`/`<head>`/`<body>` wrapper tags, just `<!doctype html>`, `<title>`, `<meta charset>`, a Google Fonts `<link>`, one `<style>` block, the body markup, and one `<script>` block at the end. Follow this shape for new apps or edits — don't split into separate CSS/JS files or add a bundler.

**Theming**: colors are CSS custom properties on `:root`. `focus-ledger`, `nova-fold` and `cascade-desk` define a dark palette twice, so both an OS-level preference and a possible `data-theme="dark"` override work:
```css
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){ /* dark values */ } }
:root[data-theme="dark"]{ /* same dark values */ }
```
No app currently ships a UI control that sets `data-theme` — this is CSS scaffolding for a future toggle, not a wired feature. Follow the same duplicated-block pattern in new apps rather than relying on the media query alone. `mirror-bloom` is the one exception: it's a fixed single-palette dark UI (a kaleidoscope "chamber") with no light variant at all — don't assume every app needs the light/dark split.

**Fonts**: each app preconnects to `fonts.googleapis.com` and pulls 2-3 Google Fonts (a display face for headings, a body sans, and a monospace for numbers/stats) — the only external dependency any app has.

**JavaScript style**: vanilla JS, no framework, no imports. Everything wrapped in a single `(function(){ "use strict"; ... })()` IIFE at the bottom of the file. ES5-leaning syntax (`var`, `function` expressions rather than arrow functions/`class`) is used consistently — match it rather than introducing modern syntax mid-file.

**Persistence**: apps that need to remember state use `localStorage` directly, always guarded with try/catch (private browsing / disabled storage should degrade silently, not throw):
```js
function load(key, fallback){ try{ var v = JSON.parse(localStorage.getItem(key)); return v || fallback; }catch(e){ return fallback; } }
function save(){ try{ localStorage.setItem(key, JSON.stringify(state)); }catch(e){} }
```

**Accessibility/motion**: interactive controls set `aria-pressed`/`aria-label` as appropriate, and apps with CSS transitions/animations include a `@media (prefers-reduced-motion: reduce)` block that disables them. `mirror-bloom` has no such block because it has no CSS transitions/animations to disable (its canvas drawing loop isn't CSS-driven) — its starfield-equivalent (in `nova-fold`) checks `matchMedia("(prefers-reduced-motion: reduce)")` in JS instead and freezes the animation loop on a single static frame.

**No network calls** beyond the Google Fonts stylesheet — apps must keep working fully offline otherwise (all logic and state is local).
