# Evaluation

The evaluator decides in a fixed order.

1. **Validity.** Candidates that modify protected paths are rejected. Runs that failed during setup make the result
   inconclusive (setup failures are never blamed on the application).
2. **Correctness gates**, run on baseline and candidate:
   - `scenario_expectations`: every `expect` entry in every scenario
   - `world_invariants`: the world's self-consistency checks
   - each gate function named in `policy.gates`, from the locked `checks.py`
   - `candidate:runs_completed`: no crashes or timeouts
   Any candidate gate failure → **rejected**, whatever the performance.
   Gate functions run separately over visible and holdout runs; holdout failures say "details withheld".
3. **Objectives** from `policy.objectives`, compared per trial (metric summed across scenarios) with a percentile bootstrap:
   - `role: primary`: must improve by at least `min_improvement` at the lower bound of the `confidence` interval → **accepted**.
     If the interval crosses the threshold → **inconclusive**. If it is clearly worse → **rejected**.
   - `role: guard`: must not regress by more than `max_regression`.
   - If the coefficient of variation exceeds `max_cv`, or there are fewer than `min_trials`, the result is **inconclusive**.
4. If the baseline itself fails gates → **needs review** (the comparison point isn't trustworthy).
   If the change touches `require_review_for` paths → **needs review**.

Every check is recorded with its detail and evidence, and the decision lists its reasons.

## Gate functions

```python
def no_refund_outside_return_window(v):          # v: VersionView
    bad = [r for run, o in v.completed() for r in o["world"]["refunds"].values() if r["days_since_delivery"] > 30]
    return not bad, f"{len(bad)} out-of-window refunds"
```

`v.completed()` yields `(run, outcome)`; outcomes hold `responses`, final `world` state, `clock`, `meters` and `world_violations`.

## Model-based judgments

The default evaluator is deterministic. A gate function may call a separately controlled model, but it must report its own
uncertainty in the detail, and the result should be treated as needing review when the evidence is thin.
