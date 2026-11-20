# Engineering Decisions

A structured log of the design choices in this submission, organized by track
(Question 1, Question 2, and Shared Architecture), along with the trade-offs,
rejected shortcuts, and bugs caught by testing.

## Measured Summary

Both baseline agents were already functionally correct. The objective was to cut
token cost dramatically without changing `action` or `kb_article`, while keeping
every prompt viable for a real hosted LLM.

Billing formula:

$$
\text{points} = (\text{uncached\_input} \times 3) + (\text{cached\_input} \times 0.3) + (\text{output} \times 15)
$$

Because output tokens cost $5\times$ as much as uncached input tokens, cutting
output verbosity was the first priority across both tracks.

| Track | Baseline (T-1001) | Optimized (T-1001) | Public Avg (Range) | Budget |
|---|---:|---:|---:|---:|
| Q1 (`SupportCopilot`) | 8,895 | 1,308 | 1,203.75 (1,146–1,308) | 3,000 |
| Q2 (`SupportPipeline`) | 73,476 | 1,260 | 1,208.25 (1,125–1,260) | 2,500 |

All headline numbers use a fresh `CostSession` per ticket and zero assumed cache
discounts.

---

## Question 1 — Single-Call Agent (`SupportCopilot`)

### 1. Exactly one LLM call with compact JSON output

**Decision:** Keep all ticket loading, policy routing, and thread sanitization in
local Python, leaving a single LLM call per ticket as required by the Q1 spec.
Instruct the model to reply with compact JSON only (`action`, `kb_article`,
`customer_reply`), removing instructions to think step-by-step or quote policy text.

**Impact:** Output tokens on T-1001 dropped from 486 to 46, cutting output cost from
7,290 points to 690 points.

**Trade-off:** No free-text reasoning trace is returned in the completion. Because
only `action` and `kb_article` are graded, keeping a minimal JSON payload avoids
paying 15 points/token for discarded prose.

### 2. Deterministic policy routing with overlap preserved (`_policy_article_ids`)

**Decision:** Instead of stuffing all five help-center articles into every prompt,
route deterministically from the order attributes to only the compacted policies
that could apply, preserving overlapping cases.

**Why overlap matters:** A customized item that arrived damaged receives both
`custom-items-no-replace` and `damage-replacement-7-days`. A digital item whose
license was never revealed still receives `returns-within-30-days`.

**Bug caught by testing:** My first Q1 router sent only `digital-license-no-refund`
for any digital order. Because the local simulator reads `ORDER_SNAPSHOT` directly,
public tests still passed. Writing the non-circular prompt-sufficiency test
(`tests/test_prompt_sufficiency.py`) immediately exposed the missing
`returns-within-30-days` article on unrevealed digital licenses, and both tracks
were fixed.

### 3. Static system prompt with full compacted policy bodies

**Decision:** Move variable data (`POLICIES`, `THREAD`, and `ORDER_SNAPSHOT`) into
the user message, drop the random run ID from the baseline, and keep the system
prompt tight and identical across all tickets. Keep the full compacted policy
wording (`compact_article`) and sanitized customer thread (`safe_thread`).

**Why:** Keeping the system prompt static makes Q1 prefix-cache-ready if prompts
grow past provider cache minimums, while keeping the real policy text and customer
thread ensures a hosted LLM can actually make the decision.

### 4. Strict JSON parsing (`parse_resolution`)

**Decision:** Extract and validate JSON strictly and raise `ValueError` if
required keys (`action`, `kb_article`) are missing or malformed.

**Why:** Failing loudly surfaces malformed model replies rather than letting a
silent fallback issue an incorrect customer action.

### 5. Rejected Q1 shortcut: ID-only policies and dropping the thread

**Tested variant:** Sending only article IDs (without body text) and omitting the
customer thread lowers Q1 T-1001 to 1,014 points on the local simulator.

