"""No closure without proof: evidence, the right signers, and two people where required."""

from datetime import timedelta

import pytest

from itmam.domain import CommandRejected, Stage

from support import (
    P1,
    PATHWAYS,
    SCREENING,
    SPECIALIST,
    People,
    assert_invariants,
    drive,
    make_engine,
    open_case,
)

PEOPLE = People(PATHWAYS[P1])


def test_care_completed_needs_the_outcome_first() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.ATTENDED)
    with pytest.raises(CommandRejected, match="wrong_stage"):
        engine.propose_closure(
            PEOPLE.clinician(SPECIALIST),
            case_id,
            "care_completed",
            {"outcome_record": "TEST-DOC", "next_step": "none"},
        )


def test_missing_evidence_is_refused() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.OUTCOME_RECORDED)
    with pytest.raises(CommandRejected, match="evidence_missing: missing evidence: next_step"):
        engine.propose_closure(
            PEOPLE.clinician(SPECIALIST),
            case_id,
            "care_completed",
            {"outcome_record": "TEST-DOC", "next_step": "  "},
        )
    assert engine.cases[case_id].pending_closure is None


def test_decline_needs_two_different_clinicians() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.ACKNOWLEDGED)
    first, second = PEOPLE.clinician(SCREENING, 1), PEOPLE.clinician(SCREENING, 2)
    engine.propose_closure(
        PEOPLE.coordinator(SCREENING),
        case_id,
        "informed_decline",
        {"counselling_note": "TEST-NOTE", "decision_record": "TEST-DEC"},
    )
    with pytest.raises(CommandRejected, match="signer_not_permitted"):
        engine.sign_closure(PEOPLE.coordinator(SCREENING), case_id)
    engine.sign_closure(first, case_id)
    with pytest.raises(CommandRejected, match="already_signed"):
        engine.sign_closure(first, case_id)
    assert engine.cases[case_id].stage == Stage.ACKNOWLEDGED
    engine.sign_closure(second, case_id)
    assert engine.cases[case_id].stage == Stage.CLOSED
    assert_invariants(engine)


def test_unreachable_needs_the_full_contact_protocol() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    evidence = {"contact_attempt_log": "TEST-LOG", "letter_sent": "TEST-LETTER"}
    coordinator = PEOPLE.coordinator(SCREENING)
    with pytest.raises(CommandRejected, match="contact_protocol_incomplete"):
        engine.propose_closure(coordinator, case_id, "unreachable", evidence)
    for channel in ("phone", "phone", "sms", "whatsapp", "phone", "letter"):
        engine.record_contact_attempt(coordinator, case_id, channel, reached=False)
    engine.propose_closure(coordinator, case_id, "unreachable", evidence)
    # The supervisor signs first and takes the supervisor slot, leaving the clinician slot.
    engine.sign_closure(PEOPLE.supervisor(SCREENING), case_id)
    engine.sign_closure(PEOPLE.clinician(SCREENING), case_id)
    closure = engine.cases[case_id].closure
    assert closure is not None and closure.counts_as == "failure"
    assert_invariants(engine)


def test_transfer_names_the_new_organisation() -> None:
    engine, _ = make_engine()
    case_id = open_case(engine)
    coordinator = PEOPLE.coordinator(SCREENING)
    evidence = {"receiving_org_acceptance": "TEST-ACCEPT"}
    with pytest.raises(CommandRejected, match="invalid_target"):
        engine.propose_closure(coordinator, case_id, "transferred_out", evidence)
    engine.propose_closure(
        coordinator, case_id, "transferred_out", evidence, transfer_to="TEST-ORG-OTHER-EMIRATE"
    )
    engine.sign_closure(coordinator, case_id)
    assert engine.cases[case_id].closure.transfer_to == "TEST-ORG-OTHER-EMIRATE"  # type: ignore[union-attr]
    assert_invariants(engine)


def test_surveillance_due_date_must_be_in_the_future() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.OUTCOME_RECORDED)
    past = (clock.now() - timedelta(days=1)).date().isoformat()
    with pytest.raises(CommandRejected, match="invalid_due_date"):
        engine.propose_closure(
            PEOPLE.clinician(SPECIALIST),
            case_id,
            "surveillance",
            {"outcome_record": "TEST-DOC", "next_due_date": past},
        )
    with pytest.raises(CommandRejected, match="invalid_due_date"):
        engine.propose_closure(
            PEOPLE.clinician(SPECIALIST),
            case_id,
            "surveillance",
            {"outcome_record": "TEST-DOC", "next_due_date": "next spring"},
        )


def test_pending_closure_blocks_progress_until_withdrawn() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.ACKNOWLEDGED)
    engine.propose_closure(
        PEOPLE.clinician(SCREENING),
        case_id,
        "not_actionable",
        {"clinical_review_note": "TEST-NOTE"},
    )
    with pytest.raises(CommandRejected, match="closure_pending"):
        engine.send_referral(
            PEOPLE.coordinator(SCREENING), case_id, SPECIALIST, "note", PEOPLE.clinician(SCREENING)
        )
    engine.withdraw_closure(PEOPLE.clinician(SCREENING), case_id, "new information")
    drive(engine, clock, case_id, Stage.REFERRED)
    assert_invariants(engine)


def test_closing_a_paused_case_ends_the_pause() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    coordinator = PEOPLE.coordinator(SCREENING)
    engine.pause(coordinator, case_id, "hospitalised", clock.now() + timedelta(days=5))
    engine.propose_closure(
        coordinator,
        case_id,
        "transferred_out",
        {"receiving_org_acceptance": "TEST-ACCEPT"},
        transfer_to="TEST-ORG-HOSPITAL-Z",
    )
    engine.sign_closure(coordinator, case_id)
    case = engine.cases[case_id]
    assert case.pause is None and case.pause_history[-1].ended_by == "closure"
    assert_invariants(engine)
