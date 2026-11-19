"""A narrated run of the engine: `python -m itmam.demo`.

Two synthetic patients with positive colorectal screening results. Mariam engages and reaches
documented closure; Omar goes silent, and the clocks notice. Everything is synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from itmam.domain import (
    Actor,
    CommandRejected,
    Engine,
    ManualClock,
    NewSubject,
    TemplateRef,
    TemplateRegistry,
    check_all,
    load_pathways,
)
from itmam.domain.calendar import UAE_TZ
from itmam.domain.roles import SUPERVISOR

P1 = "crc-positive-to-colonoscopy"
SCREENING, SPECIALIST, HOSPITAL = "Screening Clinic A", "Endoscopy Unit B", "Hospital C"


def main() -> None:
    pathways = load_pathways()
    clock = ManualClock(datetime(2026, 10, 19, 6, 0, tzinfo=UTC))  # Monday 10:00 in the UAE
    templates = TemplateRegistry()
    for message in pathways[P1].messages:
        for language in ("ar", "en"):
            templates.approve(TemplateRef(P1, message, language, 1))
    engine = Engine(pathways, clock, templates)
    feed, tick = Actor.system("lab-feed"), Actor.system("clock")
    coordinator = Actor.staff(SCREENING, "screening_coordinator", "Coordinator Noura")
    physician = Actor.staff(SCREENING, "primary_care_physician", "Dr Saeed")
    unit = Actor.staff(SPECIALIST, "endoscopy_coordinator", "Coordinator Lina")
    specialist = Actor.staff(SPECIALIST, "gastroenterologist", "Dr Fatima")
    hospital = Actor.staff(HOSPITAL, "endoscopy_coordinator", "Coordinator Ravi")
    seen = 0

    def say(text: str) -> None:
        stamp = clock.now().astimezone(UAE_TZ).strftime("%a %d %b %H:%M")
        print(f"{stamp}  {text}")

    def show_new_events() -> None:
        nonlocal seen
        for event in engine.audit.events[seen:]:
            if event.action in {"warning", "escalated", "retry_fired", "message_sent"}:
                say(f"   [{event.case_id}] {event.action}: {event.details}")
        seen = len(engine.audit.events)

    mariam = engine.ingest_result(
        feed, "TEST-R-0001", "FIT_POSITIVE", [NewSubject("TEST-P-1", "TEST-C-1", "ar")], SCREENING
    )
    omar = engine.ingest_result(
        feed, "TEST-R-0002", "FIT_POSITIVE", [NewSubject("TEST-P-2", "TEST-C-2", "en")], SCREENING
    )
    say(f"Two positive results arrive: {mariam} (Mariam, Arabic) and {omar} (Omar, English)")
    for case_id, subject in ((mariam, "TEST-P-1"), (omar, "TEST-P-2")):
        engine.record_delivery(feed, case_id, subject)
    say("Both result notices are delivered by SMS link; owner: Screening Clinic A")
    seen = len(engine.audit.events)

    engine.acknowledge(Actor.contact("TEST-C-1"), mariam, "TEST-P-1", understood=True)
    say("Mariam opens her link in Arabic and passes the understanding check")
    suggestion = engine.suggest_barriers(Actor.ai(), mariam, ["fear", "transport"], "demo-model")
    say("She writes she is scared and has no car; AI suggests barriers: fear, transport")
    engine.review_suggestion(coordinator, suggestion, accept=True)
    say("Coordinator Noura approves the tags; transport is arranged")

    for days in (2, 1, 2):
        clock.advance(timedelta(days=days))
        engine.tick(tick)
        show_new_events()
    say(f"Omar never opened his message: his case is at level {_level(engine, omar)}")

    consult = engine.request_econsult(physician, mariam, SPECIALIST, "On anticoagulants. Proceed?")
    engine.answer_econsult(specialist, mariam, consult, "Yes; pre-procedure review first.")
    say("Dr Saeed asks the specialist an e-consult question; answered, owner unchanged")
    engine.send_referral(coordinator, mariam, HOSPITAL, "Positive FIT; see e-consult.", physician)
    engine.reject_referral(hospital, mariam, "No endoscopy capacity this month")
    say("Hospital C rejects the referral with a reason; the case returns to the sender")
    engine.send_referral(coordinator, mariam, SPECIALIST, "Positive FIT; see e-consult.", physician)
    engine.accept_referral(unit, mariam)
    say(f"Endoscopy Unit B accepts; owner is now {engine.cases[mariam].owner.org}")

    engine.book_appointment(unit, mariam, clock.now() + timedelta(days=10))
    clock.advance(timedelta(days=10, hours=2))
    engine.record_attendance(unit, mariam, attended=True)
    engine.record_outcome(specialist, mariam, "polyps_removed")
    say("Colonoscopy done; outcome recorded: polyps removed")
    try:
        engine.propose_closure(unit, mariam, "care_completed", {"outcome_record": "TEST-DOC"})
    except CommandRejected as refused:
        say(f"A closure without the next step is refused: {refused.message}")
    due = (clock.now() + timedelta(days=3 * 365)).date().isoformat()
    engine.propose_closure(
        specialist, mariam, "surveillance", {"outcome_record": "TEST-DOC", "next_due_date": due}
    )
    engine.sign_closure(specialist, mariam)
    say(f"Closed to surveillance; a watch will open a new case if the {due} check is missed")

    supervisor = Actor.staff(SCREENING, SUPERVISOR, "Dr Huda")
    engine.reassign(supervisor, omar, "primary_care_physician", "Clinical call needed")
    say("Supervisor Dr Huda moves Omar's case to the physician's queue")
    print()
    print(f"Ledger: {engine.ledger()}")
    print(f"Invariant violations: {len(check_all(engine))}")
    print(f"Audit events: {len(engine.audit)}; chain problems: {len(engine.audit.verify())}")


def _level(engine: Engine, case_id: str) -> int:
    return max((e.level for e in engine.cases[case_id].escalations), default=0)


if __name__ == "__main__":
    main()
