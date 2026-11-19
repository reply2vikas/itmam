"""Case state. Plain dataclasses: the engine is the only thing that changes them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum


class Stage(StrEnum):
    ISSUED = "issued"
    NOTIFIED = "notified"
    ACKNOWLEDGED = "acknowledged"
    REFERRED = "referred"
    ACCEPTED = "accepted"
    BOOKED = "booked"
    ATTENDED = "attended"
    OUTCOME_RECORDED = "outcome_recorded"
    CLOSED = "closed"


# The stage clock (from pathway config) that runs while a case sits in each open stage.
CLOCK_FOR_STAGE: dict[Stage, str] = {
    Stage.ISSUED: "notify",
    Stage.NOTIFIED: "acknowledge",
    Stage.ACKNOWLEDGED: "refer",
    Stage.REFERRED: "accept",
    Stage.ACCEPTED: "book",
    Stage.BOOKED: "attend",
    Stage.ATTENDED: "outcome",
    Stage.OUTCOME_RECORDED: "close",
}

# Stages in which the receiving organisation owns the case.
RECEIVER_STAGES = frozenset({Stage.ACCEPTED, Stage.BOOKED, Stage.ATTENDED, Stage.OUTCOME_RECORDED})


class Side(StrEnum):
    SENDER = "sender"
    RECEIVER = "receiver"


class ActorKind(StrEnum):
    HUMAN = "human"  # staff: coordinators, clinicians, supervisors
    PATIENT = "patient"  # a patient or guardian, via their journey link
    SYSTEM = "system"  # feeds, channel adapters, the clock
    AI = "ai"  # may only make suggestions


@dataclass(frozen=True)
class Actor:
    kind: ActorKind
    id: str
    role: str = ""
    org: str = ""

    @staticmethod
    def system(name: str = "system") -> Actor:
        return Actor(ActorKind.SYSTEM, name)

    @staticmethod
    def ai(name: str = "ai:barrier-tagger") -> Actor:
        return Actor(ActorKind.AI, name, role="ai")

    @staticmethod
    def staff(org: str, role: str, name: str) -> Actor:
        return Actor(ActorKind.HUMAN, name, role=role, org=org)

    @staticmethod
    def contact(contact_id: str) -> Actor:
        return Actor(ActorKind.PATIENT, contact_id, role="contact")


@dataclass(frozen=True)
class Owner:
    org: str
    role: str
    side: Side


@dataclass
class Subject:
    id: str
    contact_id: str  # the patient themselves, or the guardian for a child
    language: str
    notified_at: datetime | None = None
    acknowledged_at: datetime | None = None
    understood: bool = False
    consents: set[str] = field(default_factory=set)


class ExpectationStatus(StrEnum):
    ARMED = "armed"
    MET = "met"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"  # clock stopped while the case is paused


@dataclass
class Expectation:
    """Something that should happen by a deadline. Silence past `breach_at` escalates."""

    id: str
    kind: str  # "stage:notify", "care_clock", "callback", "econsult_reply", "call_task", ...
    created_at: datetime
    breach_at: datetime
    warn_at: datetime | None = None
    hard_limit_at: datetime | None = None
    ref: str | None = None
    status: ExpectationStatus = ExpectationStatus.ARMED
    warned: bool = False
    level: int = 0
    retry_base: datetime | None = None
    retries_fired: int = 0
    suspended_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass
class Escalation:
    level: int  # 1 coordinator queue, 2 supervising clinician, 3 programme manager
    expectation_kind: str
    reason: str
    at: datetime
    target: str


@dataclass
class Pause:
    reason: str
    approved_by: Actor
    owner: Owner  # who owned the case when it was paused
    started_at: datetime
    wake_at: datetime
    urgent_case: bool
    note: str = ""
    ended_at: datetime | None = None
    ended_by: str | None = None


@dataclass
class Referral:
    id: str
    to_org: str
    sent_at: datetime
    sent_by: Actor
    note: str
    note_approved_by: Actor
    status: str = "sent"  # sent | accepted | rejected
    decided_at: datetime | None = None
    decided_by: Actor | None = None
    reason: str = ""


@dataclass
class Consultation:
    id: str
    to_org: str
    question: str
    asked_by: Actor
    asked_at: datetime
    answer: str = ""
    answered_by: Actor | None = None
    answered_at: datetime | None = None


@dataclass
class Callback:
    id: str
    requested_by: str
    requested_at: datetime
    note: str = ""
    completed_at: datetime | None = None
    completed_by: Actor | None = None


@dataclass
class ContactAttempt:
    at: datetime
    by: str
    channel: str
    reached: bool | None


@dataclass
class Message:
    id: str
    subject_id: str
    message_id: str
    language: str
    version: int
    channel: str
    sent_at: datetime
    approved_at_send: bool


@dataclass
class Appointment:
    at: datetime
    booked_at: datetime
    booked_by: Actor
    joint: bool = False


@dataclass
class Outcome:
    type: str
    recorded_at: datetime
    recorded_by: Actor


@dataclass
class Signature:
    actor: Actor
    slot: int
    at: datetime


@dataclass
class ClosureProposal:
    type: str
    evidence: dict[str, str]
    proposed_by: Actor
    proposed_at: datetime
    transfer_to: str | None = None
    signatures: list[Signature] = field(default_factory=list)


@dataclass
class Closure:
    type: str
    counts_as: str
    evidence: dict[str, str]
    signatures: list[Signature]
    proposed_by: Actor
    closed_at: datetime
    stage_at_close: Stage
    transfer_to: str | None = None


@dataclass
class OwnerChange:
    at: datetime
    from_owner: Owner | None
    to_owner: Owner
    reason: str  # opened | accepted | reassigned
    by: Actor
    audit_seq: int


@dataclass
class Case:
    id: str
    pathway_id: str
    pathway_version: int
    result_id: str
    result_code: str
    issued_at: datetime
    opened_at: datetime
    sender_org: str
    urgency: str
    stage: Stage
    owner: Owner
    subjects: list[Subject]
    linked_from: str | None = None
    expectations: list[Expectation] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    pause: Pause | None = None
    pause_history: list[Pause] = field(default_factory=list)
    total_paused: timedelta = timedelta(0)
    referrals: list[Referral] = field(default_factory=list)
    rejections: int = 0
    consultations: list[Consultation] = field(default_factory=list)
    callbacks: list[Callback] = field(default_factory=list)
    contact_attempts: list[ContactAttempt] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    appointments: list[Appointment] = field(default_factory=list)
    no_shows: int = 0
    outcome: Outcome | None = None
    barriers: list[str] = field(default_factory=list)
    pending_closure: ClosureProposal | None = None
    closure: Closure | None = None
    owner_history: list[OwnerChange] = field(default_factory=list)
    stage_history: list[tuple[Stage, int]] = field(default_factory=list)
    closed_fingerprint: str | None = None

    @property
    def is_open(self) -> bool:
        return self.closure is None

    @property
    def is_paused(self) -> bool:
        return self.pause is not None

    def subject(self, subject_id: str) -> Subject | None:
        return next((s for s in self.subjects if s.id == subject_id), None)

    def armed(self) -> list[Expectation]:
        return [e for e in self.expectations if e.status == ExpectationStatus.ARMED]

    def open_referral(self) -> Referral | None:
        return next((r for r in reversed(self.referrals) if r.status == "sent"), None)


@dataclass
class Watch:
    """After a surveillance closure: open a new linked case if the next check is missed."""

    id: str
    origin_case_id: str
    pathway_id: str
    sender_org: str
    subjects: list[tuple[str, str, str]]  # (subject id, contact id, language)
    due_at: datetime
    trigger_at: datetime
    result_code: str
    status: str = "watching"  # watching | satisfied | triggered
    new_case_id: str | None = None


@dataclass
class AISuggestion:
    id: str
    case_id: str
    codes: list[str]
    model: str
    created_at: datetime
    status: str = "pending"  # pending | accepted | rejected
    reviewed_by: Actor | None = None
    final_codes: list[str] = field(default_factory=list)
