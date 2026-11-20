"""The case engine: who may do what, when, and what happens when nothing happens.

Every public method is a command. A command checks all of its guards first and only then
changes state, so a refused command leaves no trace except an entry in `rejections`.
"""

from __future__ import annotations

import functools
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, TypeVar, cast

from itmam.domain.audit import AuditLog, case_fingerprint
from itmam.domain.calendar import UAE_CALENDAR, Duration, WorkCalendar
from itmam.domain.clock import Clock
from itmam.domain.errors import CommandRejected, Rejection
from itmam.domain.evidence import ladder_problems
from itmam.domain.evidence import tiers as evidence_tiers
from itmam.domain.model import (
    CLOCK_FOR_STAGE,
    Actor,
    ActorKind,
    AISuggestion,
    Appointment,
    Callback,
    Case,
    Closure,
    ClosureProposal,
    Consultation,
    ContactAttempt,
    Escalation,
    Expectation,
    ExpectationStatus,
    Message,
    Outcome,
    Owner,
    OwnerChange,
    Pause,
    Referral,
    Side,
    Signature,
    Stage,
    Subject,
    Watch,
)
from itmam.domain.pathway import ClosureRule, PathwayConfig
from itmam.domain.roles import SUPERVISOR, is_clinician, owner_staff, side_roles, token_satisfied
from itmam.domain.templates import TemplateRef, TemplateRegistry

ARMED = ExpectationStatus.ARMED
SUSPENDED = ExpectationStatus.SUSPENDED
CONTACT_CHANNELS = frozenset({"phone", "sms", "whatsapp", "email", "letter"})
CONSENT_KINDS = frozenset({"joint_session"})
# When one person could fill several sign-off slots, the most specific slot is used first,
# so a supervisor signing first does not use up the general "clinician" slot.
SLOT_SPECIFICITY = {
    "supervisor": 0,
    "sender_clinician": 1,
    "receiver_clinician": 1,
    "clinician": 2,
    "coordinator": 3,
}

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class NewSubject:
    id: str
    contact_id: str
    language: str


