"""The fifteen rules that must hold after every command. `check_all` returns every violation
it finds; the property-based test calls it after each random step."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from itmam.domain.audit import case_fingerprint
from itmam.domain.evidence import ladder_problems
from itmam.domain.model import (
    RECEIVER_STAGES,
    ActorKind,
    Case,
    ExpectationStatus,
    Owner,
    Side,
    Stage,
)
from itmam.domain.roles import SUPERVISOR, token_satisfied
from itmam.domain.templates import TemplateRef

if TYPE_CHECKING:
    from itmam.domain.engine import Engine

AI_ALLOWED_ACTIONS = frozenset({"ai_suggestion_created"})


@dataclass(frozen=True)
class Violation:
    invariant: str
    case_id: str | None
    detail: str

    def __str__(self) -> str:
        return f"{self.invariant} [{self.case_id}] {self.detail}"


def check_all(engine: Engine) -> list[Violation]:
    found: list[Violation] = []
    for check in (
        i1_one_owner,
        i2_no_silent_case,
        i3_closure_with_proof,
        i4_ownership_moves_by_rule,
        i5_bounded_pauses,
        i6_ai_never_acts,
        i7_tamper_evident_history,
        i8_ledger_balances,
        i9_closed_is_final,
        i10_approved_messages_only,
        i11_joint_sessions_need_every_consent,
        i12_care_clock_never_restarts,
        i13_time_runs_forward,
        i14_understood_before_referral,
        i15_evidence_ladder,
    ):
        found.extend(check(engine))
    return found


def _open_cases(engine: Engine) -> list[Case]:
    return [case for case in engine.cases.values() if case.is_open]


def i1_one_owner(engine: Engine) -> list[Violation]:
    """Every open case has exactly one accountable owner, on the right side of the hand-off."""
    out: list[Violation] = []
    for case in _open_cases(engine):
        owner = case.owner
        if not owner.org or not owner.role:
            out.append(Violation("I1", case.id, "owner is incomplete"))
            continue
        if case.stage in RECEIVER_STAGES:
            accepted = [r for r in case.referrals if r.status == "accepted"]
            if owner.side != Side.RECEIVER or not accepted or owner.org != accepted[-1].to_org:
                out.append(Violation("I1", case.id, f"{case.stage.value} must be receiver-owned"))
        elif owner.side != Side.SENDER or owner.org != case.sender_org:
            out.append(Violation("I1", case.id, f"{case.stage.value} must be sender-owned"))
    return out


def i2_no_silent_case(engine: Engine) -> list[Violation]:
    """Every open, unpaused case is waiting on at least one armed clock."""
    return [
        Violation("I2", case.id, f"no armed expectation in {case.stage.value}")
        for case in _open_cases(engine)
        if not case.is_paused and not case.armed()
    ]


def i3_closure_with_proof(engine: Engine) -> list[Violation]:
    """Every closed case carries its closure type's evidence and its required signers."""
    out: list[Violation] = []
    for case in engine.cases.values():
        closure = case.closure
        if closure is None:
            continue
        p = engine.pathway_of(case)
        rule = p.closures.get(closure.type)
        if rule is None:
            out.append(Violation("I3", case.id, f"unknown closure type {closure.type}"))
            continue
        missing = [k for k in rule.evidence if not str(closure.evidence.get(k, "")).strip()]
        if missing:
            out.append(Violation("I3", case.id, f"missing evidence {missing}"))
        if len(closure.signatures) != len(rule.signers):
            out.append(Violation("I3", case.id, "wrong number of signatures"))
        if len({s.actor.id for s in closure.signatures}) != len(closure.signatures):
            out.append(Violation("I3", case.id, "one person signed twice"))
        for signature in closure.signatures:
            token = rule.signers[signature.slot]
            if not token_satisfied(p, case.owner, signature.actor, token):
                out.append(Violation("I3", case.id, f"{signature.actor.id} cannot sign as {token}"))
        if rule.requires_stage and closure.stage_at_close.value != rule.requires_stage:
            out.append(Violation("I3", case.id, f"closed before {rule.requires_stage}"))
        if rule.min_contact_attempts and len(case.contact_attempts) < rule.min_contact_attempts:
            out.append(Violation("I3", case.id, "contact protocol incomplete"))
    return out


