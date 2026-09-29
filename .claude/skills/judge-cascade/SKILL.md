---
name: judge-cascade
description: Design, tune, or audit a "cheap judge first, frontier model only when unsure" LLM pipeline (judge cascade / model routing / confidence-gated escalation) — e.g. a research bot that screens, routes, verifies, dedupes and ranks sources before an expensive model ever sees them. Use when the user wants to cut LLM cost on classification-style decisions, pick an escalation threshold, check whether a judge's confidence is calibrated, sanity-check cost/accuracy claims like "99% of accuracy at half the fee", or build/extend the `mini-apps/cascade-desk` simulator. Korean triggers: "저가 판정 모델", "캐스케이드", "에스컬레이션 임계값", "LLM 비용 절감", "리서치 자동화 봇", "JEV", "판정기 보정".
---

# Judge cascade

A judge cascade splits an LLM pipeline into two kinds of work:

- **Forks**: narrow decisions with a small answer space. Read or skip? Which desk? Does the claim hold? Is it new or a duplicate? Does it matter to the user? A small, cheap judge answers these and reports a confidence.
- **Reads and writes**: open-ended understanding and generation. A general LLM does these.

When the cheap judge's confidence is below a threshold τ, the fork is **escalated** to a frontier model. Everything else is accepted as-is. The method is only as good as the cheap judge's **calibration**: if it is confidently wrong, nothing gets escalated and the errors pass straight through.

The worked example in this repo is `mini-apps/cascade-desk/index.html`, a single-file simulator of a research desk built on this pattern. Open it to see every number below move with the threshold.

## Procedure

Do these in order. Don't quote savings before step 4.

1. **Map the forks.** List every decision in the pipeline. For each, record its type and what a wrong answer costs downstream:

   | Type | Answer space | Example | Typical cost of an error |
   |---|---|---|---|
   | BOOL | yes / no | "worth reading?", "claim holds?" | a false skip loses a source for good; a false keep only costs a read |
   | CHOICE | one of N labels | "which desk?", "new / related / revises / duplicate?" | a wrong label is usually recoverable |
   | SCORE | a number compared to a cut-off | "matters to you?" rerank | depends on how close the item is to the cut-off |

   Errors are asymmetric. A false "skip" at the screen is unrecoverable, while a false "keep" just spends one read. Give such forks their own τ, or bias the judge toward the cheap-to-fix side. Don't use one global threshold without checking this.

2. **Keep reads and writes off the judge.** Summarising, drafting notes and writing the brief stay with the LLM. The cascade saves money on *decisions*, not on reading. Account for that cost separately (step 4).

3. **Measure calibration on labeled data.** Collect a few hundred forks with ground-truth labels (hand-labeled, or the frontier model's answer if you accept it as the reference). For each one, record the cheap judge's `confidence` and whether it was `correct`. Then run:

   ```
   python .claude/skills/judge-cascade/scripts/threshold_sweep.py labels.csv \
       --frontier-acc 0.96 --cheap-cost 0.05 --frontier-cost 12 --target-acc 0.95
   ```

   The script prints a reliability table (confidence bin → observed accuracy, plus ECE) and a threshold sweep. If the high-confidence bins are much less accurate than their confidence, the judge is overconfident: raise τ or recalibrate before trusting it. Run it with `--demo` to see the output format without data. The script's math is in `references/cost-and-calibration.md`.

4. **Pick τ with the full cost in view.** Choose the lowest-cost τ that meets the accuracy target, per fork if their error costs differ. Then report *three* numbers, never just the judge line:
   - judgment cost: cascade vs frontier-only
   - read/write LLM cost: unchanged by the cascade
   - total cost and the real percentage saved

   Escalated calls usually dominate the cascade's judgment bill, so a few points of escalation rate matter more than the cheap judge's price.

5. **Close the memory loop (optional).** Store accepted notes with a relation label (new / related / revises / contradicts / duplicate) and feed the store back into screening, so known material is filtered before it costs a read. Duplicates caught here are the cheapest savings in the whole pipeline.

6. **Monitor after launch.** Log confidence, verdict, whether it escalated, and (when available) a later-observed truth for each fork. Re-run step 3 on a sample every so often. Calibration drifts when the sources change.

## Auditing someone else's claim

Posts and dashboards often say things like "34% escalated, 99.6% of accuracy, 47% of the fee". Before repeating such a claim, check:

| Check | Why |
|---|---|
| Is the dashboard live data or a simulation? Read the small print. | Showcase dashboards are often illustrative. |
| Do the headline counters agree with the panels? (e.g. escalated count ÷ judgments = the stated rate) | Mismatched numbers mean the figures weren't produced by one run. |
| Which benchmark, which judge task, which frontier model? | Accuracy retention is task-specific and doesn't transfer. |
| Does "cost" include reading and writing, or only the judge calls? | Judge-only savings overstate total savings. |
| Is the cited paper or product findable and does it say this? | Don't pass on citations you haven't verified. Say "unverified" when you can't check. |

## Working on `mini-apps/cascade-desk`

Follow the repo's `CLAUDE.md` conventions: one self-contained `index.html`, ES5-style vanilla JS in one IIFE, duplicated dark-theme block, guarded `localStorage`, a reduced-motion block, no network calls beyond Google Fonts. The simulator's model constants live in a single `MODEL` object near the top of the script. Change assumptions there, and keep the "simulation, not measurements" disclaimer visible in the UI.
