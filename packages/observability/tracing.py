"""Correlation IDs and lightweight spans (spec section 62).

Every background job and audit event carries a correlation ID, so one user
action can be followed from the HTTP request through the queue into the audit
trail. That is the whole point: without it, "why did this match happen?" cannot
be answered from logs.

No vendor SDK is imported. A deployment that wants OpenTelemetry wires an
exporter to :func:`set_span_sink`.
"""

from __future__ import annotations

import contextvars
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

__all__ = ["Span", "correlation_id", "current_correlation_id", "set_span_sink", "span"]

_correlation: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "recon_correlation_id", default=None
)
_span_stack: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "recon_span_stack", default=()
)


@dataclass(frozen=True, slots=True)
class Span:
    name: str
    correlation_id: uuid.UUID
    duration_seconds: float
    parents: tuple[str, ...] = ()
    attributes: dict[str, str] = field(default_factory=dict)
    error: str | None = None


_sink: Callable[[Span], None] | None = None


def set_span_sink(sink: Callable[[Span], None] | None) -> None:
    """Install a span exporter. ``None`` discards spans."""
    global _sink
    _sink = sink


def current_correlation_id() -> uuid.UUID | None:
    return _correlation.get()


@contextmanager
def correlation_id(value: uuid.UUID | None = None) -> Iterator[uuid.UUID]:
    """Bind a correlation ID for the duration of the block."""
    identifier = value or uuid.uuid4()
    token = _correlation.set(identifier)
    try:
        yield identifier
    finally:
        _correlation.reset(token)


@contextmanager
def span(name: str, **attributes: str) -> Iterator[None]:
    """Time a named operation and hand it to the sink."""
    parents = _span_stack.get()
    token = _span_stack.set((*parents, name))
    started = time.perf_counter()
    error: str | None = None
    try:
        yield
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _span_stack.reset(token)
        if _sink is not None:
            _sink(
                Span(
                    name=name,
                    correlation_id=_correlation.get() or uuid.UUID(int=0),
                    duration_seconds=time.perf_counter() - started,
                    parents=parents,
                    attributes=attributes,
                    error=error,
                )
            )
