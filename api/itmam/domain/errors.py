"""Why a command was refused. Every refusal is also logged by the engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class CommandRejected(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Rejection:
    at: datetime
    command: str
    target: str | None  # the case, suggestion or result the command was about
    actor_kind: str
    actor_id: str
    code: str
    message: str
