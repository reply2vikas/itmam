"""Shared test fixtures: synthetic organisations, people and a ready engine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from itmam.domain import (
    Actor,
    Engine,
    ManualClock,
    NewSubject,
    PathwayConfig,
    Stage,
    TemplateRef,
    TemplateRegistry,
    check_all,
    load_pathways,
)
from itmam.domain.roles import SUPERVISOR

PATHWAYS = load_pathways()
P1 = "crc-positive-to-colonoscopy"
P2 = "premarital-at-risk-to-counselling"
P3 = "wellchild-developmental-to-assessment"
CODE = {P1: "FIT_POSITIVE", P2: "PREMARITAL_AT_RISK_COUPLE", P3: "WELLCHILD_DEVELOPMENT_REFER"}

# Synthetic, obviously fictional organisations.
SCREENING = "TEST-ORG-SCREENING-A"
SPECIALIST = "TEST-ORG-SPECIALIST-B"
HOSPITAL = "TEST-ORG-HOSPITAL-C"

START = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)  # Monday 10:00 in the UAE
SYSTEM = Actor.system("lab-feed")
CLOCK = Actor.system("clock")
AI = Actor.ai()


def approve_all(registry: TemplateRegistry, version: int = 1) -> None:
    for p in PATHWAYS.values():
        for message in p.messages:
            for language in p.languages:
                registry.approve(TemplateRef(p.id, message, language, version))


def make_engine(approve: bool = True) -> tuple[Engine, ManualClock]:
    clock = ManualClock(START)
    templates = TemplateRegistry()
    if approve:
        approve_all(templates)
    return Engine(PATHWAYS, clock, templates), clock


class People:
    """Staff for one pathway: `people.coordinator(SCREENING)`, `people.clinician(SPECIALIST, 2)`."""

    def __init__(self, pathway: PathwayConfig) -> None:
        self.p = pathway

    def _role(self, org: str, sender_role: str, receiver_role: str) -> str:
        return sender_role if org == SCREENING else receiver_role

    def coordinator(self, org: str, n: int = 1) -> Actor:
        role = self._role(org, self.p.roles.sender, self.p.roles.receiver)
        return Actor.staff(org, role, f"{org}:{role}:{n}")

    def clinician(self, org: str, n: int = 1) -> Actor:
        role = self._role(org, self.p.roles.sender_clinician, self.p.roles.receiver_clinician)
        return Actor.staff(org, role, f"{org}:{role}:{n}")

    def supervisor(self, org: str, n: int = 1) -> Actor:
        return Actor.staff(org, SUPERVISOR, f"{org}:{SUPERVISOR}:{n}")


def subjects_for(pathway_id: str, first: int = 1, language: str = "en") -> list[NewSubject]:
    count = PATHWAYS[pathway_id].subjects.count
    return [
        NewSubject(f"TEST-P-{n:05d}", f"TEST-CONTACT-{n:05d}", language)
        for n in range(first, first + count)
    ]


def open_case(
    engine: Engine,
    pathway_id: str = P1,
    result_id: str = "TEST-R-1",
    first: int = 1,
    language: str = "en",
) -> str:
    return engine.ingest_result(
        SYSTEM, result_id, CODE[pathway_id], subjects_for(pathway_id, first, language), SCREENING
    )


def drive(engine: Engine, clock: ManualClock, case_id: str, until: Stage) -> None:
    """Move a case through the happy path until it reaches `until`."""
    case = engine.cases[case_id]
    people = People(engine.pathway_of(case))
    order = [
        Stage.NOTIFIED,
        Stage.ACKNOWLEDGED,
        Stage.REFERRED,
        Stage.ACCEPTED,
        Stage.BOOKED,
        Stage.ATTENDED,
        Stage.OUTCOME_RECORDED,
    ]
    start = order.index(case.stage) + 1 if case.stage in order else 0
    for target in order[start : order.index(until) + 1]:
        if target == Stage.NOTIFIED:
            for s in case.subjects:
                engine.record_delivery(SYSTEM, case_id, s.id)
        elif target == Stage.ACKNOWLEDGED:
            for s in case.subjects:
                engine.acknowledge(Actor.contact(s.contact_id), case_id, s.id, understood=True)
        elif target == Stage.REFERRED:
            engine.send_referral(
                people.coordinator(SCREENING),
                case_id,
                SPECIALIST,
                "Positive screen; patient informed.",
                people.clinician(SCREENING),
            )
        elif target == Stage.ACCEPTED:
            engine.accept_referral(people.coordinator(SPECIALIST), case_id)
        elif target == Stage.BOOKED:
            engine.book_appointment(
                people.coordinator(SPECIALIST), case_id, clock.now() + timedelta(days=7)
            )
        elif target == Stage.ATTENDED:
            clock.advance(timedelta(days=7, hours=1))
            engine.record_attendance(people.coordinator(SPECIALIST), case_id, attended=True)
        elif target == Stage.OUTCOME_RECORDED:
            outcome = engine.pathway_of(case).outcomes[0]
            engine.record_outcome(people.clinician(SPECIALIST), case_id, outcome)
    assert case.stage == until


def assert_invariants(engine: Engine) -> None:
    violations = check_all(engine)
    assert not violations, "\n".join(str(v) for v in violations)
