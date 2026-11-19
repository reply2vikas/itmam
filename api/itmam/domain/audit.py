"""Append-only audit log with a SHA-256 hash chain, so any later edit is detectable."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

from itmam.domain.model import Actor

GENESIS = "0" * 64


_PLAIN = frozenset({str, int, float, bool, type(None)})
_FIELDS: dict[type, tuple[str, ...] | None] = {}


def _field_names(kind: type, value: Any) -> tuple[str, ...] | None:
    if kind not in _FIELDS:
        is_record = dataclasses.is_dataclass(value) and not isinstance(value, type)
        _FIELDS[kind] = tuple(f.name for f in dataclasses.fields(value)) if is_record else None
    return _FIELDS[kind]


def canonical(value: Any) -> Any:
    """Turn domain objects into plain JSON-ready values with a stable order."""
    kind = type(value)
    if kind in _PLAIN:
        return value
    if isinstance(value, Enum):
        return value.value
    names = _field_names(kind, value)
    if names is not None:
        return {name: canonical(getattr(value, name)) for name in names}
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in value.items()}  # json sorts keys later
    if isinstance(value, set | frozenset):
        return sorted(canonical(v) for v in value)
    if isinstance(value, list | tuple):
        return [canonical(v) for v in value]
    return value


def _digest(plain: Any) -> str:
    text = json.dumps(plain, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(value: Any) -> str:
    return _digest(canonical(value))


def case_fingerprint(case: Any) -> str:
    """Hash of everything about a case except the stored fingerprint itself (invariant I9)."""
    data = canonical(case)
    data.pop("closed_fingerprint", None)
    return _digest(data)


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    at: str
    case_id: str | None
    actor_kind: str
    actor_id: str
    actor_role: str
    action: str
    details: dict[str, Any]
    prev_hash: str
    hash: str

    def body(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "case_id": self.case_id,
            "actor_kind": self.actor_kind,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "action": self.action,
            "details": self.details,
        }


def _hash(prev_hash: str, body: dict[str, Any]) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + text).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def append(
        self,
        at: datetime,
        case_id: str | None,
        actor: Actor,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        prev_hash = self._events[-1].hash if self._events else GENESIS
        body = {
            "seq": len(self._events) + 1,
            "at": at.isoformat(),
            "case_id": case_id,
            "actor_kind": actor.kind.value,
            "actor_id": actor.id,
            "actor_role": actor.role,
            "action": action,
            "details": canonical(details or {}),
        }
        event = AuditEvent(**body, prev_hash=prev_hash, hash=_hash(prev_hash, body))
        self._events.append(event)
        return event

    def verify(self) -> list[str]:
        """Return a list of problems; empty means every link in the chain checks out."""
        problems: list[str] = []
        prev_hash = GENESIS
        for index, event in enumerate(self._events, start=1):
            if event.seq != index:
                problems.append(f"event {index}: sequence number is {event.seq}")
            if event.prev_hash != prev_hash:
                problems.append(f"event {index}: previous hash does not match")
            if _hash(event.prev_hash, event.body()) != event.hash:
                problems.append(f"event {index}: content does not match its hash")
            prev_hash = event.hash
        return problems