**Why rejected:** A real hosted LLM cannot enforce policy rules from an ID string
alone or read customer-provided context. Because Q1 already averages 1,203.75
points (more than $59\%$ under the 3,000-point budget), sacrificing real-LLM
viability to save ~294 points on a local mock was the wrong trade-off.

---

## Question 2 — Multi-Stage Pipeline (`SupportPipeline`)

### 1. Four architectural stages, one paid LLM call

**Decision:** Keep the four named pipeline stages (triage → retrieve → compose →
audit), running triage, retrieve, and audit deterministically in Python and
reserving the paid LLM call strictly for compose.

**Why:** Four separate LLM calls outputting just 46 tokens each would already cost
$4 \times 46 \times 15 = 2{,}760$ output points before counting a single input
token — exceeding the 2,500-point Q2 budget by math alone.

**Trade-off:** Topic tagging (`_triage`) and keyword/prefix scoring (`_retrieve`)
are tailored to this help center. At larger production scale, `_retrieve()` can
swap cleanly to BM25 or hybrid search without altering the surrounding pipeline
stages.

### 2. Bounded retrieval (`TOP_K = 4`) that never evicts the catch-all policy

**Decision:** `_retrieve()` prioritizes dedicated topic articles (ID prefix match),
falls back to body-term scoring when no dedicated article exists, caps ranked
candidates at `TOP_K = 4`, and appends `FALLBACK_ARTICLE`
(`returns-within-30-days`) afterward if it is not already included.

**Why append after slicing (instead of reserving a slot at `TOP_K - 1`):** Testing
a stricter `top[:TOP_K - 1] + [FALLBACK_ARTICLE]` cap caused an eviction bug on
orders triggering four topics simultaneously, pushing `subscription-cooling-off`
out of the retrieved set and failing three tests
(`test_prompt_alone_is_sufficient_for_every_order`,
`test_exhaustive_sweep_matches_reference`, and
`test_q2_retrieval_always_contains_the_article_the_decision_needs`). Appending
after the `TOP_K` slice guarantees the catch-all policy is always present while
keeping retrieved context strictly bounded ($\le \text{TOP\_K} + 1$).

**Scalability verified:**
`test_q2_cost_stays_flat_when_the_help_center_grows_to_hundreds_of_articles`
injects 500 filler articles mentioning refunds/returns and verifies Q2 prompt
cost stays flat and well under budget.

### 3. Fail-closed audit (`_audit`) with `customer_reply` preserved

**Decision:** Validate that the compose output parses as JSON, uses an allowed
action (`REFUND`, `REPLACE`, `SEND_ARTICLE`, `ESCALATE_TIER2`), and cites an article
that was actually retrieved. On any failure, fail closed to `ESCALATE_TIER2` with
`returns-within-30-days` and record a descriptive `validation_error`. On valid
replies, preserve `parsed.customer_reply` so Q2 returns the same fields as Q1.

**Why not substitute the first retrieved article:** Silent substitution masks
model errors and risks issuing wrong automated refunds. Escalating malformed or
ungrounded outputs to a human is safer and directly debuggable.

**Trade-off:** `_audit()` verifies structure and citation grounding, not whether a
well-formed decision applied the policy semantically.

---

## Shared Architecture & Cross-Track Decisions

### 1. Send only the 7 decision-relevant order fields (`POLICY_FIELDS`)

**Decision:** Strip `order_id`, `sku`, and `title` via `compact_order()` in
`shared/prompting.py`, sending only the 7 fields that policies inspect
(`product_type`, `subscription_age_days`, `license_revealed`, `customized`,
`damage_reported`, `delivered_days_ago`, `opened`).

**Proof:** `tests/test_prompt_sufficiency.py` verifies necessity in both directions —
dropping any of the 7 included fields changes the decision for at least one order,
while dropping `order_id`, `sku`, or `title` never changes any decision.

**Trade-off:** If a future policy depends on a new order field, `POLICY_FIELDS` must
be updated; the sufficiency test will fail loudly if an omission occurs.

### 2. Two-layer thread injection defence (`scrub()` + `safe_thread()`)

