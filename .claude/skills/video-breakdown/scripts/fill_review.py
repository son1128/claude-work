#!/usr/bin/env python3
"""Merge a compact per-shot verdict file into rea-video's review_answers.json.

Usage:
  python3 -I fill_review.py <out_dir> <verdicts.json>

verdicts.json (only the shots you actually looked at):
{
  "cast": {                       # optional, whole-video cast (object, not text)
    "style": "handheld phone UGC ad, natural light",
    "characters": [{"id": "A", "name": "presenter's hand", "kind": "person",
                    "look": "male hand, silver watch", "role": "demonstrates", "voice": "", "shots": [1, 2]}],
    "scenes": [{"id": "S1", "where": "home kitchen counter", "when": "day", "look": "grey marble, cluttered", "shots": [1, 2]}],
    "props": [{"name": "portable blender", "note": "lavender, LCD timer", "shots": [1, 2]}]
  },
  "shots": {
    "shot_001": {
      "probs": {"live_action": 0.85, "ai_video": 0.15},
      "evidence": ["legible product labels stable across cuts", "..."],
      "description": "hand drops strawberries into a navy portable blender",
      "image_prompt": "...",  # optional: first-frame image prompt
      "prompt": "..."         # optional, AI shots: English video prompt.
                              # Written to both video_prompt (what the report's
                              # remake plan prints) and video_prompt_en.
    }
  }
}
Probabilities are normalised to sum to 1. Shots not listed stay empty and
apply-review skips them.
"""
import json
import sys
from pathlib import Path

LABELS = {"live_action", "ai_video", "avatar", "cg_3d", "code_render", "screen_capture",
          "still_motion", "static_hold", "anim_2d", "stop_motion", "film_footage"}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    out_dir, verdict_path = Path(sys.argv[1]), Path(sys.argv[2])
    answers_path = out_dir / "review_answers.json"
    if not answers_path.is_file():
        print(f"missing {answers_path}: run `rea-video review {out_dir}` first", file=sys.stderr)
        return 1
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    verdicts = json.loads(verdict_path.read_text(encoding="utf-8"))
    answers["by"] = verdicts.get("by", "claude")
    if isinstance(verdicts.get("cast"), dict):
        answers["cast"] = verdicts["cast"]
    elif verdicts.get("cast"):
        print("cast must be an object (style/characters/scenes/props); ignored", file=sys.stderr)

    by_id = {s["id"]: s for s in answers["shots"]}
    filled = []
    for sid, v in verdicts.get("shots", {}).items():
        shot = by_id.get(sid)
        if shot is None:
            print(f"skip unknown shot id {sid}", file=sys.stderr)
            continue
        probs = {k: float(p) for k, p in v.get("probs", {}).items() if float(p) > 0}
        bad = set(probs) - LABELS
        if bad:
            print(f"{sid}: unknown labels {sorted(bad)}", file=sys.stderr)
            return 1
        total = sum(probs.values())
        if total <= 0:
            continue
        shot["label_probs"] = {k: round(p / total, 3) for k, p in probs.items()}
        ev = v.get("evidence", [])
        shot["evidence"] = [ev] if isinstance(ev, str) else list(ev)
        if v.get("description"):
            shot["description"] = v["description"]
        rep = shot.setdefault("replication", {})
        if v.get("prompt"):
            rep["video_prompt"] = rep["video_prompt_en"] = v["prompt"]
        for key in ("image_prompt", "video_prompt", "video_prompt_en"):
            if v.get(key):
                rep[key] = v[key]
        filled.append(sid)

    answers_path.write_text(json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"filled {len(filled)}/{len(answers['shots'])} shots: {', '.join(filled)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
