"""Live Claude provider.

Only used when LLM_PROVIDER=anthropic. The test suite never reaches this class -
it runs on the stub - so the suite stays free, offline and deterministic. What
this provider returns is fed through the same guardrail layer regardless.
"""
from __future__ import annotations

import anthropic

from app.config import settings
from app.llm.base import LLMResult


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None) -> None:
        # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth
        # login` profile from the environment.
        self.client = anthropic.Anthropic()
        self.model = model or settings.llm_model

    def complete(self, system: str, prompt: str, max_tokens: int = 4000) -> LLMResult:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            # Explaining pre-computed figures is a light task; low effort keeps
            # latency and cost down without touching answer quality.
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            # If a safety classifier declines, the API re-runs the same request
            # on a fallback model inside this call instead of returning nothing.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

        if response.stop_reason == "refusal":
            return LLMResult(
                text="",
                model=response.model,
                refused=True,
                stop_reason="refusal",
                meta={"stop_details": getattr(response, "stop_details", None)},
            )

        text = "".join(b.text for b in response.content if b.type == "text")
        return LLMResult(
            text=text,
            model=response.model,
            stop_reason=response.stop_reason,
            meta={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        )
