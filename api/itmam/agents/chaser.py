"""Chaser agent: when a case is late or stuck, what should happen next, and who does it.

A fixed decision table, checked in priority order. Safety comes first: an urgent case that has
not reached its colonoscopy always goes to a clinician, whatever else is going on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from itmam.agents.base import Proposal
from itmam.domain import Stage
from itmam.domain.model import CLOCK_FOR_STAGE, ExpectationStatus

if TYPE_CHECKING:
    from itmam.sandbox import Sandbox

BARRIER_ACTIONS = {
    "transport": "arrange transport for the appointment",
    "fear": "book a nurse call to talk through the procedure",
    "bowel_prep": "send the bowel-preparation guide and offer a nurse call",
    "cost_or_insurance": "confirm coverage with the insurer before rebooking",
    "work_or_childcare": "offer an evening or weekend slot",
    "language": "book an interpreter",
    "family_discussion": "offer a family call with the clinician",
    "done_elsewhere": "request the report from the other provider (check Malaffi)",
    "wrong_person": "verify identity and contact details",
}


def _overdue(box: Sandbox, case_id: str) -> list[str]:
    now = box.clock.now()
    case = box.case(case_id)
    return [
        e.kind
        for e in case.expectations
        if e.status == ExpectationStatus.ARMED and e.breach_at <= now
    ]


def chase(box: Sandbox, case_id: str) -> Proposal:
    case = box.case(case_id)
    level = max((x.level for x in case.escalations), default=0)
    overdue = _overdue(box, case_id)
    stage_clock = f"stage:{CLOCK_FOR_STAGE.get(case.stage, '')}"
    if any(x.expectation_kind == stage_clock for x in case.escalations):
        overdue.append(stage_clock)  # escalated on this stage: late, even if re-armed
    details: dict[str, object] = {"escalation_level": level, "overdue": overdue}
    checks = ["urgency first", "pause state", "overdue clocks", "barriers", "no-shows"]

    def say(action: str, who: str, priority: str, summary: str) -> Proposal:
        details.update(action=action, owner=who, priority=priority)
        return Proposal("chaser", case_id, summary, details, checks=checks)

    if not case.is_open:
        return say("none", "-", "none", "Closed: nothing to chase.")
    if case.is_paused and case.pause is not None:
        return say("none", "-", "low", f"Paused ({case.pause.reason}); it wakes automatically.")
    before_care = case.stage in {
        Stage.ISSUED,
        Stage.NOTIFIED,
        Stage.ACKNOWLEDGED,
        Stage.REFERRED,
        Stage.ACCEPTED,
        Stage.BOOKED,
    }
    if case.urgency == "urgent" and before_care:
        return say(
            "clinician_review",
            "clinician",
            "urgent",
            "Urgent case not yet seen: a clinician reviews it today and expedites the slot.",
        )
    if level >= 3 or "care_clock" in overdue:
        return say(
            "programme_review",
            "programme manager",
            "high",
            "Past the overall care deadline: the programme manager reviews this case.",
        )
    if case.stage == Stage.ISSUED:
        return say(
            "check_channel",
            "coordinator",
            "high",
            "The result notice has not been delivered: check the number and try another channel.",
        )
    if case.stage == Stage.NOTIFIED:
        failed = any(s.acknowledged_at and not s.understood for s in case.subjects)
        if failed or "call_task" in overdue or level >= 1:
            return say(
                "phone_call",
                "coordinator",
                "high",
                "No response to messages: phone the patient and explain the result.",
            )
        return say(
            "wait",
            "system",
            "low",
            "Notice delivered; the reminder schedule is running. No action yet.",
        )
    if case.stage == Stage.ACKNOWLEDGED:
        return say(
            "send_referral",
            "coordinator",
            "high" if overdue else "normal",
            "The patient understands the result: send the referral (ask the Router where).",
        )
    if case.stage == Stage.REFERRED:
        if overdue:
            return say(
                "chase_receiver",
                "coordinator",
                "high",
                "The receiving unit has not answered: phone them, or refer elsewhere.",
            )
        return say("wait", "receiving unit", "low", "Waiting for the receiving unit to accept.")
    if case.stage == Stage.ACCEPTED:
        barrier_steps = [BARRIER_ACTIONS[b] for b in case.barriers if b in BARRIER_ACTIONS]
        if case.no_shows:
            fix = "; ".join(barrier_steps) or "ask why and remove the barrier"
            return say(
                "rebook_with_support",
                "receiving coordinator",
                "high",
                f"Missed appointment ({case.no_shows}): {fix}, then rebook.",
            )
        visit = "colonoscopy" if case.pathway_id.startswith("crc") else "appointment"
        return say("book", "receiving coordinator", "normal", f"Book the {visit}.")
    if case.stage == Stage.BOOKED:
        return say(
            "reminder",
            "system",
            "low",
            "Booked: send the bowel-preparation reminder two days before."
            if case.pathway_id.startswith("crc")
            else "Booked: send the reminder two days before.",
        )
    if case.stage == Stage.ATTENDED:
        return say(
            "chase_outcome",
            "receiving clinician",
            "high" if overdue else "normal",
            "Visit done: the receiving clinician records the outcome.",
        )
    return say(
        "propose_closure",
        "receiving coordinator",
        "normal",
        "Outcome recorded: propose closure with evidence (ask the Verifier first).",
    )
