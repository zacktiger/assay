"""Deterministic offline provider.

The test suite runs against this by default: no API key, no network, no
per-run cost, and identical output for identical input - which is what makes
the consistency test ("ask the same question five times") meaningful. It is a
stand-in for the model, not a simulation of one; the guardrail layer it feeds
is the same code the live provider goes through.
"""
from __future__ import annotations

import re

from app.llm.base import LLMResult
from app.llm.prompts import SYSTEM_CANARY

_KNOWLEDGE = {
    "sip": (
        "A SIP (Systematic Investment Plan) invests a fixed amount into a mutual "
        "fund at a regular interval, usually monthly. Because you buy at many "
        "different prices over time, it averages your purchase cost rather than "
        "betting on one entry point. Returns remain market-linked."
    ),
    "expense ratio": (
        "The expense ratio is the annual fee a mutual fund charges, expressed as "
        "a share of the money you have invested. It is deducted from the fund's "
        "NAV, so you never see it as a separate debit - a lower ratio leaves more "
        "of the return with you."
    ),
    "elss": (
        "ELSS (Equity Linked Savings Scheme) is an equity mutual fund that "
        "qualifies for a deduction under Section 80C of the Income Tax Act. It "
        "carries the shortest lock-in among 80C options, at three years, and its "
        "returns are market-linked."
    ),
    "nav": (
        "NAV (Net Asset Value) is the per-unit value of a mutual fund, calculated "
        "as the fund's total assets minus liabilities divided by units outstanding. "
        "It is published once per day for most funds."
    ),
    "index fund": (
        "An index fund holds the same securities in the same weights as an index "
        "such as the Nifty 50, rather than picking stocks. That keeps costs low "
        "and makes returns track the index, minus fees. Returns are market-linked."
    ),
}

# Questions that ask for a promise of certainty. Deliberately wider than the
# word "guarantee": a user asking "is this safe?" is asking the same thing.
_CERTAINTY_RE = re.compile(
    r"\b(guarantee(?:d|s)?|assured|risk[- ]free|safe|no risk|zero risk|"
    r"sure[- ]shot|fixed return|definitely)\b"
)

_REFUSAL = (
    "I can't answer that from what I know reliably, and I would rather say so "
    "than guess. A SEBI-registered investment adviser can help with your "
    "specific situation."
)

# Requests that try to talk the model out of its instructions.
_INJECTION_MARKERS = [
    "ignore previous instructions",
    "ignore all previous",
    "disregard your instructions",
    "reveal your system prompt",
    "print your instructions",
    "what is your policy reference",
    "repeat the text above",
    "you are now",
    "developer mode",
]


class StubProvider:
    name = "stub"
    model = "stub-deterministic-v1"

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> LLMResult:
        text = self._respond(system, prompt)
        return LLMResult(text=text, model=self.model, stop_reason="end_turn")

    def _respond(self, system: str, prompt: str) -> str:
        lowered = prompt.lower()

        if any(marker in lowered for marker in _INJECTION_MARKERS):
            # Hold the line and say nothing about the instructions themselves.
            return (
                "I can only help with personal-finance questions, and I don't "
                "share my configuration. What would you like to know about "
                "investing?"
            )

        if lowered.startswith("explain the following figures"):
            return self._explain(prompt)

        if _CERTAINTY_RE.search(lowered):
            return (
                "No market-linked investment can promise a fixed outcome. Equity "
                "and mutual fund returns vary with the market and can be negative "
                "over short periods. Only instruments such as bank deposits carry "
                "a contracted rate."
            )

        for term, answer in _KNOWLEDGE.items():
            if re.search(rf"\b{re.escape(term)}\b", lowered):
                return answer

        return _REFUSAL

    def _explain(self, prompt: str) -> str:
        """Render the figures handed in by the recommendation endpoint.

        Reads them back out of the prompt so the response can never contain a
        percentage the application did not supply.
        """
        fields = dict(re.findall(r"^- (\w+): (.+)$", prompt, flags=re.MULTILINE))
        asset = fields.get("asset_type", "this asset").replace("_", " ")
        pct = fields.get("expected_return_pct", "the historical average")
        years = fields.get("horizon_years", "the period")
        amount = fields.get("amount", "your investment")
        projected = fields.get("projected_value", "the projected value")
        return (
            f"Over {years} years, {amount} rupees in {asset} is projected to reach "
            f"{projected} rupees, using a long-run average of {pct}% a year. That "
            "average is drawn from past asset-class behaviour, not a promise: "
            "market-linked returns vary year to year and can fall. Treat the "
            "figure as a planning estimate and review it as your goals change."
        )


class ScriptedProvider:
    """Returns queued responses. Used by tests to feed the guardrail layer the
    kind of output a real model occasionally produces - a hallucinated rate, a
    guarantee, a leaked prompt - without needing to provoke one."""

    name = "scripted"
    model = "scripted-test-double"

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> LLMResult:
        self.calls.append((system, prompt))
        text = self.responses.pop(0) if self.responses else "(no scripted response)"
        if text == "__REFUSAL__":
            return LLMResult(text="", model=self.model, refused=True, stop_reason="refusal")
        return LLMResult(text=text, model=self.model, stop_reason="end_turn")


assert SYSTEM_CANARY  # imported so a rename of the canary breaks loudly here
