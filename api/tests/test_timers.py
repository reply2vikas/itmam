"""Silence is the alarm: clocks, retries, escalations and pauses."""

from datetime import timedelta

import pytest

from itmam.domain import Actor, CommandRejected, Stage, TemplateRef
from itmam.domain.calendar import UAE_CALENDAR
from itmam.domain.model import ExpectationStatus

from support import (
    CLOCK,
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


def stage_clock(engine, case_id, key):  # type: ignore[no-untyped-def]
    return next(e for e in engine.cases[case_id].expectations if e.kind == f"stage:{key}")


def test_unsent_notice_warns_then_climbs_the_ladder() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)  # Monday 10:00 UAE; notify warns at 1wd, breaches at 2wd
    case = engine.cases[case_id]
    exp = stage_clock(engine, case_id, "notify")

    clock.advance(timedelta(days=1, minutes=1))
    engine.tick(CLOCK)
    assert exp.warned and exp.level == 0

    clock.advance(timedelta(days=1))  # Wednesday: 2 working days, breached
    engine.tick(CLOCK)
    assert exp.level == 1 and case.escalations[-1].level == 1
    assert case.escalations[-1].target.endswith("screening_coordinator queue")

    clock.advance(timedelta(days=2))  # 2 more working days without action
    engine.tick(CLOCK)
    assert exp.level == 2 and case.escalations[-1].target.endswith(":supervisor")

    clock.advance(timedelta(days=7))
    engine.tick(CLOCK)
    assert exp.level == 3 and case.escalations[-1].target == "programme_manager"
    assert exp.status == ExpectationStatus.ARMED  # still waiting: the case never goes silent
    assert_invariants(engine)


def test_urgent_cases_skip_straight_to_a_clinician() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    engine.raise_urgency(PEOPLE.clinician(SCREENING), case_id, "symptoms reported")
    clock.advance(timedelta(days=3))
    engine.tick(CLOCK)
    assert stage_clock(engine, case_id, "notify").level == 2


def test_reminder_then_phone_task_while_unacknowledged() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    case = engine.cases[case_id]
    engine.record_delivery(SYSTEM, case_id, case.subjects[0].id)

    clock.advance(timedelta(days=2, minutes=1))
    engine.tick(CLOCK)
    assert [m.message_id for m in case.messages] == ["result_notice", "reminder"]

    clock.advance(timedelta(days=1))
    engine.tick(CLOCK)
    assert any(e.kind == "call_task" for e in case.armed())
    assert_invariants(engine)


def test_no_approved_reminder_means_a_person_calls_instead() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    case = engine.cases[case_id]
    engine.record_delivery(SYSTEM, case_id, case.subjects[0].id)
    for language in ("ar", "en"):  # clinical governance withdraws the reminder text
        engine.templates.revoke(TemplateRef(P1, "reminder", language, 1), clock.now())
    clock.advance(timedelta(days=2, minutes=1))
    engine.tick(CLOCK)
    assert [m.message_id for m in case.messages] == ["result_notice"]
    assert any(e.kind == "call_task" for e in case.armed())
    assert engine.audit.events[-2].action == "message_blocked"
    assert_invariants(engine)


def test_pause_stops_the_clock_and_wakes_automatically() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    case = engine.cases[case_id]
    notify = stage_clock(engine, case_id, "notify")
    breach_before = notify.breach_at

    engine.pause(
        PEOPLE.coordinator(SCREENING), case_id, "travelling", clock.now() + timedelta(days=10)
    )
    assert notify.status == ExpectationStatus.SUSPENDED
    clock.advance(timedelta(days=5))
    engine.tick(CLOCK)
    assert case.escalations == []  # stopped clocks do not escalate

    clock.advance(timedelta(days=5))
    engine.tick(CLOCK)
    assert case.pause is None and case.pause_history[-1].ended_by == "clock"
    assert notify.status == ExpectationStatus.ARMED
    assert notify.breach_at == breach_before + timedelta(days=10)
    assert case.total_paused == timedelta(days=10)
    assert_invariants(engine)


