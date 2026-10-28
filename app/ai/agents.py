"""Agent definitions (OpenAI Agents SDK) and a resilient runner around them.

Orchestration is done in code (app/pipeline.py), not by an LLM router: the order of the steps is known in
advance, so code is cheaper, faster and deterministic. Agents are used where judgement is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agents import (
    Agent,
    GuardrailFunctionOutput,
    ModelBehaviorError,
    ModelRetryBackoffSettings,
    ModelRetrySettings,
    ModelSettings,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    RunContextWrapper,
    Runner,
    function_tool,
    output_guardrail,
    retry_policies,
)
from openai.types.shared import Reasoning
from pydantic import ValidationError

from app.ai import prompts
from app.ai.schemas import DocumentAnalysis, JudgeReport, TranslationBatch
from app.config import Settings
from app.languages import LANGUAGES, Language
from app.models import Analysis, Usage

log = logging.getLogger(__name__)


@dataclass
class TranslationContext:
    """Shared, typed context handed to every agent run (never sent to the model as such)."""

    target: Language
    analysis: Analysis | None = None
    expected_ids: list[str] = field(default_factory=list)
    fit_checker: Callable[[str, str], dict[str, Any]] | None = None
    fit_checks: int = 0


def _protected(a: Analysis | None) -> str:
    terms = sorted({t.term for t in a.protected_terms}) if a else []
    return ", ".join(f'"{t}"' for t in terms) or "(none detected)"


def _glossary(a: Analysis | None) -> str:
    return "; ".join(f"{j.term} -> {j.translation}" for j in a.jargon) if a and a.jargon else "(none)"


def _fmt(template: str, ctx: TranslationContext) -> str:
    a = ctx.analysis
    src = LANGUAGES.get(a.source_language) if a else None
    return template.format(
        target_name=ctx.target.name, target_code=ctx.target.code,
        source_name=(src.name if src else a.source_language_name) if a else "unknown",
        domain=a.domain if a else "general documents", summary=a.summary if a else "",
        tone=a.tone if a else "neutral", style_notes=a.style_notes if a else "",
        protected=_protected(a), glossary=_glossary(a),
    )


def _instructions(template: str):
    def build(run_ctx: RunContextWrapper[TranslationContext], _agent: Agent) -> str:
        return _fmt(template, run_ctx.context)
    return build


@output_guardrail(name="one_translation_per_block")
async def ids_match(ctx: RunContextWrapper[TranslationContext], _agent: Agent, output: Any) -> GuardrailFunctionOutput:
    """Trip when the model skipped, invented or duplicated block ids (cheap and catches real failures)."""
    got = [t.id for t in output.translations] if isinstance(output, TranslationBatch) else \
        [b.id for b in output.blocks]
    expected = ctx.context.expected_ids
    missing = sorted(set(expected) - set(got))
    extra = sorted(set(got) - set(expected))
    empty = [t.id for t in getattr(output, "translations", []) if not t.text.strip()]
    bad = bool(missing or extra or empty or len(got) != len(set(got)))
    return GuardrailFunctionOutput(output_info={"missing": missing, "extra": extra, "empty": empty},
                                   tripwire_triggered=bad)


@function_tool
def check_fit(ctx: RunContextWrapper[TranslationContext], block_id: str, text: str) -> str:
    """Check whether a candidate translation fits the block's box in the PDF layout.

    Args:
        block_id: The id of the block the text is for.
        text: The candidate translated text (may contain <b>/<i> tags).
    """
    ctx.context.fit_checks += 1
    if ctx.context.fit_checker is None:
        return json.dumps({"fits": True})
    return json.dumps(ctx.context.fit_checker(block_id, text))


class AgentFactory:
    def __init__(self, settings: Settings):
        self.s = settings
        self.pseudo = settings.backend == "pseudo"

    def _model(self, name: str, role: str):
        if self.pseudo:
            from app.ai.pseudo import PseudoModel
            return PseudoModel(role)
        return name

    def _settings(self, model: str, effort: str) -> ModelSettings:
        retry = ModelRetrySettings(
            max_retries=self.s.llm_max_retries,
            backoff=ModelRetryBackoffSettings(initial_delay=1.0, max_delay=20.0, multiplier=2.0, jitter=True),
            policy=retry_policies.any(retry_policies.network_error(), retry_policies.retry_after(),
                                      retry_policies.http_status([408, 409, 429, 500, 502, 503, 504])),
        )
        reasoning = Reasoning(effort=effort) if model.startswith(("gpt-5", "o")) and not self.pseudo else None
        return ModelSettings(reasoning=reasoning, retry=retry, timeout=self.s.llm_timeout_seconds,
                             include_usage=True)

    def analyst(self) -> Agent:
        return Agent(name="Analyst", instructions=_instructions(prompts.ANALYST),
                     model=self._model(self.s.analyst_model, "analyst"), output_type=DocumentAnalysis,
                     model_settings=self._settings(self.s.analyst_model, self.s.reasoning_effort))

    def translator(self) -> Agent:
        return Agent(name="Translator", instructions=_instructions(prompts.TRANSLATOR),
                     model=self._model(self.s.translator_model, "translator"), output_type=TranslationBatch,
                     output_guardrails=[ids_match],
                     model_settings=self._settings(self.s.translator_model, self.s.reasoning_effort))

    def refiner(self) -> Agent:
        return Agent(name="Refiner", instructions=_instructions(prompts.REFINER), tools=[check_fit],
                     model=self._model(self.s.translator_model, "refiner"), output_type=TranslationBatch,
                     output_guardrails=[ids_match],
                     model_settings=self._settings(self.s.translator_model, self.s.reasoning_effort))

    def escalation(self) -> Agent:
        return Agent(name="Escalation reviser", instructions=_instructions(prompts.ESCALATION), tools=[check_fit],
                     model=self._model(self.s.escalation_model, "refiner"), output_type=TranslationBatch,
                     output_guardrails=[ids_match],
                     model_settings=self._settings(self.s.escalation_model, self.s.judge_reasoning_effort))

    def judge(self) -> Agent:
        return Agent(name="Judge (MQM)", instructions=_instructions(prompts.JUDGE),
                     model=self._model(self.s.judge_model, "judge"), output_type=JudgeReport,
                     model_settings=self._settings(self.s.judge_model, self.s.judge_reasoning_effort))


class AgentRunError(RuntimeError):
    pass


async def run_agent(agent: Agent, payload: Any, ctx: TranslationContext, usage: dict[str, Usage],
                    *, images: list[str] | None = None, max_turns: int = 4, attempts: int = 2,
                    timeout: float = 180.0, group_id: str | None = None):
    """Run an agent with structured output, app-level retries and usage accounting.

    Transport errors (429, 5xx, timeouts) are retried by the SDK itself (ModelRetrySettings). Here we retry
    the failures the model is responsible for: invalid JSON, a tripped guardrail, too many tool turns.
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    note = ""
    last: Exception | None = None
    for attempt in range(attempts):
        content: list[dict[str, Any]] = [{"type": "input_text", "text": text + note}]
        for img in images or []:
            content.append({"type": "input_image", "image_url": img, "detail": "high"})
        try:
            result = await asyncio.wait_for(
                Runner.run(agent, [{"role": "user", "content": content}], context=ctx, max_turns=max_turns,
                           run_config=RunConfig(workflow_name="pdf-translation", group_id=group_id)),
                timeout=timeout)
            u = result.context_wrapper.usage
            bucket = usage.setdefault(agent.name, Usage())
            bucket.add(Usage(requests=u.requests, input_tokens=u.input_tokens, output_tokens=u.output_tokens))
            return result.final_output
        except OutputGuardrailTripwireTriggered as exc:
            info = exc.guardrail_result.output.output_info
            note = (f"\n\nYour previous answer was rejected: {json.dumps(info)}. Return exactly one non-empty "
                    f"translation for each of these ids: {ctx.expected_ids}")
            last = exc
        except (ModelBehaviorError, ValidationError, asyncio.TimeoutError) as exc:
            note = "\n\nYour previous answer was not valid. Answer with the required JSON only."
            last = exc
        except Exception as exc:  # MaxTurnsExceeded, provider errors after SDK retries...
            last = exc
            if "MaxTurns" not in type(exc).__name__:
                break
        log.warning("%s attempt %d failed: %s", agent.name, attempt + 1, last)
    raise AgentRunError(f"{agent.name} failed: {type(last).__name__}: {last}") from last
