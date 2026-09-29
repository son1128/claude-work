# claude-work

A collection of self-contained single-file HTML "mini-apps." Each app is one static `index.html` with inline CSS and JS — no build system, package manager, bundler, or test suite. Just open the file in a browser.

## Apps

| App | Description |
| --- | --- |
| [`mini-apps/focus-ledger`](mini-apps/focus-ledger/index.html) | Pomodoro timer + task ledger |
| [`mini-apps/mirror-bloom`](mini-apps/mirror-bloom/index.html) | Kaleidoscope drawing toy (canvas) |
| [`mini-apps/nova-fold`](mini-apps/nova-fold/index.html) | 2048-style tile-merging game |
| [`mini-apps/cascade-desk`](mini-apps/cascade-desk/index.html) | Judge-cascade simulator: a research desk where a cheap judge decides every fork and a frontier model only takes the unsure ones |

## Running an app

There's no dev server or build step — open the file directly, e.g. on Windows:

```
start mini-apps/focus-ledger/index.html
```

To verify a change, open the file in a browser and exercise it manually.

## Skills

`.claude/skills/judge-cascade/` is a Claude Code skill for designing, tuning and auditing "cheap judge first, frontier model only when unsure" LLM pipelines. It includes:

- `SKILL.md`: the procedure (map forks, measure calibration, pick τ with the full cost in view, audit third-party claims)
- `references/cost-and-calibration.md`: the escalation, cost and calibration formulas
- `scripts/threshold_sweep.py`: reliability table plus threshold sweep from a labeled CSV (standard library only; try `--demo`)

`mini-apps/cascade-desk` is the worked example the skill points to. The Python ports below don't cover it.

## Python ports

`test-code/` holds standalone Python/Tkinter ports of the three apps above — same logic, translated line-for-line, with no shared code between the two versions:

| App | Script |
| --- | --- |
| focus-ledger | [`test-code/focus_ledger.py`](test-code/focus_ledger.py) |
| mirror-bloom | [`test-code/mirror_bloom.py`](test-code/mirror_bloom.py) |
| nova-fold | [`test-code/nova_fold.py`](test-code/nova_fold.py) |

Each needs only the Python standard library (`tkinter`) except `mirror_bloom.py`, which uses [Pillow](https://pypi.org/project/Pillow/) to save drawings as PNG. Run with e.g. `python test-code/focus_ledger.py`. These are a separate, parallel implementation — changes to the HTML apps are not automatically reflected here and vice versa.

## Conventions

Every app follows the same shape: no `<html>`/`<head>`/`<body>` wrapper, just `<!doctype html>`, `<title>`, `<meta charset>`, a Google Fonts `<link>`, one `<style>` block, the body markup, and one `<script>` block at the end. All logic runs client-side (vanilla JS, ES5-leaning syntax, no framework); apps that persist state use `localStorage`. No network calls beyond the Google Fonts stylesheet — everything else works fully offline.

See [`CLAUDE.md`](CLAUDE.md) for the full set of shared conventions (theming, fonts, persistence, accessibility/motion) followed across apps.
