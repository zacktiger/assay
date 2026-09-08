"""Output validation for model responses.

This is the part of the system that makes an LLM feature testable. Whatever the
provider returns is treated as untrusted text and checked against the figures
the application computed. A response that fails is not shown to the user - the
endpoint substitutes a deterministic template - and the violation is reported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from app.llm.prompts import SYSTEM_CANARY

# Phrasings that promise certainty. Matched on word boundaries so "no risk of
# lock-in" style false positives stay rare; the eval suite pins the exact set.
_GUARANTEE_PATTERNS = [
    r"\bguarantee(?:d|s|ing)?\b",
    r"\bassured\b",
    r"\brisk[- ]free\b",
    r"\bno risk\b",
    r"\bzero risk\b",
    r"\bcannot lose\b",
    r"\bcan't lose\b",
    r"\bwill definitely\b",
    r"\bsure[- ]shot\b",
]

# Any percentage figure in the text, e.g. "12%", "12.5 %", "7 percent".
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", re.IGNORECASE)

VIOLATION_GUARANTEE = "guaranteed_return_claim"
VIOLATION_UNSUPPORTED_NUMBER = "unsupported_numeric_claim"
VIOLATION_PROMPT_LEAK = "system_prompt_leak"
VIOLATION_EMPTY = "empty_response"


@dataclass
class GuardrailReport:
    violations: list[str]

    @property
    def ok(self) -> bool:
        return not self.violations


def check(text: str, allowed_percentages: set[Decimal] | None = None) -> GuardrailReport:
    """Validate a model response.

    `allowed_percentages` is the set of figures the application actually gave
    the model. Any other percentage in the text is an invented number.
    """
    violations: list[str] = []

    if not text or not text.strip():
        return GuardrailReport([VIOLATION_EMPTY])

    lowered = text.lower()
    if any(re.search(p, lowered) for p in _GUARANTEE_PATTERNS):
        violations.append(VIOLATION_GUARANTEE)

    if SYSTEM_CANARY.lower() in lowered or "policy reference" in lowered:
        violations.append(VIOLATION_PROMPT_LEAK)

    if allowed_percentages is not None:
        allowed = {Decimal(str(p)).normalize() for p in allowed_percentages}
        for raw in _PERCENT_RE.findall(text):
            if Decimal(raw).normalize() not in allowed:
                violations.append(VIOLATION_UNSUPPORTED_NUMBER)
                break

    return GuardrailReport(violations)
