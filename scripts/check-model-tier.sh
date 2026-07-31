#!/usr/bin/env bash
# Blocks an expensive Claude model string outside graph/llm.py, the one
# place a model tier is allowed to be chosen. Written after a debugging
# session found the opposite: a default that had drifted to Opus with no
# check anywhere to catch it before it shipped.
#
# Local (pre-commit): checks staged .py files.
# CI (--all): checks every tracked .py file, so drift already merged is
# caught too, not just new commits.
set -euo pipefail

PATTERN='claude-(opus|sonnet|fable|mythos)'

if [ "${1:-}" = "--all" ]; then
  files=$(git ls-files '*.py')
else
  files=$(git diff --cached --name-only --diff-filter=ACM -- '*.py')
fi

hits=$(echo "$files" \
  | grep -v '^graph/llm\.py$' \
  | xargs -r grep -nE "$PATTERN" 2>/dev/null || true)

if [ -n "$hits" ]; then
  echo "Blocked: an expensive model string appears outside graph/llm.py."
  echo
  echo "$hits"
  echo
  echo "graph/llm.py is the only place a model tier is chosen. If this is intentional, move it there."
  exit 1
fi
