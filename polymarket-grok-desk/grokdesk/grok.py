"""Grok (xAI) as the desk's *supervisor*, not its trigger finger.

An LLM is far too slow and too expensive to sit in a 1-second pricing loop, and
it has no edge at predicting the next minute of BTC. What it can do is read the
structured features the quant agents produce and veto trades that look like
regime breaks, stale data, or thin books. So:

* the quant agents decide *whether* a trade has positive expected value;
* Grok can only say APPROVE or VETO for a trade they already proposed;
* every call is cost-tracked against a hard daily budget;
* if Grok is unreachable, the reply is malformed, or the budget is gone, the
  configured ``fail_mode`` decides — the default is "veto" (fail closed).

Uses xAI's OpenAI-compatible Chat Completions endpoint.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .http import HttpError, Transport

SYSTEM_PROMPT = """You are the risk supervisor of a small automated desk that trades
Polymarket "Bitcoin Up or Down" binary markets. The quant agents already priced
the trade and found positive expected value after fees. Your only job is to
catch reasons NOT to trade: stale or contradictory data, a feed gap, a book too
thin for the size, a model probability that disagrees wildly with the market
for no stated reason, extreme volatility, or anything else that suggests the
edge is an artifact. You cannot see the future; do not try to predict price.

Reply with ONLY a JSON object: {"decision": "APPROVE" | "VETO", "reason": "<= 20 words"}"""


@dataclass
class Verdict:
    approve: bool
    reason: str
    source: str  # "grok" | "disabled" | "fail_open" | "fail_closed" | "budget"
    cost_usd: float = 0.0


@dataclass
class GrokConfig:
    enabled: bool = False
    api_key_env: str = "XAI_API_KEY"
    base_url: str = "https://api.x.ai/v1"
    model: str = "grok-4"
    temperature: float = 0.0
    max_tokens: int = 120
    daily_budget_usd: float = 1.00
    # USD per 1M tokens — check xAI's current pricing for your model.
    input_price_per_m: float = 3.00
    output_price_per_m: float = 15.00
    fail_mode: str = "veto"  # "veto" (fail closed) or "approve" (fail open)


@dataclass
class GrokSupervisor:
    cfg: GrokConfig
    transport: Transport
    clock: Callable[[], float] = time.time
    spent_usd: float = 0.0
    calls: int = 0
    _day: int = field(default=-1, repr=False)

    def _roll_day(self) -> None:
        day = int(self.clock() // 86400)
        if day != self._day:
            self._day, self.spent_usd = day, 0.0

    def _fail(self, why: str) -> Verdict:
        ok = self.cfg.fail_mode == "approve"
        return Verdict(ok, why, "fail_open" if ok else "fail_closed")

    def review(self, proposal: dict[str, Any]) -> Verdict:
        if not self.cfg.enabled:
            return Verdict(True, "supervisor disabled", "disabled")
        self._roll_day()
        if self.spent_usd >= self.cfg.daily_budget_usd:
            v = self._fail(f"daily Grok budget ${self.cfg.daily_budget_usd:.2f} used")
            v.source = "budget"
            return v
        key = os.environ.get(self.cfg.api_key_env, "")
        if not key:
            return self._fail(f"{self.cfg.api_key_env} not set")

        body = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(proposal, sort_keys=True)},
            ],
        }
        try:
            resp = self.transport.post_json(
                f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                body,
                headers={"Authorization": f"Bearer {key}"},
            )
        except HttpError as e:
            return self._fail(f"grok call failed: {e.status}")

        self.calls += 1
        cost = self._cost(resp.get("usage") or {})
        self.spent_usd += cost
        try:
            text = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            v = self._fail("malformed grok response")
            v.cost_usd = cost
            return v
        v = parse_verdict(text)
        if v is None:
            v = self._fail("unparseable grok reply")
        v.cost_usd = cost
        return v

    def _cost(self, usage: dict) -> float:
        pin = float(usage.get("prompt_tokens", 0))
        pout = float(usage.get("completion_tokens", 0))
        return (pin * self.cfg.input_price_per_m + pout * self.cfg.output_price_per_m) / 1e6


def parse_verdict(text: str) -> Verdict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    decision = str(d.get("decision", "")).strip().upper()
    if decision not in ("APPROVE", "VETO"):
        return None
    return Verdict(decision == "APPROVE", str(d.get("reason", ""))[:200], "grok")
