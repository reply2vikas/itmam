"""Read-only views over the engine: where each case is stuck, which cases will breach soon,
and how long cases sat with each organisation.

Nothing here changes a case. Every result is derived from the case state and the audit log, by
rules, so the same case always gives the same answer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from itmam.domain import Engine, Stage
from itmam.domain.model import Case, ExpectationStatus

# stage -> (stuck-point code, label, who acts, the next human action)
_STUCK: dict[Stage, tuple[str, str, str, str]] = {
    Stage.ISSUED: (
        "unnotified",
        "Not notified",
        "Screening coordinator",
        "Confirm the result notice reached the patient, or try another channel",
    ),
    Stage.NOTIFIED: (
        "not_understood",
        "Not yet understood",
        "Screening coordinator",
        "Call the patient in their language and confirm they understood the next step",
    ),
    Stage.ACKNOWLEDGED: (
        "referral_not_sent",
        "Referral not sent",
        "Screening coordinator",
        "Send the referral to an in-network unit (the Router proposes one)",
    ),
    Stage.REFERRED: (
        "referral_unaccepted",
        "Referral not accepted",
        "Screening coordinator",
        "Chase the receiving unit, or re-route if it cannot accept",
    ),
    Stage.ACCEPTED: (
        "booking_gap",
        "Not booked",
        "Receiving unit coordinator",
        "Book the appointment, or record the barrier and a recovery plan",
    ),
    Stage.BOOKED: (
        "awaiting_visit",
        "Waiting for the visit",
        "Receiving unit coordinator",
        "Send the preparation and reminder messages",
    ),
    Stage.ATTENDED: (
        "outcome_gap",
        "Outcome not recorded",
        "Receiving clinician",
        "Record the outcome",
    ),
    Stage.OUTCOME_RECORDED: (
        "evidence_gap",
        "Closure evidence or sign-off missing",
        "Receiving clinician",
        "Attach the report (T2+ evidence) and sign the closure",
    ),
}
NO_SHOW = (
    "no_show",
    "Missed appointment",
    "Receiving unit coordinator",
    "Call the patient, record the barrier, and rebook",
)
SOON = timedelta(days=2)


@dataclass(frozen=True)
class StuckPoint:
    code: str
    label: str
    who: str
    next_action: str
    status: str  # on_track | at_risk | overdue | paused
    breach_at: datetime | None
    days_in_stage: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "who": self.who,
            "next_action": self.next_action,
            "status": self.status,
            "days_in_stage": self.days_in_stage,
        }


def _stage_times(engine: Engine, case: Case) -> list[tuple[Stage, datetime]]:
    at = {e.seq: datetime.fromisoformat(e.at) for e in engine.audit.events if e.case_id == case.id}
    return [(stage, at[seq]) for stage, seq in case.stage_history if seq in at]


def stuck_point(engine: Engine, case: Case, now: datetime) -> StuckPoint | None:
    """Why this open case has not moved on, who must act, and what they should do."""
    if not case.is_open:
        return None
    code, label, who, action = _STUCK[case.stage]
    if case.stage == Stage.ACCEPTED and case.no_shows:
        code, label, who, action = NO_SHOW
    times = _stage_times(engine, case)
    since = times[-1][1] if times else case.opened_at
    days = round((now - since).total_seconds() / 86400, 1)
    stage_clock = [
        e
        for e in case.expectations
        if e.kind.startswith("stage:") and e.status == ExpectationStatus.ARMED
    ]
    breach = min((e.breach_at for e in stage_clock), default=None)
    if case.is_paused:
        status = "paused"
    elif breach and breach <= now:
        status = "overdue"
    elif breach and breach - now <= SOON:
        status = "at_risk"
    else:
        status = "on_track"
    return StuckPoint(code, label, who, action, status, breach, days)


def pre_breach(engine: Engine, now: datetime) -> list[dict[str, Any]]:
    """Open cases whose current deadline falls within the next two days: act today."""
    out = []
    for case in engine.cases.values():
        point = stuck_point(engine, case, now)
        if point and point.status == "at_risk" and point.breach_at:
            hours = round((point.breach_at - now).total_seconds() / 3600)
            out.append({"case_id": case.id, "label": point.label, "hours_left": hours})
    return sorted(out, key=lambda x: x["hours_left"])


def leakage(engine: Engine, now: datetime) -> dict[str, int]:
    """How many open cases sit at each stuck point (the loop-closure ledger's headline)."""
    counts: dict[str, int] = defaultdict(int)
    for case in engine.cases.values():
        point = stuck_point(engine, case, now)
        if point:
            counts[point.label] += 1
    return dict(counts)


def delay_attribution(engine: Engine, now: datetime) -> dict[str, float]:
    """Total days cases spent waiting on each side: the screening clinic before acceptance,
    the receiving unit after it. Answers "who held the case, and for how long?"."""
    totals: dict[str, float] = defaultdict(float)
    for case in engine.cases.values():
        times = _stage_times(engine, case)
        end = case.closure.closed_at if case.closure else now
        for i, (stage, start) in enumerate(times):
            if stage == Stage.CLOSED:
                continue
            stop = times[i + 1][1] if i + 1 < len(times) else end
            if stage == Stage.NOTIFIED:
                holder = "Patient"
            elif stage in (Stage.ISSUED, Stage.ACKNOWLEDGED, Stage.REFERRED):
                holder = "Screening clinic"
            else:
                holder = "Receiving unit"
            totals[holder] += max((stop - start).total_seconds(), 0) / 86400
    return {k: round(v, 1) for k, v in totals.items()}
