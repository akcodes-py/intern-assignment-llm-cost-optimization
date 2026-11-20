"""Fill the RESULTS block of docs/WRITEUP.md from results/*.json (no hand-copied numbers).

    python scripts/benchmark.py --baseline --output results/baseline.json   # once
    python scripts/benchmark.py --output results/optimized.json --check
    python scripts/update_writeup.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
START, END = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
PUBLIC = ["T-1001", "T-1002", "T-1003", "T-1004"]
BUDGET = {"question1": 3000, "question2": 2500}


def rows(report, track):
    return {r["ticket_id"]: r for r in report[track]}


def call_line(c):
    return (f"{c['label']}: {c['uncached_input_tokens']} in x 3 + {c['cached_input_tokens']} cached x 0.3 "
            f"+ {c['output_tokens']} out x 15 = **{c['points']:,}**")


def build(base, opt) -> str:
    out = []
    meta = f"`tiktoken {opt['tiktoken']}`, Python {opt['python']}, {opt['measurement']}."
    out.append(f"Measured with {meta} Raw data: `results/optimized.json`, `results/baseline.json`.\n")
    out.append("### Headline\n")
    out.append("| Track | Baseline (T-1001) | Optimized (T-1001) | Change | Budget | Worst public ticket |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for track, label in [("question1", "Q1"), ("question2", "Q2")]:
        b, o = rows(base, track)["T-1001"], rows(opt, track)["T-1001"]
        worst = max(rows(opt, track)[t]["total_points"] for t in PUBLIC)
        change = (o["total_points"] - b["total_points"]) / b["total_points"] * 100
        out.append(f"| {label} | {b['total_points']:,} | {o['total_points']:,} | {change:+.0f}% | "
                   f"{BUDGET[track]:,} | {worst:,} |")
    for track, label in [("question1", "Q1"), ("question2", "Q2")]:
        out.append(f"\n### {label} - all tickets\n")
        out.append("| Ticket | Action | Article | Input tok | Output tok | Points | Budget |")
        out.append("|---|---|---|---:|---:|---:|---:|")
        for tid, r in rows(opt, track).items():
            c = r["calls"]
            budget = f"{BUDGET[track]:,}" if tid in PUBLIC else "holdout"
            out.append(f"| {tid} | {r['action']} | `{r['kb_article']}` | {sum(x['input_tokens'] for x in c)} | "
                       f"{sum(x['output_tokens'] for x in c)} | {r['total_points']:,} | {budget} |")
    out.append("\n### Per-call breakdown\n")
    q1 = rows(opt, "question1")["T-1001"]
    out.append("**Q1, T-1001 (physical refund):**\n")
    out += [f"- {call_line(c)}" for c in q1["calls"]]
    q2 = rows(opt, "question2")
    for tid, path in [("T-1001", "physical fulfillment path"), ("T-1003", "denial / escalation path")]:
        out.append(f"\n**Q2, {tid} ({path}):** triage, retrieve and audit run locally (0 points).\n")
        out += [f"- {call_line(c)}" for c in q2[tid]["calls"]]
        out.append(f"- total: **{q2[tid]['total_points']:,}**")
    return "\n".join(out) + "\n"


def main() -> None:
    base = json.loads((ROOT / "results/baseline.json").read_text())
    opt = json.loads((ROOT / "results/optimized.json").read_text())
    path = ROOT / "docs/WRITEUP.md"
    text = path.read_text(encoding="utf-8")
    a, b = text.index(START) + len(START), text.index(END)
    path.write_text(text[:a] + "\n" + build(base, opt) + text[b:], encoding="utf-8")
    print("updated", path)


if __name__ == "__main__":
    main()