def i4_ownership_moves_by_rule(engine: Engine) -> list[Violation]:
    """Ownership changes only by acceptance, same-organisation reassignment, or opening."""
    out: list[Violation] = []
    for case in engine.cases.values():
        p = engine.pathway_of(case)
        history = case.owner_history
        if not history or history[0].reason != "opened" or history[0].from_owner is not None:
            out.append(Violation("I4", case.id, "ownership history must start when the case opens"))
            continue
        if history[0].to_owner != Owner(case.sender_org, p.roles.sender, Side.SENDER):
            out.append(Violation("I4", case.id, "case must open in the sender's queue"))
        for before, change in zip(history, history[1:], strict=False):
            if change.from_owner != before.to_owner:
                out.append(Violation("I4", case.id, "ownership history has a gap"))
            if change.by.kind == ActorKind.AI:
                out.append(Violation("I4", case.id, "AI moved ownership"))
            src, dst = change.from_owner, change.to_owner
            if src is None:
                out.append(Violation("I4", case.id, "ownership change without a previous owner"))
                continue
            if change.reason == "accepted":
                accepted_orgs = {r.to_org for r in case.referrals if r.status == "accepted"}
                if (
                    src.side != Side.SENDER
                    or dst.side != Side.RECEIVER
                    or dst.org not in accepted_orgs
                ):
                    out.append(Violation("I4", case.id, "acceptance moved ownership wrongly"))
                receiving_roles = {p.roles.receiver, p.roles.receiver_clinician, SUPERVISOR}
                if change.by.org != dst.org or change.by.role not in receiving_roles:
                    out.append(
                        Violation("I4", case.id, "someone outside the receiving team accepted")
                    )
            elif change.reason == "reassigned":
                if src.org != dst.org or src.side != dst.side:
                    out.append(Violation("I4", case.id, "reassignment left the organisation"))
            else:
                out.append(Violation("I4", case.id, f"unknown ownership change {change.reason}"))
        if history[-1].to_owner != case.owner:
            out.append(Violation("I4", case.id, "current owner is not the last recorded owner"))
    return out


def i5_bounded_pauses(engine: Engine) -> list[Violation]:
    """Every pause has a valid reason, a permitted approver and a bounded wake-up, and stops
    the case's clocks."""
    out: list[Violation] = []
    for case in engine.cases.values():
        p = engine.pathway_of(case)
        if case.pause is not None:
            started = case.pause.started_at
            running = [
                e.kind for e in case.armed() if e.created_at < started and e.kind != "pause_wake"
            ]
            if running:
                out.append(Violation("I5", case.id, f"clocks kept running while paused: {running}"))
        pauses = list(case.pause_history) + ([case.pause] if case.pause else [])
        for pause in pauses:
            rule = p.pauses.get(pause.reason)
            if rule is None:
                out.append(Violation("I5", case.id, f"unknown pause reason {pause.reason}"))
                continue
            if pause.wake_at > rule.max.add_to(pause.started_at, engine.calendar):
                out.append(Violation("I5", case.id, f"{pause.reason} pause exceeds {rule.max}"))
            needed = (
                "clinician" if rule.approver == "clinician" or pause.urgent_case else "coordinator"
            )
            if not token_satisfied(p, pause.owner, pause.approved_by, needed):
                out.append(Violation("I5", case.id, f"pause approved without a {needed}"))
    return out


def i6_ai_never_acts(engine: Engine) -> list[Violation]:
    """The AI actor appears in the audit log only as the author of suggestions."""
    out = [
        Violation("I6", event.case_id, f"AI performed {event.action}")
        for event in engine.audit.events
        if event.actor_kind == ActorKind.AI.value and event.action not in AI_ALLOWED_ACTIONS
    ]
    for case in engine.cases.values():
        signers = case.closure.signatures if case.closure else []
        if any(s.actor.kind == ActorKind.AI for s in signers):
            out.append(Violation("I6", case.id, "AI signed a closure"))
    return out


def i7_tamper_evident_history(engine: Engine) -> list[Violation]:
    """The hash chain verifies, and every stage and owner change points at its audit event."""
    out = [Violation("I7", None, problem) for problem in engine.audit.verify()]
    events = engine.audit.events
    for case in engine.cases.values():
        seqs = [seq for _, seq in case.stage_history] + [c.audit_seq for c in case.owner_history]
        for seq in seqs:
            if not 1 <= seq <= len(events) or events[seq - 1].case_id != case.id:
                out.append(Violation("I7", case.id, f"history points at a missing event {seq}"))
        if not case.stage_history or case.stage_history[-1][0] != case.stage:
            out.append(Violation("I7", case.id, "stage history does not end at the current stage"))
    return out


