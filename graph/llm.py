"""The model layer, with the switch that turns it off.

Every LLM call in this project goes through here, for three reasons:

  1. **It must be optional.** A payment agent that stops working because an API
     key expired is worse than one with no model in it. `available()` is checked
     before any call, and every caller has a deterministic fallback.
  2. **It must be bounded.** A public demo pays per call, so there is a per-process
     ceiling and a short timeout. Exceeding either disables the model rather than
     failing the request.
  3. **It must never decide.** Callers here return signals. Whether a payment may
     proceed stays in graph/policy.py, where the rule is readable and testable.
"""

from __future__ import annotations

import json
import os
import threading

from . import tracing

# Haiku 4.5, the cheapest tier that does structured output. The only question this
# model is ever asked is "are these two strings the same company", which is a
# classification and does not need a larger model; a public demo pays for every
# call, so it gets the small one. LLM_MODEL can override it, but nothing here
# depends on a bigger tier.
MODEL = os.getenv("LLM_MODEL", "claude-haiku-4-5")
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_SECONDS", "20"))
MAX_CALLS = int(os.getenv("LLM_MAX_CALLS", "500"))

_lock = threading.Lock()
_calls = 0
_client = None
_disabled_reason: str | None = None


def available() -> bool:
    """Whether a model call should be attempted at all."""
    if _disabled_reason:
        return False
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        return False
    return _calls < MAX_CALLS


def status() -> dict:
    return {
        "model": MODEL,
        "available": available(),
        "calls": _calls,
        "max_calls": MAX_CALLS,
        "disabled_reason": _disabled_reason,
    }


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=1)
    return _client


def judge(system: str, prompt: str, schema: dict, max_tokens: int = 2000) -> dict | None:
    """Ask the model one question and get a validated object back.

    Returns None whenever the model cannot answer for any reason: no key, budget
    spent, timeout, refusal, malformed output. A None means "no signal", and the
    caller falls back to its deterministic path. It never raises.
    """
    global _calls, _disabled_reason

    if not available():
        return None

    with _lock:
        if _calls >= MAX_CALLS:
            return None
        _calls += 1

    # One span per model call, named and attributed per the OpenTelemetry GenAI
    # conventions, so a backend can read it without knowing this application.
    # The span is a no-op unless an OTLP endpoint is configured.
    with tracing.span(
        f"chat {MODEL}",
        gen_ai__operation__name="chat",
        gen_ai__provider__name="anthropic",
        gen_ai__request__model=MODEL,
        gen_ai__request__max_tokens=max_tokens,
    ) as call:
        try:
            response = _get_client().messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system,
                # Structured output: the response is constrained to the schema,
                # so there is no prose to strip and no parsing guesswork.
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 — any failure means "no signal"
            _disabled_reason = f"{type(exc).__name__}: {exc}"[:200]
            call.set(error__type=type(exc).__name__)
            return None

        call.set(
            gen_ai__response__model=response.model,
            gen_ai__response__finish_reasons=(response.stop_reason,),
            gen_ai__usage__input_tokens=response.usage.input_tokens,
            gen_ai__usage__output_tokens=response.usage.output_tokens,
        )

        # The safety classifiers can decline; a normal outcome, not an error.
        if response.stop_reason == "refusal":
            return None

        # A truncated response is half a JSON object, which the parse below
        # rejects exactly as if the model had answered badly. Name it instead:
        # the cause is a budget too small for the schema, it will recur on every
        # call, so it disables the model with a message that says what to change.
        if response.stop_reason == "max_tokens":
            _disabled_reason = f"verdict did not fit in max_tokens={max_tokens}"
            call.set(error__type="max_tokens")
            return None

        for block in response.content:
            if block.type == "text":
                try:
                    return json.loads(block.text)
                except json.JSONDecodeError:
                    call.set(error__type="JSONDecodeError")
                    return None
        return None