def command(name: str) -> Callable[[F], F]:
    """Log every refusal of a command before passing it on to the caller."""

    def decorate(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(self: Engine, actor: Actor, *args: Any, **kwargs: Any) -> Any:
            try:
                return fn(self, actor, *args, **kwargs)
            except CommandRejected as exc:
                target = args[0] if args and isinstance(args[0], str) else None
                self.rejections.append(
                    Rejection(
                        at=self.clock.now(),
                        command=name,
                        target=target,
                        actor_kind=actor.kind.value,
                        actor_id=actor.id,
                        code=exc.code,
                        message=exc.message,
                    )
                )
                raise

        return cast(F, wrapper)

    return decorate


class Engine:
    def __init__(
        self,
        pathways: Mapping[str, PathwayConfig],
        clock: Clock,
        templates: TemplateRegistry,
        calendar: WorkCalendar = UAE_CALENDAR,
    ) -> None:
        self.pathways = dict(pathways)
        self.clock = clock
        self.templates = templates
        self.calendar = calendar
        self.cases: dict[str, Case] = {}
        self.results: dict[str, str] = {}  # result ID -> case ID: the reconciliation ledger
        self.audit = AuditLog()
        self.rejections: list[Rejection] = []
        self.watches: list[Watch] = []
        self.suggestions: dict[str, AISuggestion] = {}
        self._pathway_for_code = {
            code: p.id for p in self.pathways.values() for code in p.trigger.result_codes
        }
        self._ids: dict[str, itertools.count[int]] = {}  # one counter per id prefix

    # ------------------------------------------------------------------ reading

    def pathway_of(self, case: Case) -> PathwayConfig:
        return self.pathways[case.pathway_id]

    def ledger(self) -> dict[str, int]:
        """Results received must equal cases open plus cases closed (invariant I8)."""
        closed = sum(1 for case in self.cases.values() if not case.is_open)
        return {"received": len(self.results), "open": len(self.cases) - closed, "closed": closed}

    # ---------------------------------------------------------------- results in

    @command("ingest_result")
    def ingest_result(
        self,
        actor: Actor,
        result_id: str,
        result_code: str,
        subjects: Sequence[NewSubject],
        sender_org: str,
        issued_at: datetime | None = None,
    ) -> str:
        """Open a case for an actionable result. Sending the same result ID twice is harmless."""
        self._no_ai(actor)
        if actor.kind not in (ActorKind.SYSTEM, ActorKind.HUMAN):
            raise CommandRejected("not_permitted", "results come from a lab feed or a staff upload")
        if result_id in self.results:
            return self.results[result_id]
        pathway_id = self._pathway_for_code.get(result_code)
        if pathway_id is None:
            raise CommandRejected(
                "not_actionable", f"result code {result_code!r} starts no pathway"
            )
        p = self.pathways[pathway_id]
        if len(subjects) != p.subjects.count:
            raise CommandRejected("wrong_subjects", f"{p.id} needs {p.subjects.count} subject(s)")
        if len({s.id for s in subjects}) != len(subjects):
            raise CommandRejected("wrong_subjects", "subject IDs must be distinct")
        for s in subjects:
            if s.language not in p.languages:
                raise CommandRejected("unsupported_language", f"{s.language!r} is not offered")
            if not s.id or not s.contact_id:
                raise CommandRejected("wrong_subjects", "every subject needs an ID and a contact")
        if not sender_org:
            raise CommandRejected("missing_org", "the screening organisation is required")
        now = self.clock.now()
        issued = issued_at or now
        if issued > now:
            raise CommandRejected("future_result", "a result cannot be issued in the future")
        return self._open(p, result_id, result_code, subjects, sender_org, issued, actor, None)

    # ---------------------------------------------------------- patient track

    @command("record_delivery")
    def record_delivery(
        self, actor: Actor, case_id: str, subject_id: str, channel: str = "sms"
    ) -> None:
        """The channel adapter confirms the result notice reached one subject."""
        self._no_ai(actor)
        if actor.kind != ActorKind.SYSTEM:
            raise CommandRejected(
                "not_permitted", "deliveries are confirmed by the channel adapter"
            )
        case = self._active(case_id)
        self._require_stage(case, Stage.ISSUED)
        subject = self._subject(case, subject_id)
        if subject.notified_at is not None:
            raise CommandRejected("already_notified", "this person already has the result notice")
        if channel not in CONTACT_CHANNELS:
            raise CommandRejected("unknown_channel", f"{channel!r} is not a channel")
        ref = self.templates.latest_approved(
            case.pathway_id, "result_notice", subject.language, at=self.clock.now()
        )
        if ref is None:
            raise CommandRejected(
                "template_not_approved", "no clinically approved result notice in this language"
            )
        now = self.clock.now()
        subject.notified_at = now
        self._store_message(case, subject, ref, channel)
        seq = self._record(
            case,
            actor,
            "message_delivered",
            subject=subject.id,
            message="result_notice",
            version=ref.version,
            channel=channel,
        )
        self._advance_patient_track(case, seq)

    @command("acknowledge")
    def acknowledge(
        self,
        actor: Actor,
        case_id: str,
        subject_id: str,
        understood: bool,
        via: str = "link",
        notes: str = "",
    ) -> None:
        """The person opened the result and took the understanding check (or a coordinator
        records the same after a phone call)."""
        self._no_ai(actor)
        case = self._active(case_id)
        p = self.pathway_of(case)
        subject = self._subject(case, subject_id)
        if via == "link":
            if actor.kind != ActorKind.PATIENT or actor.id != subject.contact_id:
                raise CommandRejected(
                    "not_permitted", "only this person's own link can acknowledge"
                )
        elif via == "phone":
            if not owner_staff(p, case.owner, actor):
                raise CommandRejected("not_permitted", "only the owning team records phone calls")
            if not notes.strip():
                raise CommandRejected("notes_required", "record what was discussed on the call")
        else:
            raise CommandRejected("invalid_channel", "acknowledge via 'link' or 'phone'")
        if case.stage not in (Stage.ISSUED, Stage.NOTIFIED):
            raise CommandRejected("wrong_stage", f"case is {case.stage.value}")
        if subject.notified_at is None:
            raise CommandRejected("not_notified", "the result notice has not reached this person")
        if subject.acknowledged_at is not None and subject.understood:
            raise CommandRejected("already_acknowledged", "this person already understood")
        subject.acknowledged_at = self.clock.now()
        subject.understood = understood
        if understood:
            seq = self._record(case, actor, "acknowledged", subject=subject.id, via=via)
            self._settle(case, "call_task", ref=subject.id)
        else:
            seq = self._record(
                case, actor, "understanding_check_failed", subject=subject.id, via=via
            )
            if not self._has_armed(case, "call_task", subject.id):
                self._arm(
                    case,
                    "call_task",
                    p.call_task.breach.add_to(self.clock.now(), self.calendar),
                    ref=subject.id,
                )
        self._advance_patient_track(case, seq)

    @command("record_consent")
    def record_consent(
        self, actor: Actor, case_id: str, subject_id: str, kind: str = "joint_session"
    ) -> None:
        self._no_ai(actor)
        case = self._open_case(case_id)
        subject = self._subject(case, subject_id)
        if actor.kind != ActorKind.PATIENT or actor.id != subject.contact_id:
            raise CommandRejected("not_permitted", "only the person can give their own consent")
        if kind not in CONSENT_KINDS:
            raise CommandRejected("unknown_consent", f"{kind!r} is not a consent type")
        if kind in subject.consents:
            raise CommandRejected("already_consented", "consent is already recorded")
        subject.consents.add(kind)
        self._record(case, actor, "consent_recorded", subject=subject.id, kind=kind)

    @command("request_callback")
    def request_callback(self, actor: Actor, case_id: str, note: str = "") -> str:
        """'Call me' on the journey page: the owning team gets its own clock to call back."""
        self._no_ai(actor)
        case = self._open_case(case_id)
        if actor.kind != ActorKind.PATIENT or actor.id not in {s.contact_id for s in case.subjects}:
            raise CommandRejected("not_permitted", "call-backs are requested from a journey link")
        now = self.clock.now()
        callback = Callback(
            id=self._new_id("CB"), requested_by=actor.id, requested_at=now, note=note
        )
        case.callbacks.append(callback)
        self._record(case, actor, "callback_requested", callback=callback.id)
        breach = self.pathway_of(case).callback.breach.add_to(now, self.calendar)
        self._arm(case, "callback", breach, ref=callback.id)
        return callback.id

    @command("complete_callback")
    def complete_callback(self, actor: Actor, case_id: str, callback_id: str, note: str) -> None:
        self._no_ai(actor)
        case = self._open_case(case_id)
        callback = next((c for c in case.callbacks if c.id == callback_id), None)
        if callback is None:
            raise CommandRejected("unknown_callback", f"no call-back {callback_id}")
        if callback.completed_at is not None:
            raise CommandRejected("already_completed", "this call-back is already done")
        if not owner_staff(self.pathway_of(case), case.owner, actor):
            raise CommandRejected("not_permitted", "only the owning team completes call-backs")
        if not note.strip():
            raise CommandRejected("notes_required", "record what was discussed")
        now = self.clock.now()
        callback.completed_at = now
        callback.completed_by = actor
        case.contact_attempts.append(ContactAttempt(now, actor.id, "phone", True))
        self._record(case, actor, "callback_completed", callback=callback.id)
        self._settle(case, "callback", ref=callback.id)

    @command("record_contact_attempt")
    def record_contact_attempt(
        self, actor: Actor, case_id: str, channel: str, reached: bool
    ) -> None:
        self._no_ai(actor)
        case = self._open_case(case_id)
        if not owner_staff(self.pathway_of(case), case.owner, actor):
            raise CommandRejected("not_permitted", "only the owning team logs contact attempts")
        if channel not in CONTACT_CHANNELS:
            raise CommandRejected("unknown_channel", f"{channel!r} is not a channel")
        case.contact_attempts.append(ContactAttempt(self.clock.now(), actor.id, channel, reached))
        self._record(case, actor, "contact_attempt", channel=channel, reached=reached)

    # ------------------------------------------------------- clinician track

    @command("raise_urgency")
    def raise_urgency(self, actor: Actor, case_id: str, reason: str) -> None:
        """Only a clinician can make a case urgent; nothing can make it less urgent."""
        self._no_ai(actor)
        case = self._open_case(case_id)
        if case.is_paused:
            raise CommandRejected("case_paused", "resume the case before changing urgency")
        p = self.pathway_of(case)
        if not (owner_staff(p, case.owner, actor) and is_clinician(p, actor)):
            raise CommandRejected("not_permitted", "only a clinician in the owning team")
        if case.urgency == "urgent":
            raise CommandRejected("already_urgent", "the case is already urgent")
        if not reason.strip():
            raise CommandRejected("reason_required", "say why the case is urgent")
        case.urgency = "urgent"
        self._record(case, actor, "urgency_raised", reason=reason)

    @command("request_econsult")
    def request_econsult(self, actor: Actor, case_id: str, to_org: str, question: str) -> str:
        """Ask the specialist before referring. The case keeps its owner."""
        self._no_ai(actor)
        case = self._active(case_id)
        p = self.pathway_of(case)
        if not p.econsult.allowed:
            raise CommandRejected("econsult_disabled", "this pathway has no e-consults")
        self._require_stage(case, Stage.ACKNOWLEDGED)
        if not (owner_staff(p, case.owner, actor) and actor.role == p.roles.sender_clinician):
            raise CommandRejected("not_permitted", "e-consults come from the sending clinician")
        if not to_org or to_org == case.owner.org:
            raise CommandRejected("invalid_target", "ask a different organisation")
        if not question.strip():
            raise CommandRejected("question_required", "write the question")
        now = self.clock.now()
        consult = Consultation(
            id=self._new_id("EC"), to_org=to_org, question=question, asked_by=actor, asked_at=now
        )
        case.consultations.append(consult)
        self._record(case, actor, "econsult_requested", consult=consult.id, to_org=to_org)
        self._arm(
            case,
            "econsult_reply",
            p.econsult.reply_breach.add_to(now, self.calendar),
            ref=consult.id,
        )
        return consult.id

    @command("answer_econsult")
    def answer_econsult(self, actor: Actor, case_id: str, consult_id: str, answer: str) -> None:
        self._no_ai(actor)
        case = self._open_case(case_id)
        p = self.pathway_of(case)
        consult = next((c for c in case.consultations if c.id == consult_id), None)
        if consult is None:
            raise CommandRejected("unknown_consult", f"no e-consult {consult_id}")
        if consult.answered_at is not None:
            raise CommandRejected("already_answered", "this e-consult is answered")
        if not (
            actor.kind == ActorKind.HUMAN
            and actor.org == consult.to_org
            and actor.role in {p.roles.receiver_clinician, SUPERVISOR}
        ):
            raise CommandRejected("not_permitted", "the specialist who was asked must answer")
        if not answer.strip():
            raise CommandRejected("answer_required", "write the answer")
        consult.answer = answer
        consult.answered_by = actor
        consult.answered_at = self.clock.now()
        self._record(case, actor, "econsult_answered", consult=consult.id)
        self._settle(case, "econsult_reply", ref=consult.id)

    @command("send_referral")
    def send_referral(
        self, actor: Actor, case_id: str, to_org: str, note: str, note_approved_by: Actor
    ) -> str:
        """Hand the case to another organisation. The sender stays the owner until it accepts."""
        self._no_ai(actor)
        case = self._active(case_id)
        p = self.pathway_of(case)
        self._require_stage(case, Stage.ACKNOWLEDGED)
        if not owner_staff(p, case.owner, actor):
            raise CommandRejected("not_permitted", "only the owning team refers")
        if not owner_staff(p, case.owner, note_approved_by):
            raise CommandRejected(
                "note_not_approved", "a person in the sending team must approve the hand-over note"
            )
        if not to_org or to_org == case.owner.org:
            raise CommandRejected("invalid_target", "refer to a different organisation")
        if not note.strip():
            raise CommandRejected("note_required", "the hand-over note is empty")
        referral = Referral(
            id=self._new_id("RF"),
            to_org=to_org,
            sent_at=self.clock.now(),
            sent_by=actor,
            note=note,
            note_approved_by=note_approved_by,
        )
        case.referrals.append(referral)
        seq = self._record(
            case,
            actor,
            "referral_sent",
            referral=referral.id,
            to_org=to_org,
            note_approved_by=note_approved_by.id,
        )
        self._move(case, Stage.REFERRED, seq)
        return referral.id

    @command("accept_referral")
    def accept_referral(self, actor: Actor, case_id: str) -> None:
        """Acceptance is the only way ownership crosses organisations (invariant I4)."""
        self._no_ai(actor)
        case = self._active(case_id)
        p = self.pathway_of(case)
        self._require_stage(case, Stage.REFERRED)
        referral = self._open_referral(case)
        self._require_receiver(p, referral, actor)
        now = self.clock.now()
        referral.status = "accepted"
        referral.decided_at = now
        referral.decided_by = actor
        before = case.owner
        case.owner = Owner(referral.to_org, p.roles.receiver, Side.RECEIVER)
        seq = self._record(
            case,
            actor,
            "referral_accepted",
            referral=referral.id,
            from_owner=before,
            to_owner=case.owner,
        )
        case.owner_history.append(OwnerChange(now, before, case.owner, "accepted", actor, seq))
        self._move(case, Stage.ACCEPTED, seq)

    @command("reject_referral")
    def reject_referral(self, actor: Actor, case_id: str, reason: str) -> None:
        """A rejection returns the case to the sender's queue with its own re-route clock."""
        self._no_ai(actor)
        case = self._active(case_id)
        p = self.pathway_of(case)
        self._require_stage(case, Stage.REFERRED)
        referral = self._open_referral(case)
        self._require_receiver(p, referral, actor)
        if not reason.strip():
            raise CommandRejected("reason_required", "a rejection needs a reason")
        on_reject = p.stages["accept"].on_reject
        assert on_reject is not None  # guaranteed by pathway validation
        referral.status = "rejected"
        referral.decided_at = self.clock.now()
        referral.decided_by = actor
        referral.reason = reason
        case.rejections += 1
        seq = self._record(
            case,
            actor,
            "referral_rejected",
            referral=referral.id,
            reason=reason,
            rejections=case.rejections,
        )
        exp = self._move(case, Stage.ACKNOWLEDGED, seq, breach_override=on_reject.reroute_breach)
        if case.rejections >= 2:
            self._raise_level(case, exp, 2, "second referral rejection", Actor.system("rules"))

    @command("book_appointment")
    def book_appointment(
        self, actor: Actor, case_id: str, appointment_at: datetime, joint: bool = False
    ) -> None:
        self._no_ai(actor)
        case = self._active(case_id)
        p = self.pathway_of(case)
        self._require_stage(case, Stage.ACCEPTED)
        if not owner_staff(p, case.owner, actor):
            raise CommandRejected("not_permitted", "only the receiving team books")
        now = self.clock.now()
        if appointment_at <= now:
            raise CommandRejected("appointment_in_past", "book a time in the future")
        if joint:
            if p.subjects.count < 2:
                raise CommandRejected("joint_not_applicable", "this pathway has one subject")
            if p.subjects.joint_session_requires_consent and any(
                "joint_session" not in s.consents for s in case.subjects
            ):
                raise CommandRejected(
                    "consent_missing", "every partner must consent before a joint session"
                )
        case.appointments.append(Appointment(appointment_at, now, actor, joint))
        seq = self._record(case, actor, "appointment_booked", at=appointment_at, joint=joint)
        self._move(case, Stage.BOOKED, seq, appointment_at=appointment_at)

    @command("record_attendance")
    def record_attendance(self, actor: Actor, case_id: str, attended: bool) -> None:
        self._no_ai(actor)
        case = self._active(case_id)
        p = self.pathway_of(case)
        self._require_stage(case, Stage.BOOKED)
        if not owner_staff(p, case.owner, actor):
            raise CommandRejected("not_permitted", "only the receiving team records attendance")
        if self.clock.now() < case.appointments[-1].at:
            raise CommandRejected("appointment_not_due", "the appointment has not happened yet")
        if attended:
            seq = self._record(case, actor, "attended")
            self._settle(case, "care_clock")
            self._move(case, Stage.ATTENDED, seq)
            return
        no_show = p.stages["attend"].no_show
        assert no_show is not None  # guaranteed by pathway validation
        case.no_shows += 1
        seq = self._record(case, actor, "no_show", no_shows=case.no_shows)
        exp = self._move(case, Stage.ACCEPTED, seq, breach_override=no_show.rebook_breach)
        if case.no_shows >= no_show.escalate_after:
            self._raise_level(case, exp, 2, "repeated no-shows", Actor.system("rules"))

    @command("record_outcome")
    def record_outcome(self, actor: Actor, case_id: str, outcome_type: str) -> None:
        self._no_ai(actor)
        case = self._active(case_id)
        p = self.pathway_of(case)
        self._require_stage(case, Stage.ATTENDED)
        if not (
            owner_staff(p, case.owner, actor)
            and actor.role in {p.roles.receiver_clinician, SUPERVISOR}
        ):
            raise CommandRejected("not_permitted", "the receiving clinician records the outcome")
        if outcome_type not in p.outcomes:
            raise CommandRejected("unknown_outcome", f"{outcome_type!r} is not a pathway outcome")
        case.outcome = Outcome(outcome_type, self.clock.now(), actor)
        seq = self._record(case, actor, "outcome_recorded", outcome=outcome_type)
        self._move(case, Stage.OUTCOME_RECORDED, seq)

    # ------------------------------------------------------- pauses and owners

    @command("pause")
    def pause(
        self, actor: Actor, case_id: str, reason: str, wake_at: datetime, note: str = ""
    ) -> None:
        """Stop the clocks for a valid reason, with an approver and an automatic wake-up."""
        self._no_ai(actor)
        case = self._open_case(case_id)
        if case.is_paused:
            raise CommandRejected("already_paused", "the case is already paused")
        p = self.pathway_of(case)
        rule = p.pauses.get(reason)
        if rule is None:
            raise CommandRejected("unknown_reason", f"{reason!r} is not a pause reason")
        now = self.clock.now()
        if wake_at <= now:
            raise CommandRejected("invalid_wake", "the wake-up time must be in the future")
        if wake_at > rule.max.add_to(now, self.calendar):
            raise CommandRejected("pause_too_long", f"{reason} allows at most {rule.max}")
        if rule.note_required and not note.strip():
            raise CommandRejected("notes_required", "this pause reason needs a note")
        clinician_needed = rule.approver == "clinician" or case.urgency == "urgent"
        needed = "clinician" if clinician_needed else "coordinator"
        if not token_satisfied(p, case.owner, actor, needed):
            raise CommandRejected("approver_not_permitted", f"this pause needs a {needed}")
        for exp in case.armed():
            exp.status = SUSPENDED
            exp.suspended_at = now
        case.pause = Pause(
            reason=reason,
            approved_by=actor,
            owner=case.owner,
            started_at=now,
            wake_at=wake_at,
            urgent_case=case.urgency == "urgent",
            note=note,
        )
        self._record(case, actor, "paused", reason=reason, wake_at=wake_at)
        self._arm(case, "pause_wake", wake_at)

    @command("resume")
    def resume(self, actor: Actor, case_id: str) -> None:
        self._no_ai(actor)
        case = self._open_case(case_id)
        if case.pause is None:
            raise CommandRejected("not_paused", "the case is not paused")
        if actor.kind == ActorKind.SYSTEM:
            if self.clock.now() < case.pause.wake_at:
                raise CommandRejected("not_due", "the clock resumes a case only at wake-up time")
        elif not owner_staff(self.pathway_of(case), case.owner, actor):
            raise CommandRejected("not_permitted", "only the owning team resumes a case")
        self._resume(case, actor)

    @command("reassign")
    def reassign(self, actor: Actor, case_id: str, to_role: str, reason: str) -> None:
        """Move the case to another queue inside the same organisation."""
        self._no_ai(actor)
        case = self._open_case(case_id)
        p = self.pathway_of(case)
        if not (
            actor.kind == ActorKind.HUMAN
            and actor.org == case.owner.org
            and actor.role == SUPERVISOR
        ):
            raise CommandRejected("not_permitted", "only a supervisor in the owning organisation")
        if to_role not in side_roles(p, case.owner.side):
            raise CommandRejected("invalid_role", f"{to_role!r} is not a queue on this side")
        if to_role == case.owner.role:
            raise CommandRejected("no_change", "the case is already in that queue")
        if not reason.strip():
            raise CommandRejected("reason_required", "a reassignment needs a reason")
        before = case.owner
        case.owner = Owner(before.org, to_role, before.side)
        seq = self._record(
            case, actor, "reassigned", from_owner=before, to_owner=case.owner, reason=reason
        )
        case.owner_history.append(
            OwnerChange(self.clock.now(), before, case.owner, "reassigned", actor, seq)
        )

    # ------------------------------------------------------------------ closure

    @command("propose_closure")
    def propose_closure(
        self,
        actor: Actor,
        case_id: str,
        closure_type: str,
        evidence: Mapping[str, str],
        transfer_to: str | None = None,
    ) -> None:
        self._no_ai(actor)
        case = self._open_case(case_id)
        if case.pending_closure is not None:
            raise CommandRejected("closure_pending", "a closure is already awaiting sign-off")
        p = self.pathway_of(case)
        rule = p.closures.get(closure_type)
        if rule is None:
            raise CommandRejected("unknown_closure", f"{closure_type!r} is not a closure type")
        if not owner_staff(p, case.owner, actor):
            raise CommandRejected("not_permitted", "only the owning team proposes closure")
        if rule.requires_stage is not None and case.stage.value != rule.requires_stage:
            raise CommandRejected("wrong_stage", f"{closure_type} needs {rule.requires_stage}")
        missing = [kind for kind in rule.evidence if not str(evidence.get(kind, "")).strip()]
        if missing:
            raise CommandRejected("evidence_missing", f"missing evidence: {', '.join(missing)}")
        weak = ladder_problems({k: evidence[k] for k in rule.evidence}, rule.counts_as)
        if weak:
            raise CommandRejected("evidence_too_weak", "; ".join(weak))
        if rule.min_contact_attempts and len(case.contact_attempts) < rule.min_contact_attempts:
            raise CommandRejected(
                "contact_protocol_incomplete",
                f"{len(case.contact_attempts)} of {rule.min_contact_attempts} contact attempts",
            )
        if "receiving_org_acceptance" in rule.evidence and (
            not transfer_to or transfer_to == case.owner.org
        ):
            raise CommandRejected("invalid_target", "name the organisation taking over care")
        if rule.creates_watch:
            due = self._parse_due_date(evidence.get("next_due_date", ""))
            if due <= self.clock.now():
                raise CommandRejected("invalid_due_date", "the next check must be in the future")
        now = self.clock.now()
        case.pending_closure = ClosureProposal(
            type=closure_type,
            evidence=dict(evidence),
            proposed_by=actor,
            proposed_at=now,
            transfer_to=transfer_to,
        )
        self._record(
            case,
            actor,
            "closure_proposed",
            type=closure_type,
            evidence=sorted(evidence),
            tiers=evidence_tiers({k: evidence[k] for k in rule.evidence}),
            transfer_to=transfer_to,
        )
        self._arm(case, "closure_signoff", p.closure_signoff.breach.add_to(now, self.calendar))

    @command("sign_closure")
    def sign_closure(self, actor: Actor, case_id: str) -> None:
        """Sign a pending closure. Two-signature closures need two different people."""
        self._no_ai(actor)
        case = self._open_case(case_id)
        proposal = case.pending_closure
        if proposal is None:
            raise CommandRejected("no_pending_closure", "nothing is awaiting sign-off")
        p = self.pathway_of(case)
        rule = p.closures[proposal.type]
        if any(s.actor.id == actor.id for s in proposal.signatures):
            raise CommandRejected("already_signed", "the next signature must be someone else's")
        filled = {s.slot for s in proposal.signatures}
        open_slots = [
            i
            for i, token in enumerate(rule.signers)
            if i not in filled and token_satisfied(p, case.owner, actor, token)
        ]
        if not open_slots:
            raise CommandRejected("signer_not_permitted", f"{proposal.type} needs {rule.signers}")
        slot = min(open_slots, key=lambda i: SLOT_SPECIFICITY[rule.signers[i]])
        proposal.signatures.append(Signature(actor, slot, self.clock.now()))
        self._record(
            case, actor, "closure_signed", type=proposal.type, slot=slot, token=rule.signers[slot]
        )
        if len(proposal.signatures) == len(rule.signers):
            self._close(case, proposal, rule)

    @command("withdraw_closure")
    def withdraw_closure(self, actor: Actor, case_id: str, reason: str) -> None:
        self._no_ai(actor)
        case = self._open_case(case_id)
        if case.pending_closure is None:
            raise CommandRejected("no_pending_closure", "nothing is awaiting sign-off")
        if not owner_staff(self.pathway_of(case), case.owner, actor):
            raise CommandRejected("not_permitted", "only the owning team withdraws a closure")
        if not reason.strip():
            raise CommandRejected("reason_required", "say why the closure is withdrawn")
        withdrawn = case.pending_closure.type
        case.pending_closure = None
        self._record(case, actor, "closure_withdrawn", type=withdrawn, reason=reason)
        self._settle(case, "closure_signoff", status=ExpectationStatus.CANCELLED)

    # ----------------------------------------------------------------------- AI

    @command("suggest_barriers")
    def suggest_barriers(self, actor: Actor, case_id: str, codes: Sequence[str], model: str) -> str:
        """The AI files a suggestion. It changes nothing until a person accepts it."""
        if actor.kind != ActorKind.AI:
            raise CommandRejected("not_permitted", "people record barriers by reviewing")
        case = self._open_case(case_id)
        self._check_barriers(case, codes)
        suggestion = AISuggestion(
            id=self._new_id("AI"),
            case_id=case.id,
            codes=list(codes),
            model=model,
            created_at=self.clock.now(),
        )
        self.suggestions[suggestion.id] = suggestion
        self._record(
            case,
            actor,
            "ai_suggestion_created",
            suggestion=suggestion.id,
            codes=list(codes),
            model=model,
        )
        return suggestion.id

    @command("review_suggestion")
    def review_suggestion(
        self,
        actor: Actor,
        suggestion_id: str,
        accept: bool,
        codes: Sequence[str] | None = None,
    ) -> None:
        self._no_ai(actor)
        suggestion = self.suggestions.get(suggestion_id)
        if suggestion is None:
            raise CommandRejected("unknown_suggestion", f"no suggestion {suggestion_id}")
        if suggestion.status != "pending":
            raise CommandRejected("already_reviewed", "this suggestion has been reviewed")
        case = self._open_case(suggestion.case_id)
        if not owner_staff(self.pathway_of(case), case.owner, actor):
            raise CommandRejected("not_permitted", "only the owning team reviews suggestions")
        final = list(codes) if codes is not None else list(suggestion.codes)
        if accept:
            self._check_barriers(case, final)
        suggestion.status = "accepted" if accept else "rejected"
        suggestion.reviewed_by = actor
        suggestion.final_codes = final if accept else []
        if accept:
            case.barriers.extend(code for code in final if code not in case.barriers)
        self._record(
            case,
            actor,
            "ai_suggestion_reviewed",
            suggestion=suggestion.id,
            accepted=accept,
            final_codes=suggestion.final_codes,
        )

    # --------------------------------------------------------------------- clock

    @command("tick")
    def tick(self, actor: Actor) -> int:
        """Act on every clock that has passed: wake paused cases, send due reminders, warn,
        escalate, and open new cases for missed surveillance checks. Returns actions taken."""
        if actor.kind != ActorKind.SYSTEM:
            raise CommandRejected("not_permitted", "only the system clock ticks")
        now = self.clock.now()
        actions = 0
        for case in list(self.cases.values()):
            if not case.is_open:
                continue
            if case.pause is not None and now >= case.pause.wake_at:
                self._resume(case, actor)
                actions += 1
            actions += self._fire_retries(case, now, actor)
            for exp in list(case.expectations):
                if exp.status != ARMED:
                    continue
                if exp.warn_at is not None and not exp.warned and now >= exp.warn_at:
                    exp.warned = True
                    self._record(case, actor, "warning", expectation=exp.kind, ref=exp.ref)
                    actions += 1
                if now >= exp.breach_at:
                    self._escalate_overdue(case, exp, now, actor)
                    actions += 1
                if exp.hard_limit_at is not None and now >= exp.hard_limit_at and exp.level < 3:
                    self._raise_level(case, exp, 3, "hard limit reached", actor)
                    actions += 1
        return actions + self._process_watches(now, actor)

    # ================================================================ internals

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids.setdefault(prefix, itertools.count(1))):06d}"

    def _record(self, case: Case | None, actor: Actor, action: str, **details: Any) -> int:
        event = self.audit.append(
            self.clock.now(), case.id if case else None, actor, action, details
        )
        return event.seq

    @staticmethod
    def _no_ai(actor: Actor) -> None:
        if actor.kind == ActorKind.AI:
            raise CommandRejected("ai_not_permitted", "AI may only make suggestions")

    def _case(self, case_id: str) -> Case:
        case = self.cases.get(case_id)
        if case is None:
            raise CommandRejected("unknown_case", f"no case {case_id}")
        return case

    def _open_case(self, case_id: str) -> Case:
        case = self._case(case_id)
        if not case.is_open:
            raise CommandRejected("case_closed", "closed cases never change; open a linked case")
        return case

    def _active(self, case_id: str) -> Case:
        """An open case that can move forward: not paused, no closure awaiting sign-off."""
        case = self._open_case(case_id)
        if case.is_paused:
            raise CommandRejected("case_paused", "resume the case first")
        if case.pending_closure is not None:
            raise CommandRejected("closure_pending", "sign or withdraw the pending closure first")
        return case

    @staticmethod
    def _require_stage(case: Case, *stages: Stage) -> None:
        if case.stage not in stages:
            wanted = " or ".join(s.value for s in stages)
            raise CommandRejected("wrong_stage", f"case is {case.stage.value}; needs {wanted}")

    @staticmethod
    def _subject(case: Case, subject_id: str) -> Subject:
        subject = case.subject(subject_id)
        if subject is None:
            raise CommandRejected("unknown_subject", f"no subject {subject_id} on this case")
        return subject

    @staticmethod
    def _open_referral(case: Case) -> Referral:
        referral = case.open_referral()
        if referral is None:
            raise CommandRejected("no_open_referral", "there is no referral waiting")
        return referral

    @staticmethod
    def _require_receiver(p: PathwayConfig, referral: Referral, actor: Actor) -> None:
        if not (
            actor.kind == ActorKind.HUMAN
            and actor.org == referral.to_org
            and actor.role in {p.roles.receiver, p.roles.receiver_clinician, SUPERVISOR}
        ):
            raise CommandRejected("not_permitted", "only the receiving organisation decides")

    def _check_barriers(self, case: Case, codes: Sequence[str]) -> None:
        allowed = set(self.pathway_of(case).barriers)
        if not codes or any(code not in allowed for code in codes):
            raise CommandRejected(
                "invalid_codes", f"barrier codes must come from {sorted(allowed)}"
            )

    def _parse_due_date(self, text: str) -> datetime:
        try:
            day = date.fromisoformat(text.strip())
        except ValueError as exc:
            raise CommandRejected("invalid_due_date", "next_due_date must be YYYY-MM-DD") from exc
        return datetime.combine(day, time(9, 0), tzinfo=self.calendar.tz)

    # --- clocks

    def _arm(
        self,
        case: Case,
        kind: str,
        breach_at: datetime,
        warn_at: datetime | None = None,
        hard_limit_at: datetime | None = None,
        ref: str | None = None,
        retry_base: datetime | None = None,
    ) -> Expectation:
        exp = Expectation(
            id=self._new_id("X"),
            kind=kind,
            created_at=self.clock.now(),
            breach_at=breach_at,
            warn_at=warn_at,
            hard_limit_at=hard_limit_at,
            ref=ref,
            retry_base=retry_base,
        )
        case.expectations.append(exp)
        return exp

    def _has_armed(self, case: Case, kind: str, ref: str | None = None) -> bool:
        return any(
            e.kind == kind and e.status in (ARMED, SUSPENDED) and (ref is None or e.ref == ref)
            for e in case.expectations
        )

    def _settle(
        self,
        case: Case,
        kind: str,
        ref: str | None = None,
        status: ExpectationStatus = ExpectationStatus.MET,
    ) -> None:
        for exp in case.expectations:
            if (
                exp.kind == kind
                and exp.status in (ARMED, SUSPENDED)
                and (ref is None or exp.ref == ref)
            ):
                exp.status = status
                exp.closed_at = self.clock.now()

    def _arm_stage_clock(
        self,
        case: Case,
        key: str,
        appointment_at: datetime | None = None,
        breach_override: Duration | None = None,
    ) -> Expectation:
        p = self.pathway_of(case)
        stage_clock = p.stages[key]  # type: ignore[index]
        now = self.clock.now()
        if breach_override is not None:
            start = now
        elif stage_clock.from_ == "result_issued":
            start = case.issued_at
        elif stage_clock.from_ == "appointment":
            if appointment_at is None:
                raise ValueError("the attend clock needs an appointment time")
            start = appointment_at
        else:
            start = now
        breach = (breach_override or stage_clock.breach).add_to(start, self.calendar)
        warn = (
            stage_clock.warn.add_to(start, self.calendar)
            if stage_clock.warn is not None and breach_override is None
            else None
        )
        return self._arm(
            case,
            f"stage:{key}",
            breach,
            warn_at=warn,
            retry_base=now if stage_clock.retries else None,
        )

    def _move(
        self,
        case: Case,
        stage: Stage,
        seq: int,
        appointment_at: datetime | None = None,
        breach_override: Duration | None = None,
    ) -> Expectation:
        """Leave the current stage (its clock is met) and start the next stage's clock."""
        leaving = CLOCK_FOR_STAGE[case.stage]
        self._settle(case, f"stage:{leaving}")
        case.stage = stage
        case.stage_history.append((stage, seq))
        return self._arm_stage_clock(
            case,
            CLOCK_FOR_STAGE[stage],
            appointment_at=appointment_at,
            breach_override=breach_override,
        )

    def _advance_patient_track(self, case: Case, seq: int) -> None:
        if case.stage == Stage.ISSUED and all(s.notified_at for s in case.subjects):
            self._move(case, Stage.NOTIFIED, seq)
        if case.stage == Stage.NOTIFIED and all(
            s.acknowledged_at and s.understood for s in case.subjects
        ):
            self._move(case, Stage.ACKNOWLEDGED, seq)

    def _target_for(self, case: Case, level: int) -> str:
        if level == 1:
            return f"{case.owner.org}:{case.owner.role} queue"
        if level == 2:
            return f"{case.owner.org}:{SUPERVISOR}"
        return "programme_manager"

    def _raise_level(
        self, case: Case, exp: Expectation, level: int, reason: str, actor: Actor
    ) -> None:
        if level <= exp.level:
            return
        before = exp.level
        exp.level = level
        case.escalations.append(
            Escalation(level, exp.kind, reason, self.clock.now(), self._target_for(case, level))
        )
        self._record(
            case,
            actor,
            "escalated",
            expectation=exp.kind,
            ref=exp.ref,
            from_level=before,
            to_level=level,
            reason=reason,
        )

    def _escalate_overdue(self, case: Case, exp: Expectation, now: datetime, actor: Actor) -> None:
        """Climb the ladder once per missed rung; long gaps are summarised in one event."""
        ladder = self.pathway_of(case).escalation
        before = exp.level
        repeats = 0
        while now >= exp.breach_at:
            if exp.level == 0:
                exp.level = 2 if case.urgency == "urgent" else 1
            elif exp.level < 3:
                exp.level += 1
            else:
                repeats += 1
            step = {1: ladder.l1_to_l2, 2: ladder.l2_to_l3, 3: ladder.repeat_l3}[exp.level]
            exp.breach_at = step.add_to(exp.breach_at, self.calendar)
        if exp.level != before:
            case.escalations.append(
                Escalation(
                    exp.level,
                    exp.kind,
                    "no response by the deadline",
                    now,
                    self._target_for(case, exp.level),
                )
            )
        self._record(
            case,
            actor,
            "escalated",
            expectation=exp.kind,
            ref=exp.ref,
            from_level=before,
            to_level=exp.level,
            repeats=repeats,
            next_check=exp.breach_at,
        )

    def _fire_retries(self, case: Case, now: datetime, actor: Actor) -> int:
        p = self.pathway_of(case)
        fired = 0
        for exp in list(case.expectations):
            if exp.status != ARMED or exp.retry_base is None or not exp.kind.startswith("stage:"):
                continue
            key = exp.kind.removeprefix("stage:")
            retries = p.stages[key].retries  # type: ignore[index]
            while exp.retries_fired < len(retries):
                retry = retries[exp.retries_fired]
                if now < retry.after.add_to(exp.retry_base, self.calendar):
                    break
                exp.retries_fired += 1
                fired += 1
                waiting = [
                    s
                    for s in case.subjects
                    if (key == "notify" and s.notified_at is None)
                    or (key == "acknowledge" and not (s.acknowledged_at and s.understood))
                ]
                for subject in waiting:
                    if retry.channel is not None:
                        self._send_reminder(case, subject, retry.channel, actor)
                    elif not self._has_armed(case, "call_task", subject.id):
                        self._arm(
                            case,
                            "call_task",
                            p.call_task.breach.add_to(now, self.calendar),
                            ref=subject.id,
                        )
                self._record(
                    case,
                    actor,
                    "retry_fired",
                    stage=key,
                    retry=exp.retries_fired,
                    channel=retry.channel,
                    task=retry.task,
                    subjects=[s.id for s in waiting],
                )
        return fired

    def _send_reminder(self, case: Case, subject: Subject, channel: str, actor: Actor) -> None:
        ref = self.templates.latest_approved(
            case.pathway_id, "reminder", subject.language, at=self.clock.now()
        )
        if ref is None:
            # Never send unreviewed words: a person follows up instead (invariant I10).
            self._record(case, actor, "message_blocked", subject=subject.id, message="reminder")
            if not self._has_armed(case, "call_task", subject.id):
                breach = self.pathway_of(case).call_task.breach.add_to(
                    self.clock.now(), self.calendar
                )
                self._arm(case, "call_task", breach, ref=subject.id)
            return
        self._store_message(case, subject, ref, channel)
        self._record(
            case,
            actor,
            "message_sent",
            subject=subject.id,
            message="reminder",
            version=ref.version,
            channel=channel,
        )

    def _store_message(self, case: Case, subject: Subject, ref: TemplateRef, channel: str) -> None:
        now = self.clock.now()
        case.messages.append(
            Message(
                id=self._new_id("M"),
                subject_id=subject.id,
                message_id=ref.message_id,
                language=ref.language,
                version=ref.version,
                channel=channel,
                sent_at=now,
                approved_at_send=self.templates.is_approved(ref, at=now),
            )
        )
        case.contact_attempts.append(ContactAttempt(now, "system", channel, None))

    def _resume(self, case: Case, actor: Actor) -> None:
        pause = case.pause
        assert pause is not None
        now = self.clock.now()
        paused_for = now - pause.started_at
        for exp in case.expectations:
            if exp.status == SUSPENDED:
                exp.breach_at += paused_for
                if exp.warn_at is not None:
                    exp.warn_at += paused_for
                if exp.hard_limit_at is not None:
                    exp.hard_limit_at += paused_for
                if exp.retry_base is not None:
                    exp.retry_base += paused_for
                exp.status = ARMED
                exp.suspended_at = None
        automatic = actor.kind == ActorKind.SYSTEM
        self._settle(
            case,
            "pause_wake",
            status=ExpectationStatus.MET if automatic else ExpectationStatus.CANCELLED,
        )
        pause.ended_at = now
        pause.ended_by = actor.id
        case.pause_history.append(pause)
        case.pause = None
        case.total_paused += paused_for
        self._record(
            case,
            actor,
            "resumed",
            reason=pause.reason,
            automatic=automatic,
            paused_hours=round(paused_for.total_seconds() / 3600, 2),
        )

    def _close(self, case: Case, proposal: ClosureProposal, rule: ClosureRule) -> None:
        now = self.clock.now()
        if case.pause is not None:
            case.pause.ended_at = now
            case.pause.ended_by = "closure"
            case.total_paused += now - case.pause.started_at
            case.pause_history.append(case.pause)
            case.pause = None
        for exp in case.expectations:
            if exp.status in (ARMED, SUSPENDED):
                exp.status = (
                    ExpectationStatus.MET
                    if exp.kind == "closure_signoff"
                    else ExpectationStatus.CANCELLED
                )
                exp.closed_at = now
        case.closure = Closure(
            type=proposal.type,
            counts_as=rule.counts_as,
            evidence=dict(proposal.evidence),
            signatures=list(proposal.signatures),
            proposed_by=proposal.proposed_by,
            closed_at=now,
            stage_at_close=case.stage,
            transfer_to=proposal.transfer_to,
        )
        case.pending_closure = None
        seq = self._record(
            case,
            Actor.system("rules"),
            "case_closed",
            type=proposal.type,
            counts_as=rule.counts_as,
            stage_at_close=case.stage,
        )
        case.stage = Stage.CLOSED
        case.stage_history.append((Stage.CLOSED, seq))
        if rule.creates_watch:
            self._create_watch(case, proposal)
        case.closed_fingerprint = case_fingerprint(case)

    def _create_watch(self, case: Case, proposal: ClosureProposal) -> None:
        p = self.pathway_of(case)
        assert p.watch is not None  # guaranteed by pathway validation
        due = self._parse_due_date(proposal.evidence["next_due_date"])
        watch = Watch(
            id=self._new_id("W"),
            origin_case_id=case.id,
            pathway_id=p.id,
            sender_org=case.sender_org,
            subjects=[(s.id, s.contact_id, s.language) for s in case.subjects],
            due_at=due,
            trigger_at=p.watch.grace.add_to(due, self.calendar),
            result_code=p.watch.result_code,
        )
        self.watches.append(watch)
        self._record(
            case,
            Actor.system("rules"),
            "watch_created",
            watch=watch.id,
            due_at=watch.due_at,
            trigger_at=watch.trigger_at,
        )

    def _process_watches(self, now: datetime, actor: Actor) -> int:
        opened = 0
        for watch in self.watches:
            if watch.status != "watching" or now < watch.trigger_at:
                continue
            watch.status = "triggered"
            p = self.pathways[watch.pathway_id]
            subjects = [NewSubject(*s) for s in watch.subjects]
            watch.new_case_id = self._open(
                p,
                f"WATCH-{watch.id}",
                watch.result_code,
                subjects,
                watch.sender_org,
                now,
                actor,
                watch.origin_case_id,
            )
            opened += 1
        return opened

    def _satisfy_watches(self, case: Case) -> None:
        subject_ids = {s.id for s in case.subjects}
        for watch in self.watches:
            if (
                watch.status == "watching"
                and watch.pathway_id == case.pathway_id
                and subject_ids & {s[0] for s in watch.subjects}
            ):
                watch.status = "satisfied"
                watch.new_case_id = case.id
                self._record(case, Actor.system("rules"), "watch_satisfied", watch=watch.id)

    def _open(
        self,
        p: PathwayConfig,
        result_id: str,
        result_code: str,
        subjects: Sequence[NewSubject],
        sender_org: str,
        issued: datetime,
        actor: Actor,
        linked_from: str | None,
    ) -> str:
        now = self.clock.now()
        case = Case(
            id=self._new_id("C"),
            pathway_id=p.id,
            pathway_version=p.version,
            result_id=result_id,
            result_code=result_code,
            issued_at=issued,
            opened_at=now,
            sender_org=sender_org,
            urgency=p.trigger.urgency,
            stage=Stage.ISSUED,
            owner=Owner(sender_org, p.roles.sender, Side.SENDER),
            subjects=[Subject(s.id, s.contact_id, s.language) for s in subjects],
            linked_from=linked_from,
        )
        self.cases[case.id] = case
        self.results[result_id] = case.id
        seq = self._record(
            case,
            actor,
            "case_opened",
            result_id=result_id,
            result_code=result_code,
            pathway=p.id,
            linked_from=linked_from,
            owner=case.owner,
        )
        case.owner_history.append(OwnerChange(now, None, case.owner, "opened", actor, seq))
        case.stage_history.append((Stage.ISSUED, seq))
        self._arm_stage_clock(case, "notify")
        self._arm(
            case,
            "care_clock",
            p.care_clock.target.add_to(issued, self.calendar),
            hard_limit_at=p.care_clock.hard_limit.add_to(issued, self.calendar),
        )
        self._satisfy_watches(case)
        return case.id
