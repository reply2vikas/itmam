"""Property-based test: random care journeys must never break the ten invariants.

Hypothesis generates sequences of commands -- valid and invalid, from the right and the
wrong people, including the AI actor -- interleaved with clock jumps from a minute to four
months. After every step, every invariant is checked. Refused commands must change nothing,
and a list of known mistakes (wrong signer, wrong organisation, missing evidence, ...) is
tried at every stage: each one must be refused.

    make proptest          # quick profile
    make proptest-10k      # one seeded run of 10,000 journeys
    make proptest-100k     # ten seeded runs of 10,000 journeys
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any

from hypothesis import event
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    multiple,
    rule,
)

from itmam.domain import Actor, CommandRejected, NewSubject, Stage, check_all
from itmam.domain.audit import case_fingerprint
from itmam.domain.model import Side
from itmam.domain.roles import SUPERVISOR

from support import (
    AI,
    CLOCK,
    CODE,
    HOSPITAL,
    P1,
    P2,
    P3,
    SCREENING,
    SPECIALIST,
    SYSTEM,
    People,
    make_engine,
)

PATHWAY_IDS = [P1, P2, P3]
ORGS = [SCREENING, SPECIALIST, HOSPITAL]
RECEIVERS = [SPECIALIST, HOSPITAL]
ROLE_KINDS = ["coordinator", "clinician", "supervisor"]
OPEN_STAGES = [
    Stage.ISSUED,
    Stage.NOTIFIED,
    Stage.ACKNOWLEDGED,
    Stage.REFERRED,
    Stage.ACCEPTED,
    Stage.BOOKED,
    Stage.ATTENDED,
    Stage.OUTCOME_RECORDED,
]

actor_specs = st.tuples(st.sampled_from(ORGS), st.sampled_from(ROLE_KINDS), st.integers(1, 2))
subject_index = st.integers(0, 1)
minutes = st.one_of(
    st.integers(1, 180),  # minutes to hours
    st.integers(180, 60 * 24 * 4),  # hours to days
    st.integers(60 * 24 * 4, 60 * 24 * 120),  # days to four months
)


class CareJourneys(RuleBasedStateMachine):
    cases = Bundle("cases")

    def __init__(self) -> None:
        super().__init__()
        self.engine, self.clock = make_engine()  # a fresh engine for every generated journey
        self.people_counter = 0
        self.result_ids: list[str] = []

    # ------------------------------------------------------------- helpers

    def people(self, case_id: str) -> People:
        return People(self.engine.pathway_of(self.engine.cases[case_id]))

    def actor(self, case_id: str, spec: tuple[str, str, int]) -> Actor:
        org, kind, n = spec
        result: Actor = getattr(self.people(case_id), kind)(org, n)
        return result

    def contact(self, case_id: str, index: int) -> Actor:
        subjects = self.engine.cases[case_id].subjects
        return Actor.contact(subjects[index % len(subjects)].contact_id)

    def subject_id(self, case_id: str, index: int) -> str:
        subjects = self.engine.cases[case_id].subjects
        return subjects[index % len(subjects)].id

    def attempt(self, case_id: str | None, action: Callable[[], Any]) -> Any:
        """Run a command; if it is refused, nothing about the case or the trail may change."""
        case = self.engine.cases.get(case_id) if case_id else None
        before = case_fingerprint(case) if case else None
        events, ledger = len(self.engine.audit), self.engine.ledger()
        try:
            return action()
        except CommandRejected:
            if case is not None:
                assert case_fingerprint(case) == before, "a refused command changed the case"
            assert len(self.engine.audit) == events, "a refused command wrote to the audit trail"
            assert self.engine.ledger() == ledger, "a refused command changed the ledger"
            return None

    def owner_staff(self, case_id: str, kind: str, n: int = 1) -> Actor:
        owner = self.engine.cases[case_id].owner
        result: Actor = getattr(self.people(case_id), kind)(owner.org, n)
        return result

    # ------------------------------------------------------------- results

    @initialize(
        target=cases, pathway=st.sampled_from(PATHWAY_IDS), language=st.sampled_from(["ar", "en"])
    )
    def first_result(self, pathway: str, language: str) -> Any:
        return self.ingest(pathway, 0, language, duplicate=False)

    @rule(
        target=cases,
        pathway=st.sampled_from(PATHWAY_IDS),
        reuse=st.integers(0, 6),
        language=st.sampled_from(["ar", "en"]),
        duplicate=st.booleans(),
    )
    def ingest(self, pathway: str, reuse: int, language: str, duplicate: bool) -> Any:
        if duplicate and self.result_ids:
            result_id = self.result_ids[reuse % len(self.result_ids)]
        else:
            result_id = f"TEST-R-{len(self.result_ids) + 1:05d}"
            self.result_ids.append(result_id)
        count = 2 if pathway == P2 else 1
        subjects = []
        for _ in range(count):
            self.people_counter += 1
            n = self.people_counter
            subjects.append(NewSubject(f"TEST-P-{n:05d}", f"TEST-CONTACT-{n:05d}", language))
        case_id = self.attempt(
            None,
            lambda: self.engine.ingest_result(
                SYSTEM, result_id, CODE[pathway], subjects, SCREENING
            ),
        )
        return case_id if case_id else multiple()

    def fresh_case(self, pathway: str) -> str:
        """A new case outside the bundle, for checks that need one at a chosen stage."""
        case_id = self.ingest(pathway, 0, "en", duplicate=False)
        assert isinstance(case_id, str)
        return case_id

    @rule()
    def ingest_unactionable(self) -> None:
        self.attempt(
            None,
            lambda: self.engine.ingest_result(SYSTEM, "TEST-R-NEG", "FIT_NEGATIVE", [], SCREENING),
        )

    # ------------------------------------------------------- patient track

    @rule(case=cases, who=subject_index)
    def deliver(self, case: str, who: int) -> None:
        self.attempt(
            case, lambda: self.engine.record_delivery(SYSTEM, case, self.subject_id(case, who))
        )

    @rule(
        case=cases,
        who=subject_index,
        understood=st.booleans(),
        by_phone=st.booleans(),
        spec=actor_specs,
        someone_else=st.booleans(),
    )
    def acknowledge(
        self,
        case: str,
        who: int,
        understood: bool,
        by_phone: bool,
        spec: tuple[str, str, int],
        someone_else: bool,
    ) -> None:
        subject = self.subject_id(case, who)
        if by_phone:
            actor, via, notes = self.actor(case, spec), "phone", "Explained on the phone."
        else:
            actor, via, notes = self.contact(case, who + int(someone_else)), "link", ""
        self.attempt(
            case,
            lambda: self.engine.acknowledge(actor, case, subject, understood, via=via, notes=notes),
        )

    @rule(case=cases, who=subject_index)
    def consent(self, case: str, who: int) -> None:
        self.attempt(
            case,
            lambda: self.engine.record_consent(
                self.contact(case, who), case, self.subject_id(case, who)
            ),
        )

    @rule(case=cases, who=subject_index)
    def request_callback(self, case: str, who: int) -> None:
        self.attempt(case, lambda: self.engine.request_callback(self.contact(case, who), case))

    @rule(case=cases, spec=actor_specs)
    def complete_callback(self, case: str, spec: tuple[str, str, int]) -> None:
        pending = [c.id for c in self.engine.cases[case].callbacks if c.completed_at is None]
        callback = pending[0] if pending else "CB-none"
        self.attempt(
            case,
            lambda: self.engine.complete_callback(
                self.actor(case, spec), case, callback, "Called back."
            ),
        )

    @rule(
        case=cases,
        spec=actor_specs,
        channel=st.sampled_from(["phone", "sms", "letter", "fax"]),
        reached=st.booleans(),
    )
    def contact_attempt(
        self, case: str, spec: tuple[str, str, int], channel: str, reached: bool
    ) -> None:
        self.attempt(
            case,
            lambda: self.engine.record_contact_attempt(
                self.actor(case, spec), case, channel, reached
            ),
        )

    # ----------------------------------------------------- clinician track

    @rule(case=cases, spec=actor_specs)
    def raise_urgency(self, case: str, spec: tuple[str, str, int]) -> None:
        self.attempt(
            case,
            lambda: self.engine.raise_urgency(self.actor(case, spec), case, "clinical concern"),
        )

    @rule(case=cases, spec=actor_specs, to=st.sampled_from(ORGS))
    def econsult(self, case: str, spec: tuple[str, str, int], to: str) -> None:
        self.attempt(
            case,
            lambda: self.engine.request_econsult(
                self.actor(case, spec), case, to, "Any contraindication?"
            ),
        )

    @rule(case=cases, spec=actor_specs)
    def answer_econsult(self, case: str, spec: tuple[str, str, int]) -> None:
        open_consults = [
            c.id for c in self.engine.cases[case].consultations if c.answered_at is None
        ]
        consult = open_consults[0] if open_consults else "EC-none"
        self.attempt(
            case,
            lambda: self.engine.answer_econsult(self.actor(case, spec), case, consult, "Proceed."),
        )

    @rule(
        case=cases,
        spec=actor_specs,
        approver=actor_specs,
        to=st.sampled_from(ORGS),
        ai_approved=st.booleans(),
    )
    def refer(
        self,
        case: str,
        spec: tuple[str, str, int],
        approver: tuple[str, str, int],
        to: str,
        ai_approved: bool,
    ) -> None:
        note_approver = AI if ai_approved else self.actor(case, approver)
        self.attempt(
            case,
            lambda: self.engine.send_referral(
                self.actor(case, spec), case, to, "Hand-over note.", note_approver
            ),
        )

    @rule(case=cases, spec=actor_specs, accept=st.booleans())
    def decide_referral(self, case: str, spec: tuple[str, str, int], accept: bool) -> None:
        actor = self.actor(case, spec)
        if accept:
            self.attempt(case, lambda: self.engine.accept_referral(actor, case))
        else:
            self.attempt(case, lambda: self.engine.reject_referral(actor, case, "No capacity."))

    @rule(case=cases, spec=actor_specs, days=st.integers(-2, 30), joint=st.booleans())
    def book(self, case: str, spec: tuple[str, str, int], days: int, joint: bool) -> None:
        when = self.clock.now() + timedelta(days=days, hours=1)
        self.attempt(
            case,
            lambda: self.engine.book_appointment(self.actor(case, spec), case, when, joint=joint),
        )

    @rule(case=cases, spec=actor_specs, attended=st.booleans())
    def attendance(self, case: str, spec: tuple[str, str, int], attended: bool) -> None:
        self.attempt(
            case, lambda: self.engine.record_attendance(self.actor(case, spec), case, attended)
        )

    @rule(case=cases, spec=actor_specs, which=st.integers(0, 4))
    def outcome(self, case: str, spec: tuple[str, str, int], which: int) -> None:
        outcomes = self.engine.pathway_of(self.engine.cases[case]).outcomes + ["made_up"]
        self.attempt(
            case,
            lambda: self.engine.record_outcome(
                self.actor(case, spec), case, outcomes[which % len(outcomes)]
            ),
        )

    # ------------------------------------------------- pauses and owners

    @rule(
        case=cases,
        spec=actor_specs,
        which=st.integers(0, 6),
        days=st.integers(-1, 40),
        with_note=st.booleans(),
    )
    def pause(
        self, case: str, spec: tuple[str, str, int], which: int, days: int, with_note: bool
    ) -> None:
        reasons = [*self.engine.pathway_of(self.engine.cases[case]).pauses, "made_up"]
        reason = reasons[which % len(reasons)]
        wake = self.clock.now() + timedelta(days=days, minutes=1)
        self.attempt(
            case,
            lambda: self.engine.pause(
                self.actor(case, spec), case, reason, wake, note="Noted." if with_note else ""
            ),
        )

    @rule(case=cases, spec=actor_specs, by_clock=st.booleans())
    def resume(self, case: str, spec: tuple[str, str, int], by_clock: bool) -> None:
        actor = CLOCK if by_clock else self.actor(case, spec)
        self.attempt(case, lambda: self.engine.resume(actor, case))

    @rule(case=cases, spec=actor_specs, to_kind=st.sampled_from(["coordinator", "clinician"]))
    def reassign(self, case: str, spec: tuple[str, str, int], to_kind: str) -> None:
        p = self.engine.pathway_of(self.engine.cases[case])
        role = {"coordinator": p.roles.sender, "clinician": p.roles.sender_clinician}[to_kind]
        if self.engine.cases[case].owner.side == Side.RECEIVER:
            role = {"coordinator": p.roles.receiver, "clinician": p.roles.receiver_clinician}[
                to_kind
            ]
        self.attempt(
            case, lambda: self.engine.reassign(self.actor(case, spec), case, role, "Workload.")
        )

    # ----------------------------------------------------------- closure

    def evidence_for(self, case: str, closure: str, complete: bool) -> dict[str, str]:
        rule_ = self.engine.pathway_of(self.engine.cases[case]).closures[closure]
        evidence = {kind: f"TEST-{kind}" for kind in rule_.evidence}
        if "next_due_date" in evidence:
            evidence["next_due_date"] = (self.clock.now() + timedelta(days=200)).date().isoformat()
        if not complete and evidence:
            evidence.pop(next(iter(evidence)))
        return evidence

    @rule(
        case=cases,
        spec=actor_specs,
        which=st.integers(0, 6),
        complete=st.booleans(),
        transfer=st.sampled_from([None, HOSPITAL, SCREENING]),
    )
    def propose_closure(
        self,
        case: str,
        spec: tuple[str, str, int],
        which: int,
        complete: bool,
        transfer: str | None,
    ) -> None:
        closures = [*self.engine.pathway_of(self.engine.cases[case]).closures, "made_up"]
        closure = closures[which % len(closures)]
        evidence = self.evidence_for(case, closure, complete) if closure != "made_up" else {}
        self.attempt(
            case,
            lambda: self.engine.propose_closure(
                self.actor(case, spec), case, closure, evidence, transfer_to=transfer
            ),
        )

    @rule(case=cases, spec=actor_specs)
    def sign_closure(self, case: str, spec: tuple[str, str, int]) -> None:
        self.attempt(case, lambda: self.engine.sign_closure(self.actor(case, spec), case))

    @rule(case=cases, spec=actor_specs)
    def withdraw_closure(self, case: str, spec: tuple[str, str, int]) -> None:
        self.attempt(
            case,
            lambda: self.engine.withdraw_closure(self.actor(case, spec), case, "New information."),
        )

    # ---------------------------------------------------------------- AI

    @rule(case=cases, which=st.integers(0, 9))
    def ai_tries_to_act(self, case: str, which: int) -> None:
        e, now = self.engine, self.clock.now()
        subject = self.subject_id(case, 0)
        attempts: list[Callable[[], Any]] = [
            lambda: e.record_delivery(AI, case, subject),
            lambda: e.acknowledge(AI, case, subject, True),
            lambda: e.send_referral(AI, case, SPECIALIST, "note", AI),
            lambda: e.accept_referral(AI, case),
            lambda: e.pause(AI, case, "travelling", now + timedelta(days=1)),
            lambda: e.propose_closure(AI, case, "not_actionable", {"clinical_review_note": "x"}),
            lambda: e.sign_closure(AI, case),
            lambda: e.raise_urgency(AI, case, "x"),
            lambda: e.reassign(AI, case, SUPERVISOR, "x"),
            lambda: e.record_outcome(AI, case, "x"),
        ]
        before = case_fingerprint(e.cases[case])
        try:
            attempts[which]()
        except CommandRejected as refused:
            assert refused.code == "ai_not_permitted"
        else:
            raise AssertionError("the AI changed a case")
        assert case_fingerprint(e.cases[case]) == before

    @rule(
        case=cases,
        codes=st.lists(st.sampled_from(["fear", "transport", "language", "nope"]), max_size=2),
        accept=st.booleans(),
        spec=actor_specs,
    )
    def ai_suggests_then_person_reviews(
        self, case: str, codes: list[str], accept: bool, spec: tuple[str, str, int]
    ) -> None:
        suggestion = self.attempt(
            case, lambda: self.engine.suggest_barriers(AI, case, codes, "test-model")
        )
        if suggestion:
            self.attempt(
                case,
                lambda: self.engine.review_suggestion(self.actor(case, spec), suggestion, accept),
            )

    # ------------------------------------------------- the happy path

    @rule(
        case=cases,
        to=st.sampled_from(RECEIVERS),
        days=st.integers(1, 20),
        steps=st.integers(1, 10),
        surveillance=st.booleans(),
    )
    def next_correct_steps(
        self, case: str, to: str, days: int, steps: int, surveillance: bool
    ) -> None:
        """Take up to `steps` valid steps, so random journeys also reach deep stages."""
        for _ in range(steps):
            before = (self.engine.cases[case].stage, len(self.engine.audit))
            self.next_correct_step(case, to, days, surveillance)
            if (self.engine.cases[case].stage, len(self.engine.audit)) == before:
                break

    @rule(case=cases, which=st.integers(0, 5), days=st.integers(1, 6))
    def pause_correctly(self, case: str, which: int, days: int) -> None:
        """A valid pause: known reason, a clinician approves, wake-up inside every limit."""
        c = self.engine.cases[case]
        if not c.is_open or c.is_paused:
            return
        reasons = list(self.engine.pathway_of(c).pauses)
        reason = reasons[which % len(reasons)]
        wake = self.clock.now() + timedelta(days=days)
        self.attempt(
            case,
            lambda: self.engine.pause(
                self.owner_staff(case, "clinician"), case, reason, wake, note="Noted."
            ),
        )

    def next_correct_step(self, case: str, to: str, days: int, surveillance: bool = False) -> None:
        c = self.engine.cases[case]
        if not c.is_open or c.is_paused or c.pending_closure is not None:
            return
        people = self.people(case)
        e = self.engine
        if c.stage == Stage.ISSUED:
            waiting = [s for s in c.subjects if s.notified_at is None]
            self.attempt(case, lambda: e.record_delivery(SYSTEM, case, waiting[0].id))
        elif c.stage == Stage.NOTIFIED:
            s = next(s for s in c.subjects if not (s.acknowledged_at and s.understood))
            self.attempt(case, lambda: e.acknowledge(Actor.contact(s.contact_id), case, s.id, True))
        elif c.stage == Stage.ACKNOWLEDGED:
            self.attempt(
                case,
                lambda: e.send_referral(
                    people.coordinator(SCREENING),
                    case,
                    to,
                    "Hand-over note.",
                    people.clinician(SCREENING),
                ),
            )
        elif c.stage == Stage.REFERRED:
            referral = c.open_referral()
            assert referral is not None
            self.attempt(case, lambda: e.accept_referral(people.coordinator(referral.to_org), case))
        elif c.stage == Stage.ACCEPTED:
            self.attempt(
                case,
                lambda: e.book_appointment(
                    self.owner_staff(case, "coordinator"),
                    case,
                    self.clock.now() + timedelta(days=days),
                ),
            )
        elif c.stage == Stage.BOOKED:
            appointment = c.appointments[-1].at
            if self.clock.now() < appointment:
                self.clock.set(appointment + timedelta(minutes=30))
            self.attempt(
                case, lambda: e.record_attendance(self.owner_staff(case, "coordinator"), case, True)
            )
        elif c.stage == Stage.ATTENDED:
            outcome = e.pathway_of(c).outcomes[0]
            self.attempt(
                case, lambda: e.record_outcome(self.owner_staff(case, "clinician"), case, outcome)
            )
        elif c.stage == Stage.OUTCOME_RECORDED:
            watched = surveillance and "surveillance" in e.pathway_of(c).closures
            self.close_correctly(case, "surveillance" if watched else "care_completed")

    @rule(case=cases, which=st.integers(0, 5), sign_all=st.booleans())
    def close_correctly(self, case: str, which: int | str = 0, sign_all: bool = True) -> None:
        """Propose a closure with full evidence (or continue a pending one) and collect valid,
        distinct signatures. With `sign_all=False` it stops after one signature."""
        c = self.engine.cases[case]
        if not c.is_open:
            return
        p = self.engine.pathway_of(c)
        e = self.engine
        if c.pending_closure is None:
            names = list(p.closures)
            closure = which if isinstance(which, str) else names[which % len(names)]
            coordinator = self.owner_staff(case, "coordinator")
            needed = p.closures[closure].min_contact_attempts or 0
            for _ in range(needed - len(c.contact_attempts)):
                self.attempt(
                    case, lambda: e.record_contact_attempt(coordinator, case, "phone", False)
                )
            transfer = HOSPITAL if c.owner.org != HOSPITAL else SPECIALIST
            self.attempt(
                case,
                lambda: e.propose_closure(
                    coordinator,
                    case,
                    closure,
                    self.evidence_for(case, closure, True),
                    transfer_to=transfer,
                ),
            )
        if c.pending_closure is None:
            return
        rule_ = p.closures[c.pending_closure.type]
        candidates = {
            "coordinator": [self.owner_staff(case, "coordinator", 2)],
            "clinician": [
                self.owner_staff(case, "clinician", 1),
                self.owner_staff(case, "clinician", 2),
            ],
            "receiver_clinician": [self.owner_staff(case, "clinician", 1)],
            "sender_clinician": [self.owner_staff(case, "clinician", 1)],
            "supervisor": [self.owner_staff(case, "supervisor", 1)],
        }
        for token in rule_.signers:
            for signer in candidates[token]:
                pending = c.pending_closure
                if pending is None:
                    return
                if any(s.actor.id == signer.id for s in pending.signatures):
                    continue
                before = len(pending.signatures)
                self.attempt(case, partial(e.sign_closure, signer, case))
                after = len(c.pending_closure.signatures) if c.pending_closure else before + 1
                if after > before:
                    if not sign_all:
                        return
                    break

    # ------------------------------------------------- known mistakes

    def mistakes_for(self, case: str, kind: str) -> list[tuple[str, Callable[[], Any]]]:
        """Plausible wrong moves for the case's current stage. Each must be refused."""
        e, c = self.engine, self.engine.cases[case]
        p = e.pathway_of(c)
        if c.pending_closure is not None:
            signed = c.pending_closure.signatures
            if signed and len(signed) < len(p.closures[c.pending_closure.type].signers):
                first = signed[0].actor
                return [("the same person signs twice", lambda: e.sign_closure(first, case))]
            return []
        return self.stage_mistakes(case) if kind == "stage" else self.common_mistakes(case)

    def common_mistakes(self, case: str) -> list[tuple[str, Callable[[], Any]]]:
        e, c, now = self.engine, self.engine.cases[case], self.clock.now()
        owner_coordinator = self.owner_staff(case, "coordinator")
        return [
            (
                "a closure without evidence",
                lambda: e.propose_closure(owner_coordinator, case, "not_actionable", {}),
            ),
            (
                "a coordinator reassigns",
                lambda: e.reassign(owner_coordinator, case, c.owner.role, "Workload."),
            ),
            (
                "a pause for a made-up reason",
                lambda: e.pause(owner_coordinator, case, "made_up", now + timedelta(days=1)),
            ),
            (
                "a pause longer than allowed",
                lambda: e.pause(
                    self.owner_staff(case, "clinician"),
                    case,
                    "other",
                    now + timedelta(days=60),
                    note="Too long.",
                ),
            ),
        ]

    def stage_mistakes(self, case: str) -> list[tuple[str, Callable[[], Any]]]:
        e, c, now = self.engine, self.engine.cases[case], self.clock.now()
        people = self.people(case)
        p = e.pathway_of(c)
        sender, sender_clinician = people.coordinator(SCREENING), people.clinician(SCREENING)
        if c.stage == Stage.ISSUED:
            waiting = next(s for s in c.subjects if s.notified_at is None)
            return [
                (
                    "delivery to a stranger",
                    lambda: e.record_delivery(SYSTEM, case, "TEST-P-NOBODY"),
                ),
                ("delivery by fax", lambda: e.record_delivery(SYSTEM, case, waiting.id, "fax")),
                ("a person fakes a delivery", lambda: e.record_delivery(sender, case, waiting.id)),
            ]
        if c.stage == Stage.NOTIFIED:
            s = next(s for s in c.subjects if not (s.acknowledged_at and s.understood))
            stranger = Actor.contact("TEST-CONTACT-STRANGER")
            return [
                ("someone else's link", lambda: e.acknowledge(stranger, case, s.id, True)),
                (
                    "a call without notes",
                    lambda: e.acknowledge(sender, case, s.id, True, via="phone"),
                ),
                (
                    "the receiving team acknowledges",
                    lambda: e.acknowledge(
                        people.coordinator(SPECIALIST), case, s.id, True, via="phone", notes="x"
                    ),
                ),
            ]
        if c.stage == Stage.ACKNOWLEDGED:
            return [
                (
                    "a referral to itself",
                    lambda: e.send_referral(sender, case, SCREENING, "Note.", sender_clinician),
                ),
                (
                    "an AI-approved note",
                    lambda: e.send_referral(sender, case, SPECIALIST, "Note.", AI),
                ),
                (
                    "an empty note",
                    lambda: e.send_referral(sender, case, SPECIALIST, "  ", sender_clinician),
                ),
                (
                    "a note approved by another organisation",
                    lambda: e.send_referral(
                        sender, case, SPECIALIST, "Note.", people.clinician(SPECIALIST)
                    ),
                ),
            ]
        if c.stage == Stage.REFERRED:
            referral = c.open_referral()
            assert referral is not None
            other = HOSPITAL if referral.to_org == SPECIALIST else SPECIALIST
            return [
                ("the sender accepts its own referral", lambda: e.accept_referral(sender, case)),
                (
                    "the wrong organisation accepts",
                    lambda: e.accept_referral(people.coordinator(other), case),
                ),
                (
                    "a rejection without a reason",
                    lambda: e.reject_referral(people.coordinator(referral.to_org), case, " "),
                ),
            ]
        owner_coordinator = self.owner_staff(case, "coordinator")
        owner_clinician = self.owner_staff(case, "clinician")
        if c.stage == Stage.ACCEPTED:
            joint_blocked = p.subjects.count < 2 or any(
                "joint_session" not in s.consents for s in c.subjects
            )
            mistakes = [
                (
                    "an appointment in the past",
                    lambda: e.book_appointment(owner_coordinator, case, now - timedelta(hours=1)),
                ),
                (
                    "the sender books",
                    lambda: e.book_appointment(sender, case, now + timedelta(days=2)),
                ),
            ]
            if joint_blocked:
                mistakes.append(
                    (
                        "a joint session without consent",
                        lambda: e.book_appointment(
                            owner_coordinator, case, now + timedelta(days=2), joint=True
                        ),
                    )
                )
            return mistakes
        if c.stage == Stage.BOOKED:
            mistakes = [
                ("the sender records attendance", lambda: e.record_attendance(sender, case, True))
            ]
            if now < c.appointments[-1].at:
                mistakes.append(
                    (
                        "attendance before the appointment",
                        lambda: e.record_attendance(owner_coordinator, case, True),
                    )
                )
            return mistakes
        if c.stage == Stage.ATTENDED:
            return [
                (
                    "a coordinator records the outcome",
                    lambda: e.record_outcome(owner_coordinator, case, p.outcomes[0]),
                ),
                ("an unknown outcome", lambda: e.record_outcome(owner_clinician, case, "made_up")),
            ]
        if c.stage == Stage.OUTCOME_RECORDED:
            mistakes = [
                (
                    "a closure with missing evidence",
                    lambda: e.propose_closure(
                        owner_clinician,
                        case,
                        "care_completed",
                        self.evidence_for(case, "care_completed", False),
                    ),
                ),
                (
                    "another organisation closes",
                    lambda: e.propose_closure(
                        sender,
                        case,
                        "care_completed",
                        self.evidence_for(case, "care_completed", True),
                    ),
                ),
            ]
            if "surveillance" in p.closures:
                past = (now - timedelta(days=1)).date().isoformat()
                evidence = {**self.evidence_for(case, "surveillance", True), "next_due_date": past}
                mistakes.append(
                    (
                        "a surveillance date in the past",
                        lambda: e.propose_closure(owner_clinician, case, "surveillance", evidence),
                    )
                )
            return mistakes
        return []

    @rule(
        case=cases,
        which=st.integers(0, 5),
        goal=st.sampled_from(OPEN_STAGES),
        to=st.sampled_from(RECEIVERS),
    )
    def a_stage_mistake_is_refused(self, case: str, which: int, goal: Stage, to: str) -> None:
        """Walk a case to a chosen stage, then try a plausible wrong move for that stage."""
        c = self.engine.cases[case]
        usable = c.is_open and not c.is_paused and c.pending_closure is None
        if not usable or OPEN_STAGES.index(c.stage) > OPEN_STAGES.index(goal):
            case = self.fresh_case(PATHWAY_IDS[which % len(PATHWAY_IDS)])
            c = self.engine.cases[case]
        while c.is_open and OPEN_STAGES.index(c.stage) < OPEN_STAGES.index(goal):
            before = (c.stage, len(self.engine.audit))
            self.next_correct_step(case, to, 3)
            if (c.stage, len(self.engine.audit)) == before:
                break
        self.refuse_one(case, "stage", which)

    @rule(case=cases, which=st.integers(0, 5))
    def a_common_mistake_is_refused(self, case: str, which: int) -> None:
        self.refuse_one(case, "common", which)

    def refuse_one(self, case: str, kind: str, which: int) -> None:
        c = self.engine.cases[case]
        if not c.is_open or c.is_paused:
            return
        mistakes = self.mistakes_for(case, kind)
        if not mistakes:
            return
        label, action = mistakes[which % len(mistakes)]
        self.must_refuse(case, label, action)

    def must_refuse(self, case: str, label: str, action: Callable[[], Any]) -> None:
        """A known mistake must be refused, and the refusal must change nothing."""
        c = self.engine.cases[case]
        before, events, ledger = case_fingerprint(c), len(self.engine.audit), self.engine.ledger()
        try:
            action()
        except CommandRejected:
            pass
        else:
            raise AssertionError(f"a mistake was accepted: {label}")
        assert case_fingerprint(c) == before, f"a refused mistake changed the case: {label}"
        assert len(self.engine.audit) == events, f"a refused mistake wrote to the audit: {label}"
        assert self.engine.ledger() == ledger, f"a refused mistake changed the ledger: {label}"

    @rule(case=cases)
    def urgent_cases_need_a_clinician_to_pause(self, case: str) -> None:
        """A clinician makes the case urgent; from then on a coordinator may not pause it,
        even for a reason a coordinator could approve on a routine case."""
        c, e = self.engine.cases[case], self.engine
        if not c.is_open or c.is_paused:
            return
        if c.urgency != "urgent":
            clinician = self.owner_staff(case, "clinician")
            self.attempt(case, lambda: e.raise_urgency(clinician, case, "Red-flag symptom."))
        if c.urgency != "urgent":
            return
        coordinator = self.owner_staff(case, "coordinator")
        wake = self.clock.now() + timedelta(days=1)
        self.must_refuse(
            case,
            "a coordinator pauses an urgent case",
            lambda: e.pause(coordinator, case, "travelling", wake, note="Away."),
        )

    @rule(case=cases, decline=st.booleans())
    def double_signing_is_refused(self, case: str, decline: bool) -> None:
        """Two-signature closures: one person can never provide both signatures."""
        c, e = self.engine.cases[case], self.engine
        if not c.is_open or c.pending_closure is not None:
            return
        closure = "informed_decline" if decline else "not_actionable"
        coordinator = self.owner_staff(case, "coordinator")
        evidence = self.evidence_for(case, closure, True)
        self.attempt(case, lambda: e.propose_closure(coordinator, case, closure, evidence))
        if c.pending_closure is None:
            return
        first = self.owner_staff(case, "clinician")
        self.attempt(case, lambda: e.sign_closure(first, case))
        if c.pending_closure is not None and c.pending_closure.signatures:
            self.must_refuse(
                case, "the same person signs twice", lambda: e.sign_closure(first, case)
            )
        if c.pending_closure is not None:  # withdraw, so the case can carry on
            self.attempt(case, lambda: e.withdraw_closure(coordinator, case, "Check done."))

    # -------------------------------------------------------------- time

    @rule(delta=minutes)
    def time_passes(self, delta: int) -> None:
        self.clock.advance(timedelta(minutes=delta))
        self.engine.tick(CLOCK)

    @rule()
    def tick(self) -> None:
        self.engine.tick(CLOCK)

    # --------------------------------------------------------- the rules

    @invariant()
    def all_ten_invariants_hold(self) -> None:
        violations = check_all(self.engine)
        assert not violations, "\n".join(str(v) for v in violations)

    def teardown(self) -> None:
        """Label each journey so `--hypothesis-show-statistics` shows how deep the runs go."""
        order = list(Stage)
        cases = list(self.engine.cases.values())
        deepest = max(
            (max(order.index(stage) for stage, _ in c.stage_history) for c in cases), default=-1
        )
        event(f"deepest stage: {order[deepest].value if deepest >= 0 else 'no case'}")
        for case in cases:
            for stage in {stage for stage, _ in case.stage_history}:
                event(f"a case reached {stage.value}")
            if case.closure is not None:
                event(f"closed as {case.closure.type}")
            if case.escalations:
                event(f"escalated to L{max(e.level for e in case.escalations)}")
            if case.pause_history:
                event("paused and resumed")
        if self.engine.watches:
            event("surveillance watch created")


TestCareJourneys = CareJourneys.TestCase
