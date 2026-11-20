"""Evidence ladder, invariants I11-I15, stuck points, the couple case and simulated Malaffi."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from itmam.app import app
from itmam.domain import CommandRejected, check_all
from itmam.domain.evidence import ladder_problems, source_of, tier_of
from itmam.insights import delay_attribution, leakage, pre_breach, stuck_point
from itmam.sandbox import new_sandbox

MARIAM, OMAR, COUPLE = "C-000001", "C-000002", "C-000005"


def walk(box, case_id: str) -> None:  # type: ignore[no-untyped-def]
    for _ in range(14):
        message = box.next_step(case_id)
        if "Skip ahead" in message:
            box.advance(7)
        if not box.case(case_id).is_open:
            return
    raise AssertionError("case did not close")


def test_evidence_tiers() -> None:
    assert tier_of("malaffi:ENC-1") == 3
    assert tier_of("self:patient says done") == 0
    assert tier_of("DOC-REPORT") == 2 and source_of("DOC-REPORT") == "staff"
    assert tier_of("ai: summary") is None
    assert tier_of("http://x") == 2  # an unknown prefix is not a source


def test_ladder_rules() -> None:
    assert ladder_problems({"a": "emr:1"}, "success") == []
    assert ladder_problems({"a": "navigator:said so"}, "success")
    assert ladder_problems({"a": "navigator:said so"}, "neutral") == []
    assert ladder_problems({"a": "ai:x"}, "neutral")


def test_success_closure_refuses_patient_word() -> None:
    box = new_sandbox()
    while box.case(MARIAM).stage.value != "outcome_recorded":
        if "Skip ahead" in box.next_step(MARIAM):
            box.advance(7)
    c = box.case(MARIAM)
    staff = box.staff(c)
    due = (box.clock.now() + timedelta(days=1000)).date().isoformat()
    with pytest.raises(CommandRejected) as refused:
        box.engine.propose_closure(
            staff.coordinator(c.owner.org),
            MARIAM,
            "surveillance",
            {"outcome_record": "self:I had it done", "next_due_date": due},
        )
    assert refused.value.code == "evidence_too_weak"


def test_ai_text_is_never_evidence() -> None:
    box = new_sandbox()
    c = box.case(OMAR)
    with pytest.raises(CommandRejected) as refused:
        box.engine.propose_closure(
            box.staff(c).coordinator(c.owner.org),
            OMAR,
            "not_actionable",
            {"clinical_review_note": "ai: no follow-up needed"},
        )
    assert refused.value.code == "evidence_too_weak"


def test_cases_close_with_simulated_malaffi_evidence() -> None:
    box = new_sandbox()
    walk(box, MARIAM)
    walk(box, COUPLE)
    assert "malaffi:" in box.case(MARIAM).closure.evidence["outcome_record"]  # type: ignore[union-attr]
    assert box.case(COUPLE).closure.counts_as == "success"  # type: ignore[union-attr]
    assert check_all(box.engine) == []


def test_couple_needs_both_consents() -> None:
    box = new_sandbox()
    c = box.case(COUPLE)
    assert len(c.subjects) == 2
    with pytest.raises(CommandRejected):
        box.engine.book_appointment(
            box.staff(c).coordinator(c.owner.org), COUPLE, box.clock.now() + timedelta(days=2), True
        )
    assert "consent" in box.next_step(COUPLE)
    assert "both consents" in box.next_step(COUPLE)


def test_stuck_points_and_ledger() -> None:
    box = new_sandbox()
    now = box.clock.now()
    assert stuck_point(box.engine, box.case(OMAR), now).code == "not_understood"  # type: ignore[union-attr]
    assert stuck_point(box.engine, box.case("C-000004"), now).code == "no_show"  # type: ignore[union-attr]
    assert sum(leakage(box.engine, now).values()) == 5
    box.advance(4)
    assert any(x["case_id"] == OMAR for x in pre_breach(box.engine, box.clock.now())) or (
        stuck_point(box.engine, box.case(OMAR), box.clock.now()).status == "overdue"  # type: ignore[union-attr]
    )
    assert delay_attribution(box.engine, box.clock.now())["Patient"] > 0


def test_new_mistakes_are_refused() -> None:
    client = TestClient(app)
    sid = client.post("/api/demo").json()["sid"]
    for kind in ("ai_evidence", "joint_without_consent"):
        r = client.post(f"/api/demo/{sid}/cases/{COUPLE}/mistake/{kind}").json()
        assert r["ok"] is False and r["violations"] == []


def test_new_invariants_catch_breaks() -> None:
    box = new_sandbox()
    walk(box, MARIAM)
    closure = box.case(MARIAM).closure
    assert closure is not None
    closure.evidence["outcome_record"] = "ai: forged"
    assert any(v.invariant == "I15" for v in check_all(box.engine))
    box = new_sandbox()
    box.case(OMAR).stage_history.append((box.case(MARIAM).stage, 0))
    assert any(v.invariant == "I14" for v in check_all(box.engine))
    box = new_sandbox()
    from itmam.domain.model import Appointment

    c = box.case(COUPLE)
    c.appointments.append(
        Appointment(box.clock.now(), box.clock.now(), box.staff(c).coordinator(c.owner.org), True)
    )
    assert any(v.invariant == "I11" for v in check_all(box.engine))


def test_simulation_runs_clean() -> None:
    import yaml

    from itmam.simulation import PARAMETERS, run

    params = yaml.safe_load(PARAMETERS.read_text(encoding="utf-8"))
    base = run("none", params, cohort=40, days=60, seed=1)
    with_itmam = run("itmam", params, cohort=40, days=60, seed=1)
    assert base.violations == 0 and with_itmam.violations == 0
    assert base.cohort == with_itmam.cohort == 40


def test_barrier_codes_fit_the_case_pathway() -> None:
    client = TestClient(app)
    sid = client.post("/api/demo").json()["sid"]
    for case_id in (COUPLE, MARIAM):
        r = client.post(
            f"/api/demo/{sid}/cases/{case_id}/reply", json={"text": "I don't have a car"}
        ).json()
        assert r["ok"] is True, r["message"]
