"""Guard agent: AI security for everything that goes to or comes from a language model.

Three checks, all deterministic so they can be tested and audited:

- Prompt injection: patient text that tries to instruct the model ("ignore previous
  instructions", "you are now", "system prompt"...), in English and Arabic. Flagged text never
  reaches the model; the rules handle it and a person sees the flag.
- Personal data: Emirates ID numbers, phone numbers and email addresses are redacted before any
  text leaves for the model (data minimisation).
- Size: over-long input is cut, so a single reply cannot flood the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_INPUT = 500

INJECTION = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|rules|messages)"
    r"|disregard (the |all )?(instructions|rules)"
    r"|you are now|act as (an? )?(admin|doctor|system|developer)"
    r"|system prompt|developer mode|jailbreak"
    r"|(close|delete|approve|cancel) (this|my|the) case"
    r"|mark (me|this|it) as (done|closed|complete)"
    r"|تجاهل (كل |جميع )?(التعليمات|الأوامر)|أغلق (الملف|الحالة)",
    re.IGNORECASE,
)
EMIRATES_ID = re.compile(r"\b784[-\s]?\d{4}[-\s]?\d{7}[-\s]?\d\b")
PHONE = re.compile(r"(?<!\d)(?:\+?971|00971|0)[\s-]?5\d(?:[\s-]?\d){7}(?!\d)")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


@dataclass
class Screened:
    """The result of screening one piece of text."""

    text: str  # safe to send to the model (redacted, trimmed)
    injection: bool = False
    redactions: list[str] = field(default_factory=list)
    trimmed: bool = False

    @property
    def allowed_to_model(self) -> bool:
        return not self.injection

    def report(self) -> list[str]:
        notes = []
        if self.injection:
            notes.append("possible prompt injection: kept away from the model")
        if self.redactions:
            notes.append("redacted before the model: " + ", ".join(self.redactions))
        if self.trimmed:
            notes.append(f"trimmed to {MAX_INPUT} characters")
        return notes


def screen(text: str) -> Screened:
    trimmed = len(text) > MAX_INPUT
    safe = text[:MAX_INPUT]
    redactions = []
    for label, pattern, token in (
        ("Emirates ID", EMIRATES_ID, "[ID]"),
        ("phone number", PHONE, "[PHONE]"),
        ("email", EMAIL, "[EMAIL]"),
    ):
        safe, count = pattern.subn(token, safe)
        if count:
            redactions.append(label)
    return Screened(safe, bool(INJECTION.search(text)), redactions, trimmed)
