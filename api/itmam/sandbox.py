"""A private demo world for one visitor: synthetic organisations, people, patients and a clock.

Every visitor to the web demo gets their own `Sandbox`, so nobody's clicks affect anyone else.
Everything here is synthetic: the organisations, the people and the patients are invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from itmam.domain import (
    Actor,
    CommandRejected,
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
from itmam.domain.calendar import UAE_TZ
from itmam.domain.model import Case
from itmam.domain.roles import SUPERVISOR

P1 = "crc-positive-to-colonoscopy"
SCREENING = "Screening Clinic A"
START = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)  # Monday 10:00 in the UAE

FEED = Actor.system("lab-feed")
CLOCK_ACTOR = Actor.system("clock")


@dataclass(frozen=True)
class Provider:
    """A synthetic receiving unit in the provider directory the Router agent reads."""

    org: str
    networks: tuple[str, ...]
    female_endoscopist: bool
    languages: tuple[str, ...]
    next_slot_days: int
    accepting: bool = True


DIRECTORY: tuple[Provider, ...] = (
    Provider("Endoscopy Unit B", ("Thiqa", "Daman Enhanced"), True, ("ar", "en"), 6),
    Provider("Endoscopy Unit C", ("Thiqa", "Daman Basic", "Daman Enhanced"), False, ("en",), 3),
    Provider("Endoscopy Unit D", ("Daman Enhanced", "Other"), True, ("ar", "en", "ur"), 12),
    Provider("Endoscopy Unit E", ("Thiqa",), False, ("ar",), 2, accepting=False),
)


@dataclass
class Profile:
    """What the programme knows about a patient besides the case: synthetic and minimal."""

    name: str
    insurance: str
    prefers_female_endoscopist: bool = False
    note: str = ""


@dataclass
class Staff:
    """The people in each organisation for one pathway."""

    p: PathwayConfig

    def _role(self, org: str, sender_role: str, receiver_role: str) -> str:
        return sender_role if org == SCREENING else receiver_role

    def coordinator(self, org: str) -> Actor:
        role = self._role(org, self.p.roles.sender, self.p.roles.receiver)
        return Actor.staff(org, role, f"Coordinator, {org}")

    def clinician(self, org: str, n: int = 1) -> Actor:
        role = self._role(org, self.p.roles.sender_clinician, self.p.roles.receiver_clinician)
        return Actor.staff(org, role, f"Clinician {n}, {org}")

    def supervisor(self, org: str) -> Actor:
        return Actor.staff(org, SUPERVISOR, f"Supervisor, {org}")


@dataclass
class Sandbox:
    engine: Engine
    clock: ManualClock
    profiles: dict[str, Profile] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers

    def staff(self, case: Case) -> Staff:
        return Staff(self.engine.pathway_of(case))

    def case(self, case_id: str) -> Case:
        if case_id not in self.engine.cases:
            raise CommandRejected("unknown_case", f"no case {case_id}")
        return self.engine.cases[case_id]

    def local_time(self) -> str:
        return self.clock.now().astimezone(UAE_TZ).strftime("%a %d %b %Y, %H:%M")

    def advance(self, days: float) -> None:
        self.clock.advance(timedelta(days=days))
        self.engine.tick(CLOCK_ACTOR)

    # --------------------------------------------------------------- next step

    def next_step(self, case_id: str, to_org: str | None = None) -> str:
        """Take the next correct step as the right person, and say what happened."""
        from itmam.agents.router import route  # local import: agents depend on the sandbox

        c = self.case(case_id)
        e = self.engine
        s = self.staff(c)
        if not c.is_open:
            return "This case is closed."
        if c.is_paused:
            return "This case is paused; it resumes automatically at its wake-up time."
        if c.stage == Stage.ISSUED:
            for subject in c.subjects:
                if subject.notified_at is None:
                    e.record_delivery(FEED, case_id, subject.id)
            return "The SMS gateway confirmed the result notice was delivered."
        if c.stage == Stage.NOTIFIED:
            for subject in c.subjects:
                if not (subject.acknowledged_at and subject.understood):
                    e.acknowledge(Actor.contact(subject.contact_id), case_id, subject.id, True)
            return "The patient opened the link and passed the understanding check."
        if c.stage == Stage.ACKNOWLEDGED:
            target = to_org or route(self, case_id).details.get("org")
            if not target:
                raise CommandRejected("no_provider", "no in-network provider: escalate")
            e.send_referral(
                s.coordinator(SCREENING),
                case_id,
                target,
                "Positive screening result; patient informed and agrees to colonoscopy.",
                s.clinician(SCREENING),
            )
            return (
                f"Referral sent to {target}. Screening Clinic A stays the owner until it accepts."
            )
        if c.stage == Stage.REFERRED:
            referral = c.open_referral()
            assert referral is not None
            e.accept_referral(s.coordinator(referral.to_org), case_id)
            return f"{referral.to_org} accepted the referral and now owns the case."
        if c.stage == Stage.ACCEPTED:
            when = self.clock.now() + timedelta(days=5)
            e.book_appointment(s.coordinator(c.owner.org), case_id, when)
            local = when.astimezone(UAE_TZ).strftime("%a %d %b, %H:%M")
            return f"Colonoscopy booked for {local}."
        if c.stage == Stage.BOOKED:
            appointment = c.appointments[-1].at
            if self.clock.now() < appointment:
                local = appointment.astimezone(UAE_TZ).strftime("%a %d %b")
                return f"The appointment is on {local}. Skip ahead to reach it."
            e.record_attendance(s.coordinator(c.owner.org), case_id, True)
            return "The patient attended the colonoscopy."
        if c.stage == Stage.ATTENDED:
            e.record_outcome(s.clinician(c.owner.org), case_id, "polyps_removed")
            return "The gastroenterologist recorded the outcome: polyps removed."
        if c.stage == Stage.OUTCOME_RECORDED:
            if c.pending_closure is None:
                due = (self.clock.now() + timedelta(days=3 * 365)).date().isoformat()
                e.propose_closure(
                    s.coordinator(c.owner.org),
                    case_id,
                    "surveillance",
                    {"outcome_record": "DOC-COLONOSCOPY-REPORT", "next_due_date": due},
                )
            e.sign_closure(s.clinician(c.owner.org), case_id)
            return "Closed to surveillance with evidence and a clinician's signature."
        return "Nothing to do."

    # ------------------------------------------------------------------- views

    def summary(self) -> dict[str, Any]:
        return {
            "clock": self.local_time(),
            "ledger": self.engine.ledger(),
            "violations": [str(v) for v in check_all(self.engine)],
            "audit_events": len(self.engine.audit),
            "audit_chain_ok": not self.engine.audit.verify(),
        }


def _approve_templates(registry: TemplateRegistry, pathways: dict[str, PathwayConfig]) -> None:
    for p in pathways.values():
        for message in p.messages:
            for language in p.languages:
                registry.approve(TemplateRef(p.id, message, language, 1))


def new_sandbox() -> Sandbox:
    """A fresh demo world with four synthetic patients at different points of the journey."""
    pathways = load_pathways()
    clock = ManualClock(START)
    templates = TemplateRegistry()
    _approve_templates(templates, pathways)
    engine = Engine(pathways, clock, templates)
    box = Sandbox(engine, clock)
    staff = Staff(pathways[P1])

    def open_case(n: int, language: str, profile: Profile) -> str:
        subject = NewSubject(f"SYN-P-{n}", f"SYN-C-{n}", language)
        case_id = engine.ingest_result(FEED, f"SYN-R-{n}", "FIT_POSITIVE", [subject], SCREENING)
        box.profiles[case_id] = profile
        return case_id

    mariam = open_case(1, "ar", Profile("Mariam", "Thiqa", note="Engaged; replies quickly."))
    omar = open_case(2, "en", Profile("Omar", "Daman Basic", note="Has not opened any message."))
    sara = open_case(
        3,
        "ar",
        Profile("Sara", "Daman Enhanced", True, "Rectal bleeding reported to her GP."),
    )
    rashid = open_case(4, "en", Profile("Rashid", "Thiqa", note="Missed his first appointment."))

    for case_id in (mariam, omar, sara, rashid):
        box.next_step(case_id)  # result notice delivered
    for case_id in (mariam, sara, rashid):
        box.next_step(case_id)  # acknowledged

    engine.raise_urgency(staff.clinician(SCREENING), sara, "Rectal bleeding reported.")

    box.next_step(rashid, to_org="Endoscopy Unit C")
    box.next_step(rashid)  # accepted
    unit = engine.cases[rashid].owner.org
    engine.book_appointment(staff.coordinator(unit), rashid, clock.now() + timedelta(hours=1))
    clock.advance(timedelta(hours=2))
    engine.record_attendance(staff.coordinator(unit), rashid, attended=False)
    engine.tick(CLOCK_ACTOR)
    return box
