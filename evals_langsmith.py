"""The same eval suite, run through LangSmith.

Local evals.py answers "does it pass right now" in your terminal.
This answers "did it get better or worse than last time", because every run
becomes an experiment attached to a versioned dataset, and every scenario
links to its own trace.

Three pieces, which is the whole LangSmith eval model:

  dataset     the scenarios, stored server-side and versioned
  target      the thing under test (here: run the graph, return what it did)
  evaluators  plain functions scoring one run, returning {key, score}

Run:  .venv/bin/python evals_langsmith.py
"""

import os
from pathlib import Path

# Load .env before importing anything that reads the environment.
for line in Path(__file__).with_name(".env").read_text().splitlines():
    if line.strip() and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

os.environ["PAYMENTS_MODE"] = "dry"   # never create a real payment from an eval

from langsmith import Client
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from evals import SCENARIOS
from graph.app import builder

DATASET = "payment-agent-scenarios"

client = Client()
graph = builder.compile(checkpointer=InMemorySaver())


def sync_dataset() -> None:
    """Create the dataset once, then keep its examples in step with evals.py."""
    if client.has_dataset(dataset_name=DATASET):
        dataset = client.read_dataset(dataset_name=DATASET)
        existing = list(client.list_examples(dataset_id=dataset.id))
        # Always rewrite: the golden file is the source of truth, and metadata
        # or expectations may have changed even when the count has not.
        for example in existing:
            client.delete_example(example_id=example.id)
    else:
        dataset = client.create_dataset(
            dataset_name=DATASET,
            description="Payment agent routing scenarios: outcome + trajectory assertions.",
        )

    client.create_examples(
        dataset_id=dataset.id,
        inputs=[{"payload": s["input"], "resume": s["resume"], "name": s["name"]} for s in SCENARIOS],
        outputs=[
            {
                "ends_at": s["ends_at"],
                "must_not_visit": s["must_not_visit"],
                "expect_vop": s.get("expect_vop"),
                "expect_visits_once": s.get("expect_visits_once", []),
            }
            for s in SCENARIOS
        ],
        # The split groups cases by the guarantee they cover, so an experiment
        # can show that adversarial went red while core stayed green.
        # "protects" travels with the case, so a failure in the UI reads as a
        # broken rule rather than a node name.
        metadata=[{"id": s["id"], "protects": s["protects"]} for s in SCENARIOS],
    )

    # Splits are not accepted through metadata at creation time; passing
    # dataset_split there silently leaves every example in "base". They have
    # to be set with update_example(split=...) afterwards.
    by_id = {(e.metadata or {}).get("id"): e.id for e in client.list_examples(dataset_id=dataset.id)}
    for s in SCENARIOS:
        if s["id"] in by_id:
            client.update_example(example_id=by_id[s["id"]], split=s["split"])

    print(f"dataset '{DATASET}' synced with {len(SCENARIOS)} examples")


def target(inputs: dict) -> dict:
    """Run one scenario through the graph and report what it did."""
    thread = {"configurable": {"thread_id": f"ls-{abs(hash(inputs['name'])) % 10**8}"}}
    state = graph.invoke(inputs["payload"], thread)

    paused_unexpectedly = False
    if "__interrupt__" in state:
        if inputs["resume"] is None:
            paused_unexpectedly = True
        else:
            state = graph.invoke(Command(resume=inputs["resume"]), thread)

    return {
        "trail": state.get("trail", []),
        "vop_status": state.get("vop", {}).get("status"),
        "outcome": state.get("outcome"),
        "paused_unexpectedly": paused_unexpectedly,
    }


# ----------------------------------------------------------- evaluators
# Each takes the run's outputs and the dataset's reference outputs,
# and returns a score. 1 is a pass, 0 is a failure.

