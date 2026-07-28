"""Eval suite for the payment agent.

Runs in dry mode, so no payment is created and no network call is made.
Each scenario asserts two things:

  outcome     where it ended
  trajectory  which nodes it must never have touched

The second one is the important half. A graph can reach the right ending for
the wrong reason. "Did execute_payment ever run?" is the question that matters
when the node moves money.

Run:  PAYMENTS_MODE=dry .venv/bin/python evals.py
"""

import json
import os
from pathlib import Path

os.environ.setdefault("PAYMENTS_MODE", "dry")
os.environ.setdefault("VOP_PROVIDER", "stub")

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from graph.app import builder

graph = builder.compile(checkpointer=InMemorySaver())

GOLDEN = json.loads((Path(__file__).parent / "data" / "golden.json").read_text())

# The cases live in data/golden.json so the expectations can be reviewed and
# diffed without reading Python. Each case names the rule it protects.
SCENARIOS = [
    {
        "id": c["id"],
        "split": c["split"],
        "name": c["name"],
        "protects": c["protects"],
        "input": c["input"],
        "resume": c["resume"],
        "ends_at": c["ends_at"],
        "must_not_visit": c["must_not_visit"],
        "expect_vop": c.get("expect_vop"),
        "expect_visits_once": c.get("expect_visits_once", []),
    }
    for c in GOLDEN["cases"]
]


def run(scenario: dict, index: int) -> dict:
    config = {"configurable": {"thread_id": f"eval-{scenario['id']}"}}
    state = graph.invoke(scenario["input"], config)

    if "__interrupt__" in state:
        if scenario["resume"] is None:
            return {"pass": False, "why": "paused for a human unexpectedly",
                    "trail": state.get("trail", [])}
        state = graph.invoke(Command(resume=scenario["resume"]), config)

    trail = state.get("trail", [])
    problems = []

    if not trail or trail[-1] != scenario["ends_at"]:
        problems.append(f"ended at {trail[-1] if trail else 'nothing'}, expected {scenario['ends_at']}")

    for node in scenario["must_not_visit"]:
        if node in trail:
            problems.append(f"reached {node}, which it must never do")

    for node in scenario.get("expect_visits_once", []):
        if trail.count(node) != 1:
            problems.append(f"{node} ran {trail.count(node)} times, expected exactly 1")

    expected_vop = scenario.get("expect_vop")
    if expected_vop and state.get("vop", {}).get("status") != expected_vop:
        problems.append(f"vop was {state.get('vop', {}).get('status')}, expected {expected_vop}")

    return {"pass": not problems, "why": "; ".join(problems), "trail": trail}


def main() -> None:
    results = []
    for i, scenario in enumerate(SCENARIOS):
        result = run(scenario, i)
        results.append(result)
        print(f"{'PASS' if result['pass'] else 'FAIL'}  {scenario['name']}")
        print(f"      {' -> '.join(result['trail'])}")
        if not result["pass"]:
            print(f"      why: {result['why']}")

    passed = sum(1 for r in results if r["pass"])
    print(f"\n{passed}/{len(results)} passed ({100 * passed // len(results)}%)")

    by_split = {}
    for scenario, result in zip(SCENARIOS, results):
        hit, total = by_split.get(scenario["split"], (0, 0))
        by_split[scenario["split"]] = (hit + int(result["pass"]), total + 1)
    print("\nby split:")
    for split, (hit, total) in sorted(by_split.items()):
        print(f"  {split:12} {hit}/{total}")


if __name__ == "__main__":
    main()
