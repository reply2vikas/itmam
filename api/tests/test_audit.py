"""The audit trail is append-only and tamper-evident (invariant I7)."""

import dataclasses

from itmam.domain import check_all
from itmam.domain.audit import canonical, fingerprint

from support import make_engine, open_case


def test_every_command_leaves_a_verifiable_trail() -> None:
    engine, _ = make_engine()
    open_case(engine)
    open_case(engine, result_id="TEST-R-2", first=2)
    assert len(engine.audit) >= 2
    assert engine.audit.verify() == []


def test_editing_history_is_detected() -> None:
    engine, _ = make_engine()
    open_case(engine)
    open_case(engine, result_id="TEST-R-2", first=2)
    events = engine.audit._events
    events[0] = dataclasses.replace(events[0], details={**events[0].details, "pathway": "edited"})
    problems = engine.audit.verify()
    assert problems and "content does not match" in problems[0]
    assert any(v.invariant == "I7" for v in check_all(engine))


def test_fingerprints_are_stable_and_order_independent() -> None:
    assert fingerprint({"b": {2, 1}, "a": [1]}) == fingerprint({"a": [1], "b": {1, 2}})
    assert canonical({"z": 1, "a": 2}) == {"a": 2, "z": 1}
