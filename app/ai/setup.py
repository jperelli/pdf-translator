"""Wire the Agents SDK to our settings: API key, tracing export and the local trace recorder."""

from __future__ import annotations

from agents import set_default_openai_key, set_trace_processors, set_tracing_export_api_key
from agents.tracing import add_trace_processor
from agents.tracing.processors import default_processor

from app.config import Settings
from app.observability import recorder

_done = False


def configure_sdk(settings: Settings) -> None:
    global _done
    if _done:
        return
    _done = True
    if settings.backend == "openai" and settings.openai_api_key:
        set_default_openai_key(settings.openai_api_key, use_for_tracing=settings.openai_tracing)
        if settings.openai_tracing:
            set_tracing_export_api_key(settings.openai_api_key)
            set_trace_processors([default_processor(), recorder])
            return
    # no key / tracing off: keep spans local only (shown in the UI), never try to export them
    set_trace_processors([recorder])


__all__ = ["configure_sdk", "add_trace_processor"]
