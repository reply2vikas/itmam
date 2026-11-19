"""Whole journeys through each pathway, end to end."""

from datetime import timedelta

import pytest

from itmam.domain import Actor, CommandRejected, Stage
from itmam.domain.model import ExpectationStatus, Side

from support import (
    CLOCK,
    P1,
    P2,
    P3,
    PATHWAYS,
    SCREENING,
    SPECIALIST,
    SYSTEM,
    People,
    assert_invariants,
    drive,
    make_engine,
    open_case,
)


def test_p1_happy_path_from_result_to_documented_closure() -> None:
    engine, clock = make_engine()
    people = People(PATHWAYS[P1])
    case_id = open_case(engine)
    case = engine.cases[case_id]
    assert case.owner.org == SCREENING and case.stage == Stage.ISSUED

    drive(engine, clock, case_id, Stage.ACKNOWLEDGED)
    consult = engine.request_econsult(
        people.clinician(SCREENING), case_id, SPECIALIST, "Patient takes anticoagulants. Proceed?"
    )
    engine.answer_econsult(
        people.clinician(SPECIALIST), case_id, consult, "Yes. Pre-procedure review needed."
    )
    assert case.owner.org == SCREENING  # an e-consult never moves ownership

    drive(engine, clock, case_id, Stage.REFERRED)
    assert case.owner.side == Side.SENDER  # still the sender's until acceptance
    drive(engine, clock, case_id, Stage.OUTCOME_RECORDED)
    assert case.owner.org == SPECIALIST and case.owner.side == Side.RECEIVER
    assert [c.reason for c in case.owner_history] == ["opened", "accepted"]

    engine.propose_closure(
        people.clinician(SPECIALIST),
        case_id,
        "care_completed",
        {"outcome_record": "TEST-DOC-1", "next_step": "routine screening"},
    )
    engine.sign_closure(people.clinician(SPECIALIST), case_id)

    assert case.stage == Stage.CLOSED and case.closure is not None
    assert case.closure.counts_as == "success"
    assert all(
        e.status in (ExpectationStatus.MET, ExpectationStatus.CANCELLED) for e in case.expectations
    )
    assert engine.ledger() == {"received": 1, "open": 0, "closed": 1}
    assert_invariants(engine)


def test_p2_each_partner_must_acknowledge_before_referral() -> None:
    engine, clock = make_engine()
    people = People(PATHWAYS[P2])
    case_id = open_case(engine, P2)
    case = engine.cases[case_id]
    first, second = case.subjects

    engine.record_delivery(SYSTEM, case_id, first.id)
    assert case.stage == Stage.ISSUED  # one partner notified is not enough
    engine.acknowledge(Actor.contact(first.contact_id), case_id, first.id, understood=True)
    engine.record_delivery(SYSTEM, case_id, second.id)
    assert case.stage == Stage.NOTIFIED
    with pytest.raises(CommandRejected, match="wrong_stage"):
        engine.send_referral(
            people.coordinator(SCREENING), case_id, SPECIALIST, "note", people.clinician(SCREENING)
        )
    # A partner can never acknowledge for the other.
    with pytest.raises(CommandRejected, match="not_permitted"):
        engine.acknowledge(Actor.contact(first.contact_id), case_id, second.id, understood=True)
    engine.acknowledge(Actor.contact(second.contact_id), case_id, second.id, understood=True)
    assert case.stage == Stage.ACKNOWLEDGED
    assert_invariants(engine)


def test_p2_joint_session_needs_both_consents() -> None:
    engine, clock = make_engine()
    people = People(PATHWAYS[P2])
    case_id = open_case(engine, P2)
    drive(engine, clock, case_id, Stage.ACCEPTED)
    case = engine.cases[case_id]
    when = clock.now() + timedelta(days=3)
    engine.record_consent(Actor.contact(case.subjects[0].contact_id), case_id, case.subjects[0].id)
    with pytest.raises(CommandRejected, match="consent_missing"):
        engine.book_appointment(people.coordinator(SPECIALIST), case_id, when, joint=True)
    engine.record_consent(Actor.contact(case.subjects[1].contact_id), case_id, case.subjects[1].id)
    engine.book_appointment(people.coordinator(SPECIALIST), case_id, when, joint=True)
    assert case.stage == Stage.BOOKED
    assert_invariants(engine)


def test_p3_surveillance_closure_watches_for_the_next_check() -> None:
    engine, clock = make_engine()
    people = People(PATHWAYS[P3])
    case_id = open_case(engine, P3)
    drive(engine, clock, case_id, Stage.OUTCOME_RECORDED)
    due = (clock.now() + timedelta(days=180)).date().isoformat()
    engine.propose_closure(
        people.clinician(SPECIALIST),
        case_id,
        "surveillance",
        {"assessment_report": "TEST-DOC-9", "next_due_date": due},
    )
    engine.sign_closure(people.clinician(SPECIALIST), case_id)
    watch = engine.watches[0]
    assert watch.status == "watching"

    clock.advance(timedelta(days=200))  # past the due date, still inside the 30-day grace
    engine.tick(CLOCK)
    assert watch.status == "watching"
    clock.advance(timedelta(days=30))
    engine.tick(CLOCK)
    assert watch.status == "triggered"
    new_case = engine.cases[watch.new_case_id or ""]
    assert new_case.linked_from == case_id and new_case.result_code == "WELLCHILD_RESCREEN_DUE"
    assert engine.cases[case_id].stage == Stage.CLOSED  # the original never reopens
    assert_invariants(engine)


def test_a_new_result_satisfies_the_watch() -> None:
    engine, clock = make_engine()
    people = People(PATHWAYS[P1])
    case_id = open_case(engine, P1, first=7)
    drive(engine, clock, case_id, Stage.OUTCOME_RECORDED)
    due = (clock.now() + timedelta(days=365)).date().isoformat()
    engine.propose_closure(
        people.clinician(SPECIALIST),
        case_id,
        "surveillance",
        {"outcome_record": "TEST-DOC-3", "next_due_date": due},
    )
    engine.sign_closure(people.clinician(SPECIALIST), case_id)
    open_case(engine, P1, result_id="TEST-R-2", first=7)  # same person, next screening
    assert engine.watches[0].status == "satisfied"
    assert_invariants(engine)


def test_same_result_twice_opens_one_case() -> None:
    engine, _ = make_engine()
    first = open_case(engine)
    again = open_case(engine)
    assert first == again and engine.ledger()["received"] == 1
    assert_invariants(engine)


def test_unknown_result_code_opens_nothing() -> None:
    engine, _ = make_engine()
    with pytest.raises(CommandRejected, match="not_actionable"):
        engine.ingest_result(SYSTEM, "TEST-R-9", "FIT_NEGATIVE", [], SCREENING)
    assert engine.ledger()["received"] == 0
