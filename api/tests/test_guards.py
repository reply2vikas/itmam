"""Commands that must be refused, and proof that refusals change nothing."""

from datetime import timedelta
from typing import Any

import pytest

from itmam.domain import Actor, CommandRejected, Stage
from itmam.domain.audit import case_fingerprint

from support import (
    AI,
    P1,
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

PEOPLE = People(PATHWAYS[P1])


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("acknowledge", ("TEST-P-00001", True)),
        ("raise_urgency", ("looks serious",)),
        ("pause", ("travelling", None)),
        ("record_contact_attempt", ("phone", False)),
        ("reassign", ("primary_care_physician", "AI thinks so")),
        ("propose_closure", ("not_actionable", {"clinical_review_note": "x"})),
    ],
)
def test_ai_can_never_change_a_case(command: str, args: tuple[Any, ...]) -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    args = tuple(clock.now() + timedelta(days=1) if a is None else a for a in args)
    before = case_fingerprint(engine.cases[case_id])
    with pytest.raises(CommandRejected, match="ai_not_permitted"):
        getattr(engine, command)(AI, case_id, *args)
    assert case_fingerprint(engine.cases[case_id]) == before
    assert engine.rejections[-1].code == "ai_not_permitted"
    assert_invariants(engine)


def test_ai_suggestions_wait_for_a_person() -> None:
    engine, _ = make_engine()
    case_id = open_case(engine)
    suggestion = engine.suggest_barriers(AI, case_id, ["fear", "transport"], "test-model")
    assert engine.cases[case_id].barriers == []  # nothing changes until a person reviews
    with pytest.raises(CommandRejected, match="ai_not_permitted"):
        engine.review_suggestion(AI, suggestion, accept=True)
    engine.review_suggestion(
        PEOPLE.coordinator(SCREENING), suggestion, accept=True, codes=["transport"]
    )
    assert engine.cases[case_id].barriers == ["transport"]
    assert_invariants(engine)


def test_ai_cannot_suggest_codes_outside_the_pathway_list() -> None:
    engine, _ = make_engine()
    case_id = open_case(engine)
    with pytest.raises(CommandRejected, match="invalid_codes"):
        engine.suggest_barriers(AI, case_id, ["close_this_case"], "test-model")


def test_referral_note_needs_a_human_approver() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.ACKNOWLEDGED)
    with pytest.raises(CommandRejected, match="note_not_approved"):
        engine.send_referral(PEOPLE.coordinator(SCREENING), case_id, SPECIALIST, "draft", AI)


def test_only_the_receiving_organisation_accepts() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.REFERRED)
    for wrong in (
        PEOPLE.coordinator(SCREENING),
        Actor.staff("TEST-ORG-OTHER", "endoscopy_coordinator", "x"),
    ):
        with pytest.raises(CommandRejected, match="not_permitted"):
            engine.accept_referral(wrong, case_id)
    assert engine.cases[case_id].owner.org == SCREENING


def test_patients_use_only_their_own_link() -> None:
    engine, _ = make_engine()
    case_id = open_case(engine)
    subject = engine.cases[case_id].subjects[0]
    engine.record_delivery(SYSTEM, case_id, subject.id)
    with pytest.raises(CommandRejected, match="not_permitted"):
        engine.acknowledge(Actor.contact("TEST-CONTACT-99999"), case_id, subject.id, True)


def test_phone_acknowledgement_needs_call_notes() -> None:
    engine, _ = make_engine()
    case_id = open_case(engine)
    subject = engine.cases[case_id].subjects[0]
    engine.record_delivery(SYSTEM, case_id, subject.id)
    with pytest.raises(CommandRejected, match="notes_required"):
        engine.acknowledge(PEOPLE.coordinator(SCREENING), case_id, subject.id, True, via="phone")
    engine.acknowledge(
        PEOPLE.coordinator(SCREENING),
        case_id,
        subject.id,
        True,
        via="phone",
        notes="Explained the result; patient agrees to colonoscopy.",
    )
    assert engine.cases[case_id].stage == Stage.ACKNOWLEDGED


def test_failed_understanding_check_creates_a_call_task() -> None:
    engine, _ = make_engine()
    case_id = open_case(engine)
    subject = engine.cases[case_id].subjects[0]
    engine.record_delivery(SYSTEM, case_id, subject.id)
    engine.acknowledge(Actor.contact(subject.contact_id), case_id, subject.id, understood=False)
    case = engine.cases[case_id]
    assert case.stage == Stage.NOTIFIED
    assert any(e.kind == "call_task" for e in case.armed())
    engine.acknowledge(Actor.contact(subject.contact_id), case_id, subject.id, understood=True)
    assert case.stage == Stage.ACKNOWLEDGED
    assert not any(e.kind == "call_task" for e in case.armed())


def test_unapproved_template_cannot_be_sent() -> None:
    engine, _ = make_engine(approve=False)
    case_id = open_case(engine)
    with pytest.raises(CommandRejected, match="template_not_approved"):
        engine.record_delivery(SYSTEM, case_id, engine.cases[case_id].subjects[0].id)
    assert engine.cases[case_id].messages == []


def test_closed_case_refuses_every_change() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.ACKNOWLEDGED)
    for clinician in (PEOPLE.clinician(SCREENING, 1), PEOPLE.clinician(SCREENING, 2)):
        if engine.cases[case_id].pending_closure is None:
            engine.propose_closure(
                clinician,
                case_id,
                "not_actionable",
                {"clinical_review_note": "Already under specialist care."},
            )
        engine.sign_closure(clinician, case_id)
    assert engine.cases[case_id].stage == Stage.CLOSED
    before = case_fingerprint(engine.cases[case_id])
    attempts = [
        lambda: engine.raise_urgency(PEOPLE.clinician(SCREENING), case_id, "late finding"),
        lambda: engine.request_callback(Actor.contact("TEST-CONTACT-00001"), case_id),
        lambda: engine.record_contact_attempt(PEOPLE.coordinator(SCREENING), case_id, "sms", True),
    ]
    for attempt in attempts:
        with pytest.raises(CommandRejected, match="case_closed"):
            attempt()
    assert case_fingerprint(engine.cases[case_id]) == before
    assert_invariants(engine)


def test_reassignment_stays_inside_the_organisation() -> None:
    engine, _ = make_engine()
    case_id = open_case(engine)
    with pytest.raises(CommandRejected, match="not_permitted"):
        engine.reassign(PEOPLE.coordinator(SCREENING), case_id, "primary_care_physician", "busy")
    with pytest.raises(CommandRejected, match="invalid_role"):
        engine.reassign(PEOPLE.supervisor(SCREENING), case_id, "gastroenterologist", "busy")
    engine.reassign(
        PEOPLE.supervisor(SCREENING), case_id, "primary_care_physician", "clinical question first"
    )
    assert engine.cases[case_id].owner.role == "primary_care_physician"
    assert_invariants(engine)


def test_only_clinicians_raise_urgency_and_it_never_drops() -> None:
    engine, _ = make_engine()
    case_id = open_case(engine)
    with pytest.raises(CommandRejected, match="not_permitted"):
        engine.raise_urgency(PEOPLE.coordinator(SCREENING), case_id, "worried")
    engine.raise_urgency(PEOPLE.clinician(SCREENING), case_id, "reports bleeding")
    with pytest.raises(CommandRejected, match="already_urgent"):
        engine.raise_urgency(PEOPLE.clinician(SCREENING), case_id, "again")
    assert engine.cases[case_id].urgency == "urgent"
