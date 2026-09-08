"""Provider interface shared by the live and stub backends."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class LLMResult:
    text: str
    model: str
    refused: bool = False
    stop_reason: str | None = None
    meta: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str

    def complete(self, system: str, prompt: str, max_tokens: int = 1024) -> LLMResult: ...
