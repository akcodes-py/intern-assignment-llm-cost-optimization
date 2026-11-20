# ShopAssist — support copilot cost lab

**Submission by Atul Kumar** — [Live repository](https://github.com/akcodes-py/intern-assignment-llm-cost-optimization)

ShopAssist is a Tier-1 e-commerce support copilot. For each ticket it reads the customer
thread and order facts, picks one of four actions (`REFUND`, `REPLACE`, `SEND_ARTICLE`,
`ESCALATE_TIER2`), and cites the help-center article that backs the decision.

## Results

| Track | Baseline (T-1001) | Optimized (T-1001) | Public average | Budget |
|---|---:|---:|---:|---:|
| **Q1** — single call | 8,895 | 1,308 | **1,204** | 3,000 |
| **Q2** — 4-node pipeline | 73,476 | 1,260 | **1,208** | 2,500 |

All figures use a fresh `CostSession` per ticket, `tiktoken 0.14.0`, and zero assumed
cache discounts. Every public ticket and the holdout `T-1091` returns the correct
`action` and `kb_article`. The full per-call breakdown is in
[`docs/WRITEUP.md`](docs/WRITEUP.md); the reasoning behind each choice is in
[`DECISIONS.md`](DECISIONS.md).

## Tracks

| Track | Code | Spec |
|---|---|---|
| Q1 — single call | [`question1/agent.py`](question1/agent.py) | [`questions/question-1.md`](questions/question-1.md) |
| Q2 — pipeline | [`question2/agent.py`](question2/agent.py) | [`questions/question-2.md`](questions/question-2.md) |

### Q1 — `SupportCopilot`

Exactly one LLM call per ticket. A deterministic router selects only the compacted
policy articles an order could need (overlapping policies are kept), seven
decision-relevant order fields are sent, and the system prompt is static across
tickets. Output is compact JSON with no reasoning or quoted articles: 486 output
tokens on T-1001 became 46.

### Q2 — `SupportPipeline`

The four required stages are kept, but only `compose` calls the model:

```
triage (local) → retrieve (local, TOP_K=4) → compose (ONE LLM CALL) → audit (local, fail-closed)
```

Four 46-token replies would already cost 2,760 output points, over the 2,500-point
budget before a single input token — so the node count stays and the extra calls do not.
The catch-all policy is appended after the `TOP_K` slice so it can never evict a
dedicated policy the decision needs.

## Run

```bash
pip install -r requirements.txt
python scripts/benchmark.py --output results/optimized.json --check
pytest -q
python question1/run.py
python question2/run.py
```

`benchmark.py --check` exits non-zero if any public ticket exceeds its budget; it
currently exits 0. `pytest -q` reports **116 passed, 2 failed** — the two failures are
`test_baseline_cost_is_high`, which intentionally assert the original high cost and
must fail once the agents are optimized. They are deselected in CI.

## Testing beyond the simulator

The local simulator derives its answer from `ORDER_SNAPSHOT` alone, so passing it does
not prove a prompt is sufficient for a real model. The submission adds:

- a **prompt-only stand-in model** over a 960-order sweep (`tests/test_prompt_sufficiency.py`)
- ablation tests that withhold a needed article to prove the test can fail
- field-necessity proofs for all 7 sent order fields
- hostile/messy thread tests and a 500-article scaling test (`tests/test_robustness.py`)
- CI with lint, the budget gate, and a 100% coverage gate on the three main modules

See [`docs/WRITEUP.md`](docs/WRITEUP.md) for results and limits.

## Billing

| Input | Cached input | Output |
|---:|---:|---:|
| 3 | 0.3 | 15 |

Cached input = identical prefix ≥ 1,024 tokens across calls.

```
points = uncached_input × 3 + cached_input × 0.3 + output × 15
```

Per-call accounting lives in [`shared/billing.py`](shared/billing.py) (`CostSession`).
`shared/llm.py` provides the local simulator. Do not import `eval/reference.py` from
an agent — the submission does not.

## Repository layout

```
DECISIONS.md              engineering decision log
docs/WRITEUP.md           results, per-call breakdowns, limits
results/                  baseline.json and optimized.json
scripts/benchmark.py      measurement + budget gate
scripts/update_writeup.py regenerates the Results section from measured JSON
tests/                    optimized, prompt-sufficiency and robustness suites
.github/workflows/ci.yml  lint, budget, tests, coverage
```
