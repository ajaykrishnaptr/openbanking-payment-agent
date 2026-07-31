"""Tracing, in a format that is not tied to one vendor.

LangSmith already traces this agent, and it does it well. The reason to add
OpenTelemetry underneath is that the spans stop belonging to the tracing tool:
the same instrumentation goes to LangSmith today (it accepts OTLP) and to
anything else later by changing an environment variable rather than the code.

The model call in particular follows the OpenTelemetry GenAI semantic
conventions — gen_ai.request.model, gen_ai.usage.input_tokens, and so on — which
is what makes a span about an LLM legible to a backend that has never heard of
this application.

Three properties this file is built around:

  1. **Off unless configured.** No OTEL_EXPORTER_OTLP_ENDPOINT means every span
     here is a no-op. Local runs and the eval suite are unaffected and send
     nothing anywhere.
  2. **Optional at import.** If the opentelemetry packages are not installed at
     all, this degrades to no-ops rather than breaking the app. The deployment
     can drop them to save bundle size without a code change.
  3. **Never on the decision path.** A tracing failure must not fail a payment,
     so everything here swallows its own errors.

Configure with the standard OTLP variables, e.g. for LangSmith:

    OTEL_EXPORTER_OTLP_ENDPOINT=https://api.smith.langchain.com/otel
    OTEL_EXPORTER_OTLP_HEADERS="x-api-key=<langsmith key>,Langsmith-Project=payment-agent"
"""

from __future__ import annotations

import os
from contextlib import contextmanager

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "payment-agent")

_tracer = None
_state = "not started"


def enabled() -> bool:
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))


def setup() -> bool:
    """Start the exporter once. Returns whether tracing is on.

    Safe to call repeatedly, which matters on serverless: several cold instances
    each import the app, and each one needs its own provider.
    """
    global _tracer, _state

    if _tracer is not None:
        return True
    if not enabled():
        _state = "off (no OTEL_EXPORTER_OTLP_ENDPOINT)"
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(SERVICE_NAME)
        _state = "on"
        return True
    except Exception as exc:  # noqa: BLE001 — tracing must never break the app
        _state = f"off ({type(exc).__name__}: {exc})"
        print(f"[tracing] disabled: {exc}")
        return False


def describe() -> str:
    """What the internals panel should say about tracing."""
    return _state


@contextmanager
def span(name: str, **attributes):
    """A span, or nothing at all when tracing is off.

    Yields an object with .set (so callers can add attributes discovered while
    the block runs, like token counts) that does nothing in the off case. Call
    sites therefore read the same whether tracing is configured or not.
    """
    if _tracer is None and not setup():
        yield _NoSpan()
        return

    try:
        with _tracer.start_as_current_span(name) as raw:
            wrapper = _Span(raw)
            wrapper.set(**attributes)
            try:
                yield wrapper
            except Exception as exc:
                wrapper.error(exc)
                raise
    except Exception:  # noqa: BLE001 — a broken exporter is not a broken payment
        yield _NoSpan()


class _NoSpan:
    def set(self, **attributes) -> None:
        pass

    def error(self, exc: BaseException) -> None:
        pass


class _Span:
    def __init__(self, raw) -> None:
        self._raw = raw

    def set(self, **attributes) -> None:
        for key, value in attributes.items():
            if value is None:
                continue
            # OTel attribute names are dotted, which is not valid in a keyword
            # argument. A double underscore stands in for the dot, so
            # gen_ai__request__model means gen_ai.request.model. Single
            # underscores are left alone, because the conventions use them
            # inside a segment (gen_ai, input_tokens).
            self._raw.set_attribute(key.replace("__", "."), value)

    def error(self, exc: BaseException) -> None:
        try:
            from opentelemetry.trace import Status, StatusCode

            self._raw.record_exception(exc)
            self._raw.set_status(Status(StatusCode.ERROR, str(exc)))
        except Exception:  # noqa: BLE001
            pass