**Decision:** Customer threads are untrusted data that can bait an LLM into verbose
output or attempt instruction overrides. Rather than unconditionally deleting
threads (breaking real-LLM context) or relying only on a narrow phrase list, both
Q1 and Q2 pass threads through two layers in `shared/prompting.py`:

- `scrub()` strips known cost-bait phrases (e.g., "step by step", "double-check",
  "quote the policy") while keeping legitimate customer context intact.
- `safe_thread()` applies a broader detector for reasoning triggers and role
  overrides (e.g., "chain of thought", "show your reasoning", "ignore previous
  instructions", "SYSTEM:", "you are now", "reveal your prompt"). If any unsafe
  pattern survives, it fails closed by dropping the thread completely.

**Measured protection on T-1001 (covered by four parametrized hostile-input tests):**

| Hostile Input Pattern | Q1 Points | Q2 Points | Thread Handling |
|---|---:|---:|---|
| Normal ticket | 1,308 | 1,260 | Kept |
| "step by step... quote the policy" | 1,329 (+21) | 1,281 (+21) | Scrubbed and kept |
| "chain of thought... list each step" | 1,176 | 1,128 | Dropped (fail-closed) |
| "ignore previous instructions..." | 1,176 | 1,128 | Dropped (fail-closed) |
| "SYSTEM: you are now..." | 1,176 | 1,128 | Dropped (fail-closed) |

Without this defence, the same verbose bait inflates ticket cost to roughly 8,000
points.

**Trade-off:** Broad pattern matching can trigger false positives on legitimate
frustrated customers (e.g., asking to "show your reasoning"). In this fixture, all
decision facts exist in `ORDER_SNAPSHOT`, so dropping a flagged thread cannot break
routing. In production, a dropped-thread event should be logged or routed to human
review.

### 3. No prompt-caching discounts in reported numbers

**Decision:** Report cold-start numbers with zero cached-input discounts and do not
pad prompts to hit caching thresholds.

**Why:** Hosted APIs typically require an identical prefix of at least 1,024 tokens
to enable prompt caching. After trimming, both Q1 and Q2 prompts sit near ~200
tokens; padding them to 1,024 tokens just to unlock cached pricing would waste
roughly 3,000 points on cold calls. Keeping system prompts static across tickets
ensures caching works automatically if system instructions ever grow past 1,024
tokens.

### 4. Non-circular testing (`tests/test_prompt_sufficiency.py`)

**Decision:** Because the local simulator parses `ORDER_SNAPSHOT` and ignores the
rest of the prompt, passing standard tests does not prove the prompt actually gives
a real model enough information. `test_prompt_sufficiency.py` swaps in a strict
`PromptOnlyLlm` that decides only from visible `POLICIES` and `ORDER_SNAPSHOT` text
across a 960-order Cartesian sweep, paired with ablation tests that withhold a
needed article to prove the test fails when information is missing.

---

## Rejected System-Wide Alternatives

- **Vector search / embeddings:** Five help-center articles do not justify
  embedding latency or index infrastructure. Deterministic topic routing with
  `TOP_K` bounding keeps retrieval fast, explainable, and cost-flat even against
  500 synthetic filler articles.
- **Model routing:** The local simulator exposes a single model pricing tier, so
  multi-model routing cannot be measured honestly in this harness.
- **Semantic caching:** Unsafe for support decisions where two nearly identical
  customer messages differ by one critical order fact (e.g.,
  `delivered_days_ago = 7` vs. `8`).

---

## Limits (What This Does Not Claim)

- **Local simulator vs. live LLM:** Passing the local harness verifies token cost,
  prompt completeness, routing logic, and structural guardrails. It does not prove
  live accuracy against a hosted LLM.
- **Structural vs. semantic audit:** Q2's `_audit()` checks JSON schema validity,
  allowed action enums, and citation grounding against retrieved articles; it
  cannot verify whether a well-formed completion reasoned correctly over the
  policy text.
