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
P2 = "premarital-at-risk-to-counselling"
GENETICS = "Genetics Centre G"
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
    ai_log: list[dict[str, Any]] = field(default_factory=list)  # every agent proposal

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
            target: str | None = to_org
            if not target:
                target = GENETICS if c.pathway_id != P1 else route(self, case_id).details.get("org")
            if not target:
                raise CommandRejected("no_provider", "no in-network provider: escalate")
            e.send_referral(
                s.coordinator(SCREENING),
                case_id,
                target,
                "Actionable screening result; patient informed and agrees to the next step.",
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
            local = when.astimezone(UAE_TZ).strftime("%a %d %b, %H:%M")
            if len(c.subjects) > 1:
                waiting = [x for x in c.subjects if "joint_session" not in x.consents]
                if waiting:
                    for x in waiting:
                        e.record_consent(Actor.contact(x.contact_id), case_id, x.id)
                    return "The other partner gave their own consent to a joint session."
                e.book_appointment(s.coordinator(c.owner.org), case_id, when, joint=True)
                return f"Joint counselling session booked for {local}, with both consents."
            e.book_appointment(s.coordinator(c.owner.org), case_id, when)
            return f"Appointment booked for {local}."
        if c.stage == Stage.BOOKED:
            appointment = c.appointments[-1].at
            if self.clock.now() < appointment:
                local = appointment.astimezone(UAE_TZ).strftime("%a %d %b")
                return f"The appointment is on {local}. Skip ahead to reach it."
            e.record_attendance(s.coordinator(c.owner.org), case_id, True)
            return "The patient attended."
        if c.stage == Stage.ATTENDED:
            p = e.pathway_of(c)
            outcome = "polyps_removed" if c.pathway_id == P1 else p.outcomes[0]
            e.record_outcome(s.clinician(c.owner.org), case_id, outcome)
            return f"The clinician recorded the outcome: {outcome.replace('_', ' ')}."
        if c.stage == Stage.OUTCOME_RECORDED:
            record = self.malaffi_record(c)
            if c.pending_closure is None:
                if c.pathway_id == P1:
                    due = (self.clock.now() + timedelta(days=3 * 365)).date().isoformat()
                    e.propose_closure(
                        s.coordinator(c.owner.org),
                        case_id,
                        "surveillance",
                        {"outcome_record": record, "next_due_date": due},
                    )
                else:
                    e.propose_closure(
                        s.coordinator(c.owner.org),
                        case_id,
                        "care_completed",
                        {"counselling_record": record, "options_discussed": record},
                    )
            e.sign_closure(s.clinician(c.owner.org), case_id)
            return (
                "Closed with T3 evidence: the visit report was confirmed through Malaffi "
                "(simulated), and a clinician signed."
            )
        return "Nothing to do."

    def malaffi_record(self, c: Case) -> str:
        """A simulated Malaffi lookup: once a visit is recorded, the exchange holds the report.
        There is no real Malaffi connection; the reference is synthetic and labelled so."""
        if c.stage not in (Stage.ATTENDED, Stage.OUTCOME_RECORDED):
            raise CommandRejected("no_record", "Malaffi has no visit record for this case yet")
        return f"malaffi:ENC-SYN-{c.id} (simulated)"

    # ------------------------------------------------------------------- views

    def summary(self) -> dict[str, Any]:
        return {
            "clock": self.local_time(),
            "ledger": self.engine.ledger(),
            "violations": [str(v) for v in check_all(self.engine)],
            "audit_events": len(self.engine.audit),
            "audit_chain_ok": not self.engine.audit.verify(),
            "metrics": self.metrics(),
            "insights": self.insights(),
        }

    def insights(self) -> dict[str, Any]:
        from itmam.insights import delay_attribution, leakage, pre_breach

        now = self.clock.now()
        names = {cid: p.name for cid, p in self.profiles.items()}
        soon = [
            {**x, "name": names.get(x["case_id"], x["case_id"])}
            for x in pre_breach(self.engine, now)
        ]
        return {
            "stuck_points": leakage(self.engine, now),
            "pre_breach": soon,
            "days_held": delay_attribution(self.engine, now),
        }

    def metrics(self) -> dict[str, Any]:
        """Statement 05's closure and impact measures, computed from this sandbox's cases and
        audit log. Prototype figures on synthetic patients, not outcomes."""
        from itmam.domain.model import ExpectationStatus

        now = self.clock.now()
        cases = list(self.engine.cases.values())
        firsts: dict[tuple[str, str], datetime] = {}
        for event in self.engine.audit.events:
            if event.case_id:
                key = (event.case_id, event.action)
                firsts.setdefault(key, datetime.fromisoformat(event.at))

        def avg_days(start: str, end: str) -> float | None:
            gaps = [
                (firsts[(c.id, end)] - firsts[(c.id, start)]).total_seconds() / 86400
                for c in cases
                if (c.id, start) in firsts and (c.id, end) in firsts
            ]
            return round(sum(gaps) / len(gaps), 1) if gaps else None

        def overdue(c: Case) -> bool:
            return c.is_open and any(
                e.status == ExpectationStatus.ARMED and e.breach_at <= now for e in c.expectations
            )

        return {
            "actionable_results": len(cases),
            "notified": sum(any(s.notified_at for s in c.subjects) for c in cases),
            "understood": sum(all(s.understood for s in c.subjects) for c in cases),
            "referrals_accepted": sum(
                any(r.status == "accepted" for r in c.referrals) for c in cases
            ),
            "attended": sum(
                c.stage.value in {"attended", "outcome_recorded"} or bool(c.closure and c.outcome)
                for c in cases
            ),
            "no_shows": sum(c.no_shows for c in cases),
            "overdue_now": sum(overdue(c) for c in cases),
            "escalated": sum(bool(c.escalations) for c in cases),
            "closed_with_evidence": sum(c.closure is not None for c in cases),
            "unresolved": sum(c.is_open for c in cases),
            "avg_days_to_notification": avg_days("case_opened", "message_delivered"),
            "avg_days_to_referral_accepted": avg_days("case_opened", "referral_accepted"),
            "avg_days_to_closure": avg_days("case_opened", "case_closed"),
        }


def _approve_templates(registry: TemplateRegistry, pathways: dict[str, PathwayConfig]) -> None:
    for p in pathways.values():
        for message in p.messages:
            for language in p.languages:
                registry.approve(TemplateRef(p.id, message, language, 1))


def new_sandbox() -> Sandbox:
    """A fresh demo world with five synthetic cases at different points of the journey."""
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

    # A premarital at-risk couple: one case, two people, each understands and consents alone.
    partners = [NewSubject("SYN-P-5", "SYN-C-5", "ar"), NewSubject("SYN-P-6", "SYN-C-6", "ar")]
    couple = engine.ingest_result(FEED, "SYN-R-5", "PREMARITAL_AT_RISK_COUPLE", partners, SCREENING)
    box.profiles[couple] = Profile(
        "Aisha & Khalid", "Thiqa", note="Premarital couple: one case, two people, two consents."
    )
    for _ in range(4):
        box.next_step(couple)  # notified, both understood, referred, accepted
    engine.record_consent(Actor.contact("SYN-C-5"), couple, "SYN-P-5")

    box.next_step(rashid, to_org="Endoscopy Unit C")
    box.next_step(rashid)  # accepted
    unit = engine.cases[rashid].owner.org
    engine.book_appointment(staff.coordinator(unit), rashid, clock.now() + timedelta(hours=1))
    clock.advance(timedelta(hours=2))
    engine.record_attendance(staff.coordinator(unit), rashid, attended=False)
    engine.tick(CLOCK_ACTOR)
    return box