def i8_ledger_balances(engine: Engine) -> list[Violation]:
    """Every actionable result maps to exactly one case: received = open + closed."""
    out: list[Violation] = []
    by_result = {case.result_id: case.id for case in engine.cases.values()}
    if len(by_result) != len(engine.cases):
        out.append(Violation("I8", None, "two cases share one result"))
    if by_result != engine.results:
        out.append(Violation("I8", None, "the result ledger and the cases disagree"))
    ledger = engine.ledger()
    if ledger["received"] != ledger["open"] + ledger["closed"]:
        out.append(Violation("I8", None, f"ledger does not balance: {ledger}"))
    return out


def i9_closed_is_final(engine: Engine) -> list[Violation]:
    """A closed case never changes after closing."""
    out: list[Violation] = []
    live = (ExpectationStatus.ARMED, ExpectationStatus.SUSPENDED)
    for case in engine.cases.values():
        if case.closure is None:
            if case.stage == Stage.CLOSED or case.closed_fingerprint is not None:
                out.append(Violation("I9", case.id, "closed state without a closure"))
            continue
        if case.stage != Stage.CLOSED or case.pause is not None or case.pending_closure is not None:
            out.append(Violation("I9", case.id, "closed case is still active"))
        if any(e.status in live for e in case.expectations):
            out.append(Violation("I9", case.id, "closed case still has live clocks"))
        if case.closed_fingerprint != case_fingerprint(case):
            out.append(Violation("I9", case.id, "closed case changed after closing"))
    return out


def i10_approved_messages_only(engine: Engine) -> list[Violation]:
    """Every message a patient received used a clinically approved template version."""
    out: list[Violation] = []
    for case in engine.cases.values():
        for message in case.messages:
            ref = TemplateRef(
                case.pathway_id, message.message_id, message.language, message.version
            )
            if not message.approved_at_send or not engine.templates.is_approved(
                ref, message.sent_at
            ):
                out.append(Violation("I10", case.id, f"unapproved message {message.id}"))
    return out


# --------------------------------------------------------------- I11 to I15


_STAGE_ORDER = list(Stage)


def i11_joint_sessions_need_every_consent(engine: Engine) -> list[Violation]:
    """A joint session (for example a premarital couple's counselling) is booked only when every
    person in the case has recorded their own consent."""
    out: list[Violation] = []
    for case in engine.cases.values():
        if any(a.joint for a in case.appointments) and any(
            "joint_session" not in s.consents for s in case.subjects
        ):
            out.append(Violation("I11", case.id, "joint session booked without every consent"))
    return out


def i12_care_clock_never_restarts(engine: Engine) -> list[Violation]:
    """The result-to-care clock is set once, when the case opens. Rejections, re-referrals and
    no-shows never restart it, so a case cannot buy time by bouncing between units."""
    out: list[Violation] = []
    for case in engine.cases.values():
        clocks = [e for e in case.expectations if e.kind == "care_clock"]
        if len(clocks) > 1:
            out.append(Violation("I12", case.id, f"care clock set {len(clocks)} times"))
        if clocks and clocks[0].created_at > case.opened_at:
            out.append(Violation("I12", case.id, "care clock started after the case opened"))
    return out


def i13_time_runs_forward(engine: Engine) -> list[Violation]:
    """Nothing is recorded in the past: audit times never go backwards, so no event can be
    backdated to hide a delay."""
    out: list[Violation] = []
    previous = ""
    for event in engine.audit.events:
        if previous and event.at < previous:
            out.append(Violation("I13", event.case_id, f"event {event.seq} is backdated"))
        previous = max(previous, event.at)
    return out


def i14_understood_before_referral(engine: Engine) -> list[Violation]:
    """No case moves past acknowledgement until every person in it has confirmed they
    understood the result and the next step (Statement 05's "understood")."""
    out: list[Violation] = []
    ack = _STAGE_ORDER.index(Stage.ACKNOWLEDGED)
    for case in engine.cases.values():
        reached = [stage for stage, _ in case.stage_history if stage != Stage.CLOSED]
        moved_on = any(_STAGE_ORDER.index(stage) >= ack for stage in reached)
        if moved_on and not all(s.acknowledged_at and s.understood for s in case.subjects):
            out.append(Violation("I14", case.id, "moved on before everyone understood"))
    return out


def i15_evidence_ladder(engine: Engine) -> list[Violation]:
    """Closure evidence is never AI-generated text, and a closure that counts as success rests
    on evidence at tier T2 or stronger (see `evidence.py`)."""
    out: list[Violation] = []
    for case in engine.cases.values():
        closure = case.closure
        if closure is None:
            continue
        rule = engine.pathway_of(case).closures.get(closure.type)
        if rule is None:
            continue
        evidence = {k: closure.evidence.get(k, "") for k in rule.evidence}
        for problem in ladder_problems(evidence, closure.counts_as):
            out.append(Violation("I15", case.id, problem))
    return out
