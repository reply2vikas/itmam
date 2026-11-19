"""Registry of clinically approved message template versions (invariant I10).

Approvals and withdrawals are kept as history, so the engine can always answer
"was this exact version approved when it was sent?"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TemplateRef:
    pathway_id: str
    message_id: str
    language: str
    version: int


class TemplateRegistry:
    def __init__(self) -> None:
        # (template, when, approved?). `when=None` means approved before any case existed.
        self._history: list[tuple[TemplateRef, datetime | None, bool]] = []

    def approve(self, ref: TemplateRef, at: datetime | None = None) -> None:
        self._history.append((ref, at, True))

    def revoke(self, ref: TemplateRef, at: datetime) -> None:
        self._history.append((ref, at, False))

    def is_approved(self, ref: TemplateRef, at: datetime | None = None) -> bool:
        approved = False
        for entry, when, state in self._history:
            if entry != ref or (at is not None and when is not None and when > at):
                continue
            approved = state
        return approved

    def latest_approved(
        self, pathway_id: str, message_id: str, language: str, at: datetime | None = None
    ) -> TemplateRef | None:
        candidates = {
            ref
            for ref, _, _ in self._history
            if (ref.pathway_id, ref.message_id, ref.language) == (pathway_id, message_id, language)
        }
        approved = [ref for ref in candidates if self.is_approved(ref, at)]
        return max(approved, key=lambda ref: ref.version) if approved else None
