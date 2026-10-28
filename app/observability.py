"""Local agent observability: every Agents SDK span (agent run, model call, tool call, guardrail) is
forwarded to the job that produced it, so the UI can show a live trace even without the OpenAI dashboard."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from agents.tracing import TracingProcessor

log = logging.getLogger("app.trace")
Sink = Callable[[dict[str, Any]], None]


def _ms(start: str | None, end: str | None) -> float | None:
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000, 1)
    except (TypeError, ValueError):
        return None


class JobTraceRecorder(TracingProcessor):
    def __init__(self) -> None:
        self._sinks: dict[str, Sink] = {}  # group_id (job id) -> sink
        self._traces: dict[str, str] = {}  # trace_id -> group_id

    def register(self, job_id: str, sink: Sink) -> None:
        self._sinks[job_id] = sink

    def unregister(self, job_id: str) -> None:
        self._sinks.pop(job_id, None)
        self._traces = {t: g for t, g in self._traces.items() if g != job_id}

    def on_trace_start(self, trace) -> None:
        group = getattr(trace, "group_id", None) or (trace.export() or {}).get("group_id")
        if group:
            self._traces[trace.trace_id] = group

    def on_trace_end(self, trace) -> None:
        self._traces.pop(trace.trace_id, None)

    def on_span_start(self, span) -> None:
        pass

    def on_span_end(self, span) -> None:
        sink = self._sinks.get(self._traces.get(span.trace_id, ""))
        data = span.span_data
        kind = getattr(data, "type", "span")
        name = getattr(data, "name", None) or getattr(data, "model", None) or kind
        event: dict[str, Any] = {"type": "trace", "span": kind, "name": name,
                                 "ms": _ms(span.started_at, span.ended_at)}
        if kind == "guardrail":
            event["triggered"] = bool(getattr(data, "triggered", False))
        if kind == "response" and getattr(data, "response", None) is not None:
            usage = getattr(data.response, "usage", None)
            if usage:
                event["tokens"] = {"in": usage.input_tokens, "out": usage.output_tokens}
        if span.error:
            event["error"] = str(span.error.get("message", span.error))[:200]
        log.debug("span %s", event)
        if sink:
            sink(event)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass


recorder = JobTraceRecorder()
