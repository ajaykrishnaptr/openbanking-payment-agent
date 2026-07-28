"""Evals for the semantic payee matcher.

Kept apart from evals.py on purpose. That suite is deterministic, free, and runs
on every change. This one calls a model, so it costs money, can disagree with
itself between runs, and is run deliberately.

Each case is a name pair and the verdict a careful payments reviewer would give.
Because the model is probabilistic, the bar is a pass rate rather than a clean
sweep, and the same suite runs against the stub so the comparison is visible.

Run:  VOP_PROVIDER=semantic .venv/bin/python evals_semantic.py
"""

import os
from pathlib import Path

for env_file in (".envrc", ".env"):
    path = Path(__file__).with_name(env_file)
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, value = line.replace("export ", "").split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"'))

from graph.vop import SemanticVoP, StubVoP

ACCOUNT = {"sort_code": "040668", "account_number": "00000871"}  # Pinguin Pfannkuchen GmbH

# expected verdict, and what the case is actually testing
CASES = [
    ("Pinguin Pfannkuchen GmbH", "MATCH", "exact match still short-circuits, no model call"),
    ("Pinguin Pfannkuchen Ltd", "PARTIAL", "same name, different legal form"),
    ("Pinguin Pfannkuchen Gesellschaft mbH", "PARTIAL", "GmbH written out in full"),
    ("Pinguin Pfannkuchen", "PARTIAL", "legal form omitted entirely"),
    ("PINGUIN PFANNKUCHEN GMBH", "MATCH", "case only"),
    ("Waffelwerk Bremen GmbH", "NO_MATCH", "a different real company"),
    ("Penguin Pancakes Ltd", "NO_MATCH", "translated name, other jurisdiction"),
    ("Pinguin Pfannkuchen Immobilien GmbH", "NO_MATCH", "same words, different group company"),
    ("Pfannkuchen Pinguin GmbH", "NO_MATCH", "words reversed, not the same registration"),
    ("Ajay Krishna", "NO_MATCH", "a person, not the company"),
]


def run(adapter, label: str) -> tuple[int, int]:
    passed = 0
    print(f"\n=== {label} ===")
    for name, expected, note in CASES:
        result = adapter.verify(name, ACCOUNT)
        ok = result.status == expected
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {result.status:18} expected {expected:18} {note}")
        if not ok and result.reason:
            print(f"      reason: {result.reason}")
    print(f"{passed}/{len(CASES)} ({100 * passed // len(CASES)}%)")
    return passed, len(CASES)


if __name__ == "__main__":
    stub_passed, total = run(StubVoP(), "deterministic stub (string ratio)")
    sem_passed, _ = run(SemanticVoP(), "semantic matcher (model judges the middle)")

    print(f"\nstub     {stub_passed}/{total}")
    print(f"semantic {sem_passed}/{total}")
    from graph import llm

    print("llm:", llm.status())
