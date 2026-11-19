"""Orchestrator: runs the right agents for a case, in a fixed order, and explains itself.

It decides nothing clinical. For each agent it writes a short report: what the agent read, what
it proposed, and which evidence was missing. Missing evidence lowers the evidence score, so a
reviewer can see at a glance when a proposal rests on thin information. Every proposal is
written to the sandbox's AI log for governance review.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from itmam.agents.base import Proposal, model_name
from itmam.agents.chaser import chase
from itmam.agents.navigator import draft_message
from itmam.agents.router import route
from itmam.agents.verifier import verify_closure
from itmam.domain import Stage

if TYPE_CHECKING:
    from itmam.sandbox import Sandbox

ROUTING_STAGES = {Stage.ISSUED, Stage.NOTIFIED, Stage.ACKNOWLEDGED}


def evidence(box: Sandbox, case_id: str) -> dict[str, bool]:
    """The inputs the agents rely on, and whether each one is present."""
    case = box.case(case_id)
    profile = box.profiles.get(case_id)
    subject = case.subjects[0]
    return {
        "result code": bool(case.result_code),
        "preferred language": bool(subject.language),
        "contact on file": bool(subject.contact_id),
        "insurance network": bool(profile and profile.insurance),
        "patient acknowledged": subject.acknowledged_at is not None,
        "barriers asked": bool(case.barriers) or case.stage not in {Stage.ACCEPTED, Stage.BOOKED},
    }


def run_agents(box: Sandbox, case_id: str) -> dict[str, Any]:
    case = box.case(case_id)
    proposals: list[Proposal] = [chase(box, case_id), draft_message(box, case_id)]
    if case.is_open and case.stage in ROUTING_STAGES:
        proposals.append(route(box, case_id))
    if case.is_open and case.stage == Stage.OUTCOME_RECORDED:
        due = (box.clock.now() + timedelta(days=3 * 365)).date().isoformat()
        closure_evidence = {"outcome_record": "DOC-COLONOSCOPY-REPORT", "next_due_date": due}
        proposals.append(verify_closure(box, case_id, "surveillance", closure_evidence))
    inputs = evidence(box, case_id)
    missing = [name for name, present in inputs.items() if not present]
    score = round(100 * (len(inputs) - len(missing)) / len(inputs))
    at = box.clock.now().isoformat()
    for p in proposals:
        box.ai_log.append(
            {
                "at": at,
                "case_id": case_id,
                "agent": p.agent,
                "source": p.source,
                "summary": p.summary,
                "applied": False,
            }
        )
    return {
        "order": [p.agent for p in proposals],
        "evidence_score": score,
        "missing_evidence": missing,
        "caution": "Proposals rest on incomplete information: check before acting."
        if missing
        else "",
        "model": model_name() or "none (rules only)",
        "proposals": [p.to_dict() for p in proposals],
    }