def ended_in_the_right_place(outputs: dict, reference_outputs: dict) -> dict:
    trail = outputs["trail"]
    ok = bool(trail) and trail[-1] == reference_outputs["ends_at"]
    return {"key": "ends_at", "score": int(ok),
            "comment": f"ended at {trail[-1] if trail else 'nothing'}"}


def avoided_forbidden_nodes(outputs: dict, reference_outputs: dict) -> dict:
    """The one that matters. Reaching execute_payment on a path that should
    never execute is the failure worth catching."""
    breached = [n for n in reference_outputs["must_not_visit"] if n in outputs["trail"]]
    return {"key": "no_forbidden_nodes", "score": int(not breached),
            "comment": f"reached {breached}" if breached else "clean"}


def payee_check_correct(outputs: dict, reference_outputs: dict) -> dict:
    expected = reference_outputs.get("expect_vop")
    if not expected:
        return {"key": "vop_status", "score": 1, "comment": "not asserted"}
    ok = outputs["vop_status"] == expected
    return {"key": "vop_status", "score": int(ok),
            "comment": f"got {outputs['vop_status']}, expected {expected}"}


def no_unexpected_pause(outputs: dict, reference_outputs: dict) -> dict:
    return {"key": "no_unexpected_pause", "score": int(not outputs["paused_unexpectedly"])}


def steps_not_replayed(outputs: dict, reference_outputs: dict) -> dict:
    """After a resume, earlier nodes must not run twice."""
    repeated = [n for n in reference_outputs.get("expect_visits_once", [])
                if outputs["trail"].count(n) != 1]
    return {"key": "no_replay", "score": int(not repeated),
            "comment": f"replayed {repeated}" if repeated else "clean"}


def write_results(results, experiment_name: str) -> None:
    """Record what the experiment found, so the demo page can show verified
    numbers instead of a claim. Written to data/eval-results.json."""
    import json
    from collections import defaultdict

    by_case, per_evaluator, per_split = [], defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    lookup = {s["name"]: s for s in SCENARIOS}

    for row in results:
        example = row["example"]
        scenario = lookup.get((example.inputs or {}).get("name"), {})
        scores = {r.key: r.score for r in row["evaluation_results"]["results"]}
        passed = all(bool(v) for v in scores.values())

        # Record the exact path each case walks, so the demo can match a live
        # run to the case that asserts that path.
        run_outputs = getattr(row.get("run"), "outputs", None) or {}

        by_case.append({
            "trail": run_outputs.get("trail", []),
            "outcome": run_outputs.get("outcome"),
            "input": scenario.get("input", {}),
            "id": scenario.get("id", "unknown"),
            "split": scenario.get("split", "unknown"),
            "name": scenario.get("name", ""),
            "protects": scenario.get("protects", ""),
            "passed": passed,
            "scores": scores,
        })
        for key, value in scores.items():
            per_evaluator[key][0] += int(bool(value))
            per_evaluator[key][1] += 1
        split = scenario.get("split", "unknown")
        per_split[split][0] += int(passed)
        per_split[split][1] += 1

    payload = {
        "experiment": experiment_name,
        "dataset": DATASET,
        "cases": sorted(by_case, key=lambda c: (c["split"], c["id"])),
        "per_evaluator": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(per_evaluator.items())},
        "per_split": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(per_split.items())},
        "passed": sum(1 for c in by_case if c["passed"]),
        "total": len(by_case),
    }
    out = Path(__file__).parent / "data" / "eval-results.json"
    out.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {out.name}: {payload['passed']}/{payload['total']} cases")


if __name__ == "__main__":
    sync_dataset()
    results = client.evaluate(
        target,
        data=DATASET,
        evaluators=[
            ended_in_the_right_place,
            avoided_forbidden_nodes,
            payee_check_correct,
            no_unexpected_pause,
            steps_not_replayed,
        ],
        experiment_prefix="payment-agent",
        max_concurrency=4,
    )
    name = getattr(results, "experiment_name", "unknown")
    write_results(results, name)
    print("\nexperiment:", name)
