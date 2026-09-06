# claude-work

A collection of self-contained single-file HTML "mini-apps." Each app is one static `index.html` with inline CSS and JS — no build system, package manager, bundler, or test suite. Just open the file in a browser.

## Apps

| App | Description |
| --- | --- |
| [`mini-apps/focus-ledger`](mini-apps/focus-ledger/index.html) | Pomodoro timer + task ledger |
| [`mini-apps/mirror-bloom`](mini-apps/mirror-bloom/index.html) | Kaleidoscope drawing toy (canvas) |
| [`mini-apps/nova-fold`](mini-apps/nova-fold/index.html) | 2048-style tile-merging game |

## Running an app

There's no dev server or build step — open the file directly, e.g. on Windows:

```
start mini-apps/focus-ledger/index.html
```

To verify a change, open the file in a browser and exercise it manually.

## Conventions

Every app follows the same shape: no `<html>`/`<head>`/`<body>` wrapper, just `<!doctype html>`, `<title>`, `<meta charset>`, a Google Fonts `<link>`, one `<style>` block, the body markup, and one `<script>` block at the end. All logic runs client-side (vanilla JS, ES5-leaning syntax, no framework); apps that persist state use `localStorage`. No network calls beyond the Google Fonts stylesheet — everything else works fully offline.

See [`CLAUDE.md`](CLAUDE.md) for the full set of shared conventions (theming, fonts, persistence, accessibility/motion) followed across apps.
