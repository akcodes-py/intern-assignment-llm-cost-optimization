# ShopAssist cost optimization

**Result:** points per ticket fall from ~8,900 to ~1,200 on Q1 and from ~73,500 to ~1,200 on Q2,
with the same `action` and `kb_article` on every ticket tested. Exact, regenerated numbers are in
[Results](#results). For the reasoning behind each choice, see [DECISIONS.md](../DECISIONS.md).

Billing: `points = uncached_input x 3 + cached_input x 0.3 + output x 15`.
Output costs 5x input, so I attacked it first, then the number of calls, then input size.

## Why the baseline was expensive

| Cause | Where | Effect |
|---|---|---|
| "Think step by step... quote articles" | Q1 prompt | ~480 output tokens x 15 = ~7,300 of 8,895 points |
| Per-run `uuid` in the system prompt | Q1 prompt | every prompt unique, nothing can ever be cached |
| 80k-120k tokens of help center in triage/audit | Q2 prompts | tens of thousands of input points |
| Four model calls that re-send each other's output | Q2 pipeline | four replies of even 46 tokens already cost 2,760 > 2,500 budget |

## Architecture

```
Q1   ticket + order ──> route 1-2 articles (local) ──> [ ONE LLM CALL ] ──> parse ──> action, article

Q2   ticket + order ──> triage (local) ──> retrieve top-K (local) ──> [ compose: ONE LLM CALL ]
                                                                            │
                                  action, article  <── audit (local, fail-closed) <──┘
```
Only the bracketed boxes cost points. Everything else is plain code.

## Design decisions and trade-offs

| Decision | Why | Cost of the choice |
|---|---|---|
| Output kept to JSON only | output is 15 pts/token vs 3 for input | no free-text reasoning to inspect |
| One model call in Q2, four stages kept | the spec names four *nodes*, not four calls; 4 replies of even 46 tokens = 2,760 pts > budget | triage/retrieve/audit logic is code I must maintain and test |
| Local fail-closed audit instead of a second LLM "judge" | a judge re-sends the whole context (more input points) and is itself fallible | audit is structural only (see Limits) |
| Top-K bounded retrieval | prompt size independent of help-center size | a relevant article could be ranked out; mitigated by dedicated-policy score 10 and the catch-all article always included |
| Send 7 order fields, not the whole order | identifiers never affect a decision | a new policy that reads a new field needs `POLICY_FIELDS` updated (tests fail loudly if a field is missing) |
| Two-layer defence on customer text | `scrub()` removes known cost bait; `safe_thread()` applies a wider net and drops the whole thread if anything survives | a phrase list alone is only a mitigation; dropping unsafe text costs a little context on hostile tickets only |
| No caching | prompts are ~200-400 tokens, below the 1,024-token minimum | none today; revisit if prompts grow |

## What I changed

### Q1 - one call
- Reasoning/quoting instructions, few-shot block, `uuid` and pretty-printed JSON removed.
- A small deterministic router sends only the policy articles the order actually needs (usually 1-2)
  (overlapping policies such as custom + damaged are kept).
- Only the seven order fields the policy reads are sent; the reply is JSON only.

### Q2 - four stages, one paid call
1. `triage` - local: order facts -> policy topics.
2. `retrieve` - local: a topic that has a dedicated policy article (id starts with the topic, e.g.
   `custom-items-...`) uses exactly that article, so generic words like "refund" never drag in
   unrelated policies (an earlier keyword version sent three articles for a plain return; this sends
   one). A topic with no dedicated article falls back to the best-scoring body match. Total is capped
   at `TOP_K=4` (plus the always-included catch-all returns article), so the prompt is bounded no
   matter how large the help center grows. An order of unknown shape gets only the catch-all article.
3. `compose` - the only model call.
4. `audit` - local and **fail-closed**: unparseable JSON, an unknown action, or a citation outside
   the retrieved set becomes `ESCALATE_TIER2` with a recorded `validation_error`. A wrong refund
   costs far more than a human review, so failures must be visible, not silently "fixed".

### Untrusted input
The customer thread is untrusted. Phrases such as "step by step", "double-check" or "quote the..."
make the simulator (and a real model) write long answers, and output is the expensive side.
There are two layers:

1. `scrub()` removes the known cost-bait phrases.
2. `safe_thread()` applies a second, wider net for other reasoning requests and role-override
   attempts ("chain of thought", "ignore previous instructions", "SYSTEM:", "you are now", …).
   If anything survives that net, the entire thread is dropped — fail closed, not best-effort.

Measured on T-1001 with four hostile messages: the worst case was **1,329 Q1 / 1,281 Q2**
against baselines of 1,308 / 1,260 (+21 points), and three of the four attacks caused the thread
to be dropped entirely (1,176 / 1,128). Without any defence the same message costs ~8k points.
Four parametrized tests assert both agents stay at baseline cost under all four attacks, plus a
unit test that `safe_thread()` returns an empty string for instruction-override text.

## Caching: considered and rejected
Cached input is 0.3 vs 3 per token, but only for an identical prefix of >= 1,024 tokens.
After trimming, a whole Q1 prompt is ~200 tokens, so there is no eligible prefix. Padding the prompt
to 1,024 tokens to qualify would cost ~3,000 points on the cold call and only pays back after many
warm hits (estimate, assuming ~60 ticket-specific tokens). Shrinking the prompt saves more than
caching a bigger one. The Q2 system prompt is still byte-identical across tickets (tested), so
caching can be switched on later if the prompt ever grows.

## Evidence it is correct, not just cheap

The repo's simulator answers from `ORDER_SNAPSHOT` and ignores the rest of the prompt, so passing it
alone proves little. I therefore added checks that do not depend on it:

- **Prompt-only stand-in model** (`tests/test_prompt_sufficiency.py`): decides *only* from the policy
  text and order facts visible in the prompt, and answers "cannot tell" if the needed article is
  missing. Over 960 order combinations (1,920 agent runs), both agents' prompts are sufficient. Ablations show that
  dropping the needed article makes Q1 fail loudly and Q2 fail closed, so the test is not vacuous.
- **Field necessity**: each of the 7 order fields I send is shown to change the correct decision for
  some order; the dropped identifiers (`order_id`, `sku`, `title`) are shown never to matter. The
  trimming is lossless and minimal.
- **Hand-written scenarios** derived from the policy text, not from `eval/reference.py`: 17 cases
  including boundaries (day 7 / 30 / 31), opened goods, digital + license, subscriptions on
  day 1 / 30 / 31 / 38 / 400, and missing fields.
- **Hostile / messy threads** (all caps, empty, another language, "ignore previous instructions",
  reasoning-bait) never change the decision or the length of the reply; unsafe text is dropped
  entirely by `safe_thread()`.
- **Scaling**: 500 filler articles that mention "refund / return" leave the prompt size and the answer
  unchanged.
- **Audit**: malformed JSON, wrong type, unknown action and a fabricated article id all fail closed.
- **Coverage**: 100% line coverage of `question1/agent.py`, `question2/agent.py` and
  `shared/prompting.py`; 115 tests; CI (`.github/workflows/ci.yml`) runs lint, the budget gate, the tests, and fails below 100% coverage of those files.

### A bug these tests caught
My first Q1 router sent only the digital article for any digital product. The prompt-only check showed
that for a digital item **without** a revealed license (or customized, or damaged) the correct answer cites
a different article that was never in the prompt, so a real model could not have cited it. The simulator
could not see this because it never reads the articles. Fixed by adding the custom / damage / returns
articles in those branches; the public tickets and their costs are unaffected.

## FAQ

**Where is the AI, if rules choose the context?** In this fixture the policy is small enough that the
decision is fully determined by a few order fields, so the model's real work is reading free-form
customer language and writing the reply. I have *not* measured a hosted model here: the local simulator
cannot show that, and I say so rather than claim it. The routing code decides what the model sees; it
does not replace the model.

**Why not cache?** See "Caching: considered and rejected".

**What would you do next?** Run the hand-written scenarios plus messy threads against one hosted model
and report accuracy and real cost, then replace keyword scoring with a maintained index or embeddings.

## Results
<!-- RESULTS:START -->
Measured with `tiktoken 0.14.0`, Python 3.14.4, fresh session per ticket; local simulator; no cache discounts. Raw data: `results/optimized.json`, `results/baseline.json`.

### Headline

| Track | Baseline (T-1001) | Optimized (T-1001) | Change | Budget | Worst public ticket |
|---|---:|---:|---:|---:|---:|
| Q1 | 8,895 | 1,308 | -85% | 3,000 | 1,308 |
| Q2 | 73,476 | 1,260 | -98% | 2,500 | 1,260 |

### Q1 - all tickets

| Ticket | Action | Article | Input tok | Output tok | Points | Budget |
|---|---|---|---:|---:|---:|---:|
| T-1001 | REFUND | `returns-within-30-days` | 206 | 46 | 1,308 | 3,000 |
| T-1002 | SEND_ARTICLE | `digital-license-no-refund` | 187 | 39 | 1,146 | 3,000 |
| T-1003 | ESCALATE_TIER2 | `custom-items-no-replace` | 201 | 39 | 1,188 | 3,000 |
| T-1004 | REPLACE | `damage-replacement-7-days` | 211 | 36 | 1,173 | 3,000 |
| T-1091 | SEND_ARTICLE | `subscription-cooling-off` | 187 | 38 | 1,131 | holdout |

### Q2 - all tickets

| Ticket | Action | Article | Input tok | Output tok | Points | Budget |
|---|---|---|---:|---:|---:|---:|
| T-1001 | REFUND | `returns-within-30-days` | 190 | 46 | 1,260 | 2,500 |
| T-1002 | SEND_ARTICLE | `digital-license-no-refund` | 206 | 39 | 1,203 | 2,500 |
| T-1003 | ESCALATE_TIER2 | `custom-items-no-replace` | 220 | 39 | 1,245 | 2,500 |
| T-1004 | REPLACE | `damage-replacement-7-days` | 195 | 36 | 1,125 | 2,500 |
| T-1091 | SEND_ARTICLE | `subscription-cooling-off` | 206 | 38 | 1,188 | holdout |

### Per-call breakdown

**Q1, T-1001 (physical refund):**

- handle_ticket: 206 in x 3 + 0 cached x 0.3 + 46 out x 15 = **1,308**

**Q2, T-1001 (physical fulfillment path):** triage, retrieve and audit run locally (0 points).

- compose: 190 in x 3 + 0 cached x 0.3 + 46 out x 15 = **1,260**
- total: **1,260**

**Q2, T-1003 (denial / escalation path):** triage, retrieve and audit run locally (0 points).

- compose: 220 in x 3 + 0 cached x 0.3 + 39 out x 15 = **1,245**
- total: **1,245**
<!-- RESULTS:END -->

## Limits (honest)
- `shared/llm.py` is a simulator that derives answers from `ORDER_SNAPSHOT`. Passing it shows the
  prompts are cheap, carry the needed facts and articles, and the guardrails work; it does **not**
  show accuracy of a hosted model. That would need an evaluation set run against a real model.
- Retrieval is keyword/score based and tuned to this small help center; a production help center
  would need a maintained index or embeddings, with this logic as the fallback.
- The audit is structural: it proves the output is well-formed and grounded in retrieved
  articles, not that the policy decision is right.
- Upstream `test_baseline_cost_is_high` tests assert the old cost and fail by design.
- No cache discount is assumed in any reported number. Absolute token counts depend on the local
  tokenizer (`tiktoken`); treat them as reproducible in this repo, not vendor-exact.

## Reproduce
```bash
python scripts/benchmark.py --baseline --output results/baseline.json   # optional, already committed
python scripts/benchmark.py --output results/optimized.json --check
python scripts/update_writeup.py
pytest -q          # 2 upstream baseline-cost tests fail by design
```
