"""Tracing (M6.2): one trace per live call or typed turn, with a span for
each caller turn, model call and tool. Off unless COLLECTWISE_TRACING is
"console" (spans printed as JSON) or "otlp" (sent to
OTEL_EXPORTER_OTLP_ENDPOINT, e.g. a local Jaeger).

Spans hold codes, counts and timings: never the caller's words, the model's
replies, tool arguments or results, amounts or account ids. FastAPI and the
Anthropic SDK make spans of their own; both are given PROVIDER, and every
span it makes records an exception's type only, never its message, which
could quote what a caller said.
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status

logger = logging.getLogger(__name__)

TRACING_MODES = ("console", "otlp")

# The SDK provider while tracing is on; None means every span is a no-op.
_provider: TracerProvider | None = None
_NO_OP = trace.NoOpTracerProvider()


class _SafeSpan(Span):
    """A recording span that keeps an exception's type and drops its message."""

    def __init__(self, span: Span) -> None:
        self._span = span

    def record_exception(self, exception, attributes=None, timestamp=None, escaped=False) -> None:
        self._span.add_event("exception", {"exception.type": type(exception).__name__}, timestamp)

    def set_status(self, status, description=None) -> None:
        code = status.status_code if isinstance(status, Status) else status
        self._span.set_status(Status(code))

    def end(self, end_time=None) -> None:
        self._span.end(end_time)

    def get_span_context(self):
        return self._span.get_span_context()

    def set_attributes(self, attributes) -> None:
        self._span.set_attributes(attributes)

    def set_attribute(self, key, value) -> None:
        self._span.set_attribute(key, value)

    def add_event(self, name, attributes=None, timestamp=None) -> None:
        self._span.add_event(name, attributes, timestamp)

    def add_link(self, context, attributes=None) -> None:
        self._span.add_link(context, attributes)

    def update_name(self, name) -> None:
        self._span.update_name(name)

    def is_recording(self) -> bool:
        return self._span.is_recording()


class _CurrentTracer(trace.Tracer):
    """Starts spans on whichever provider is current, so FastAPI and the
    Anthropic client, built once, follow tracing being switched on or off."""

    def __init__(self, *args: Any) -> None:
        self._args = args

    def start_span(self, name, context=None, kind=SpanKind.INTERNAL, attributes=None, links=None,
                   start_time=None, record_exception=True, set_status_on_exception=True) -> Span:
        if _provider is None:
            return _NO_OP.get_tracer(*self._args).start_span(name, context)
        span = _provider.get_tracer(*self._args).start_span(
            name, context, kind, attributes, links, start_time,
            record_exception=False, set_status_on_exception=False,
        )
        return _SafeSpan(span)

    @contextmanager
    def start_as_current_span(self, name, context=None, kind=SpanKind.INTERNAL, attributes=None, links=None,
                              start_time=None, record_exception=True, set_status_on_exception=True,
                              end_on_exit=True) -> Iterator[Span]:
        span = self.start_span(name, context, kind, attributes, links, start_time)
        with trace.use_span(span, end_on_exit=end_on_exit, record_exception=record_exception,
                            set_status_on_exception=set_status_on_exception) as current:
            yield current


class _CurrentProvider(trace.TracerProvider):
    def get_tracer(self, instrumenting_module_name, instrumenting_library_version=None, schema_url=None,
                   attributes=None) -> trace.Tracer:
        return _CurrentTracer(instrumenting_module_name, instrumenting_library_version, schema_url, attributes)


# Given to FastAPI, the Anthropic client and our own code.
PROVIDER = _CurrentProvider()


def configure(mode: str | None) -> TracerProvider | None:
    """Switch tracing on for "console" or "otlp"; anything else leaves it off."""
    if mode not in TRACING_MODES:
        if mode:
            logger.warning("Unknown COLLECTWISE_TRACING %r: tracing stays off", mode)
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": "collectwise"}))
    if mode == "console":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    install(provider)
    logger.info("Tracing on (%s)", mode)
    return provider


def install(provider: TracerProvider | None) -> TracerProvider | None:
    """Make `provider` current (None switches tracing off); returns the old one."""
    global _provider
    previous, _provider = _provider, provider
    return previous


def shutdown() -> None:
    """Send what is buffered and switch tracing off."""
    provider = install(None)
    if provider is not None:
        provider.shutdown()


def tracer() -> trace.Tracer:
    return PROVIDER.get_tracer("collectwise")


def attributes(**values: Any) -> Mapping[str, Any]:
    """Span attributes under the collectwise. prefix, leaving out unknowns."""
    return {f"collectwise.{key}": value for key, value in values.items() if value is not None}


@contextmanager
def span(name: str, **values: Any) -> Iterator[Span]:
    """A span around a block; an exception marks it as an error, by type."""
    with tracer().start_as_current_span(name, attributes=attributes(**values)) as current:
        yield current


def annotate(current: Span, **values: Any) -> None:
    current.set_attributes(attributes(**values))
