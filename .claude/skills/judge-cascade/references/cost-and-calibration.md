# Cost and calibration math

Notation for one fork type, evaluated on N labeled judgments:

| Symbol | Meaning |
|---|---|
| cᵢ | cheap judge's confidence on item i (0.5–1 for BOOL; top-label probability for CHOICE) |
| yᵢ | 1 if the cheap verdict was correct, else 0 |
| τ | escalation threshold: escalate when cᵢ < τ |
| a_F | frontier model accuracy on the escalated items |
| p_C, p_F | price per judgment, cheap and frontier |

## Escalation and accuracy

```
E(τ)   = (1/N) · #{ i : cᵢ < τ }                     escalation rate
A(τ)   = (1/N) · [ Σ_{cᵢ ≥ τ} yᵢ  +  a_F · #{cᵢ < τ} ]  cascade accuracy
```

`a_F` is the frontier accuracy **on the escalated items**, which are the hard ones. It is usually lower than the frontier's overall accuracy. If your CSV has a `frontier_correct` column, `threshold_sweep.py` uses the observed values instead of the constant, and you should prefer that.

"Accuracy retained" = A(τ) / a_F(all items). A figure like 99.6% is only meaningful next to the task and model it was measured on.

## Cost

Every item pays for the cheap judge. Escalated items also pay for the frontier:

```
C_cascade(τ) = p_C + E(τ) · p_F            per judgment
C_frontier   = p_F
fee ratio    = C_cascade / C_frontier = p_C/p_F + E(τ)
```

Because p_C ≪ p_F, **the fee ratio is essentially the escalation rate**. Take the claim "34% escalated at 47% of the fee": those two numbers only fit together if the cheap judge costs about 13% of the frontier's price, or if escalated calls are longer than average. That kind of mismatch is worth asking about.

Total pipeline cost adds the reads and writes, which the cascade doesn't touch:

```
C_total = judgments · C_cascade(τ) + reads · p_read + writes · p_write
```

Report `C_total(cascade) / C_total(frontier-only)`, not just the fee ratio.

## Calibration

Bin items by confidence (e.g. 0.50–0.60, …, 0.90–1.00). For each bin compare mean confidence with observed accuracy:

```
ECE = Σ_bins (n_b / N) · | mean(c)_b − mean(y)_b |
```

- **Calibrated**: accuracy ≈ confidence in every bin. Then τ reads directly as "the minimum accuracy I'll accept without a second opinion".
- **Overconfident**: accuracy is below confidence, worst in the top bins. These are the items the cascade never escalates, so this is the dangerous failure. Fix it with temperature scaling or isotonic regression on held-out data, or raise τ.
- **Underconfident**: accuracy is above confidence. It's safe but wasteful. Lowering τ saves money.

## Choosing τ

1. Sweep τ over 0.50–1.00.
2. Keep the τ values with A(τ) ≥ target.
3. Pick the one with the lowest cost.
4. If none qualifies, the cheap judge can't carry that fork at the target. Either escalate it always, or improve the judge.

When forks have asymmetric error costs, weight the errors (e.g. a false skip costs 5× a false keep) and minimise expected total cost instead of meeting a flat accuracy target.