@pytest.mark.parametrize(
    ("approver", "reason", "days", "code"),
    [
        ("coordinator", "travelling", 31, "pause_too_long"),
        ("coordinator", "patient_request", 7, "approver_not_permitted"),
        ("clinician", "other", 3, "notes_required"),
        ("coordinator", "holiday_mood", 3, "unknown_reason"),
    ],
)
def test_pause_rules(approver: str, reason: str, days: int, code: str) -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    actor = getattr(PEOPLE, approver)(SCREENING)
    with pytest.raises(CommandRejected, match=code):
        engine.pause(actor, case_id, reason, clock.now() + timedelta(days=days))
    assert engine.cases[case_id].pause is None


def test_urgent_case_pauses_need_a_clinician() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    engine.raise_urgency(PEOPLE.clinician(SCREENING), case_id, "red flag")
    with pytest.raises(CommandRejected, match="approver_not_permitted"):
        engine.pause(
            PEOPLE.coordinator(SCREENING), case_id, "travelling", clock.now() + timedelta(days=3)
        )
    engine.pause(
        PEOPLE.clinician(SCREENING), case_id, "travelling", clock.now() + timedelta(days=3)
    )
    assert_invariants(engine)


def test_paused_cases_cannot_move_forward() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    engine.pause(
        PEOPLE.coordinator(SCREENING), case_id, "travelling", clock.now() + timedelta(days=3)
    )
    with pytest.raises(CommandRejected, match="case_paused"):
        engine.record_delivery(SYSTEM, case_id, engine.cases[case_id].subjects[0].id)


def test_care_clock_hard_limit_reaches_the_programme_manager() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    drive(engine, clock, case_id, Stage.ACCEPTED)
    care = next(e for e in engine.cases[case_id].expectations if e.kind == "care_clock")
    clock.advance(timedelta(days=181))
    engine.tick(CLOCK)
    assert care.level == 3
    assert_invariants(engine)


def test_second_rejection_goes_to_the_supervisor() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    case = engine.cases[case_id]
    for attempt in (1, 2):
        drive(engine, clock, case_id, Stage.REFERRED)
        engine.reject_referral(PEOPLE.coordinator(SPECIALIST), case_id, f"no capacity {attempt}")
        assert case.stage == Stage.ACKNOWLEDGED and case.owner.org == SCREENING
    refer = [e for e in case.armed() if e.kind == "stage:refer"][-1]
    assert refer.level == 2 and case.escalations[-1].reason == "second referral rejection"
    assert refer.breach_at == UAE_CALENDAR.add_working_days(clock.now(), 2)
    assert_invariants(engine)


def test_repeated_no_shows_escalate() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    case = engine.cases[case_id]
    for _ in range(2):
        drive(engine, clock, case_id, Stage.BOOKED)
        clock.advance(timedelta(days=8))
        engine.record_attendance(PEOPLE.coordinator(SPECIALIST), case_id, attended=False)
        assert case.stage == Stage.ACCEPTED
    assert case.no_shows == 2 and case.escalations[-1].reason == "repeated no-shows"
    assert_invariants(engine)


def test_callback_has_its_own_clock() -> None:
    engine, clock = make_engine()
    case_id = open_case(engine)
    contact = Actor.contact(engine.cases[case_id].subjects[0].contact_id)
    callback = engine.request_callback(contact, case_id, "Please call after 5pm")
    clock.advance(timedelta(days=3))
    engine.tick(CLOCK)
    exp = next(e for e in engine.cases[case_id].expectations if e.kind == "callback")
    assert exp.level >= 1
    engine.complete_callback(PEOPLE.coordinator(SCREENING), case_id, callback, "Called; reassured.")
    assert exp.status == ExpectationStatus.MET
    assert_invariants(engine)
