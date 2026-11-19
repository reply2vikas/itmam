"""Verifier agent: checks a closure before anyone signs it.

The engine already refuses a closure with missing evidence. The Verifier catches what the engine
cannot know from field names alone: placeholder text, a due date in the wrong range, and a
closure type that contradicts the recorded outcome.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import TYPE_CHECKING

from itmam.agents.base import Proposal

if TYPE_CHECKING:
    from itmam.sandbox import Sandbox

PLACEHOLDERS = {"", "tbd", "todo", "n/a", "na", "none", "-", "?", "pending", "xxx", "test"}

# Outcome -> closure types that do not fit it (the case is not finished).
CONFLICTS = {
    "incomplete_needs_repeat": {"care_completed", "surveillance"},
    "cancer_suspected": {"surveillance"},
}

SURVEILLANCE_RANGE = (timedelta(days=180), timedelta(days=10 * 365))


def verify_closure(
    box: Sandbox, case_id: str, closure_type: str, evidence: Mapping[str, str]
) -> Proposal:
    case = box.case(case_id)
    p = box.engine.pathway_of(case)
    issues: list[str] = []
    checks = ["closure type", "stage", "evidence present", "placeholder text", "outcome fit"]
    rule = p.closures.get(closure_type)
    if rule is None:
        issues.append(f"{closure_type!r} is not a closure type for this pathway")
    else:
        if rule.requires_stage and case.stage.value != rule.requires_stage:
            issues.append(f"needs stage {rule.requires_stage}; the case is {case.stage.value}")
        for kind in rule.evidence:
            value = str(evidence.get(kind, "")).strip()
            if not value:
                issues.append(f"missing evidence: {kind}")
            elif value.lower() in PLACEHOLDERS:
                issues.append(f"{kind} is placeholder text ({value!r})")
        if rule.min_contact_attempts:
            checks.append("contact attempts")
            made = len(case.contact_attempts)
            if made < rule.min_contact_attempts:
                issues.append(f"{made} of {rule.min_contact_attempts} contact attempts made")
        if "next_due_date" in rule.evidence and evidence.get("next_due_date"):
            checks.append("due-date range")
            try:
                due = date.fromisoformat(str(evidence["next_due_date"]))
                gap = due - box.clock.now().date()
                low, high = SURVEILLANCE_RANGE
                if not low <= gap <= high:
                    issues.append("next due date should be 6 months to 10 years ahead")
            except ValueError:
                issues.append("next_due_date is not a date (YYYY-MM-DD)")
        if case.outcome and closure_type in CONFLICTS.get(case.outcome.type, set()):
            issues.append(
                f"outcome is {case.outcome.type.replace('_', ' ')}: "
                f"{closure_type.replace('_', ' ')} would close a case that still needs care"
            )
    signers = ", ".join(rule.signers) if rule else "-"
    if issues:
        summary = "Not ready to sign: " + "; ".join(issues) + "."
    else:
        summary = f"Ready for signature by: {signers}."
    return Proposal(
        "verifier",
        case_id,
        summary,
        {"closure_type": closure_type, "issues": issues, "ready": not issues, "signers": signers},
        checks=checks,
    )
