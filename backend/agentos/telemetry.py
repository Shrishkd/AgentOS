"""OTLP export is optional. Span attributes and log fields are application metadata only."""

import logging
import os
from contextlib import contextmanager
from time import monotonic

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)
propagator = TraceContextTextMapPropagator()
_provider = None


@contextmanager
def tracing(service):
    global _provider
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if not endpoint:
        yield
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service}), shutdown_on_exit=False)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=3)))
    _provider = provider
    try:
        yield
    finally:
        _provider = None
        provider.shutdown()


def traceparent():
    carrier = {}
    propagator.inject(carrier)
    return carrier.get("traceparent")


def failed(span, code):
    span.set_attribute("error.type", code)
    span.set_status(trace.Status(trace.StatusCode.ERROR))


@contextmanager
def execution_span(name, *, parent=None, root=False, **fields):
    context = propagator.extract({"traceparent": parent}) if parent else Context() if root else None
    tracer = (_provider or trace.NoOpTracerProvider()).get_tracer("agentos.execution")
    start = monotonic()
    with tracer.start_as_current_span(name, context=context, attributes=fields,
                                      record_exception=False, set_status_on_exception=False) as span:
        outcome = "completed"
        try:
            yield span
        except BaseException as exc:
            outcome = type(exc).__name__
            if outcome != "RetryScheduled":
                failed(span, getattr(exc, "code", outcome))
            raise
        finally:
            sc = span.get_span_context()
            logger.info(name, extra={**fields, "outcome": outcome,
                                     "trace_id": format(sc.trace_id, "032x") if sc.is_valid else None,
                                     "duration_ms": round((monotonic() - start) * 1000, 2)})
