"""Reproduce per-call accounting for the original or optimized agents."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.billing import CostSession  # noqa: E402

BASELINE_COMMIT = "d9989e294615046519a1658ff76c4343f465fb0c"
TICKETS = ["T-1001", "T-1002", "T-1003", "T-1004", "T-1091"]


def load_agents(baseline: bool):
    if not baseline:
        from question1.agent import SupportCopilot
        from question2.agent import SupportPipeline

        return SupportCopilot, SupportPipeline
    agents = []
    for track, name in [("question1", "SupportCopilot"), ("question2", "SupportPipeline")]:
        source = subprocess.check_output(
            ["git", "show", f"{BASELINE_COMMIT}:{track}/agent.py"], cwd=ROOT, text=True
        )
        module = types.ModuleType(f"baseline_{track}")
        sys.modules[module.__name__] = module
        # Load the pinned original implementation without changing working files.
        exec(compile(source, f"{BASELINE_COMMIT}:{track}/agent.py", "exec"), module.__dict__)
        agents.append(getattr(module, name))
    return agents


def measure(agent_class, ticket_id: str) -> dict:
    # Fresh agent and session per ticket: no warm-up or assumed cache discounts.
    session = CostSession()
    agent = agent_class(session=session)
    resolution = agent.handle_ticket(ticket_id)
    calls = []
    for call in session.calls:
        cached = min(call.cached_prefix_tokens, call.input_tokens)
        calls.append(
            {
                "label": call.label,
                "input_tokens": call.input_tokens,
                "cached_input_tokens": cached,
                "uncached_input_tokens": call.input_tokens - cached,
                "output_tokens": call.output_tokens,
                "points": call.points(),
                "prompt_sha256": hashlib.sha256(call.prompt.encode()).hexdigest(),
            }
        )
    return {
        "ticket_id": ticket_id,
        "action": resolution.action,
        "kb_article": resolution.kb_article,
        "calls": calls,
        "total_points": session.total_points(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="Run pinned upstream agents")
    parser.add_argument("--output", type=Path, help="Also save the JSON report")
    parser.add_argument("--check", action="store_true", help="Fail if a public budget is exceeded")
    args = parser.parse_args()
    if args.baseline and args.check:
        parser.error("--check applies only to optimized agents")
    q1, q2 = load_agents(args.baseline)
    report = {
        "implementation": "baseline" if args.baseline else "optimized",
        "baseline_commit": BASELINE_COMMIT,
        "python": sys.version.split()[0],
        "tiktoken": importlib.metadata.version("tiktoken"),
        "billing": {"input": 3, "cached_input": 0.3, "output": 15},
        "measurement": "fresh session per ticket; local simulator; no cache discounts",
        "question1": [measure(q1, tid) for tid in TICKETS],
        "question2": [measure(q2, tid) for tid in TICKETS],
    }
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    if args.check:
        violations = [
            f"{track}/{row['ticket_id']}: {row['total_points']} > {budget}"
            for track, budget in [("question1", 3000), ("question2", 2500)]
            for row in report[track]
            if row["ticket_id"] in TICKETS[:4] and row["total_points"] > budget
        ]
        if violations:
            raise SystemExit("\n".join(violations))


if __name__ == "__main__":
    main()
