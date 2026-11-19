"""Router agent: picks the receiving unit for a referral.

It reads the provider directory and the patient's profile, never the other way round, and
proposes one unit with its reasons. Rules, in order:

1. Only units that are accepting referrals and take the patient's insurance.
2. Never a unit that has already rejected this case.
3. If the patient asked for a female endoscopist, only units that have one; if none qualifies,
   say so rather than quietly ignore the preference.
4. Urgent cases need a slot within 7 days, or the proposal is to escalate to a clinician.
5. Among the rest: the earliest slot, then a unit that speaks the patient's language.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from itmam.agents.base import Proposal

if TYPE_CHECKING:
    from itmam.sandbox import Provider, Sandbox

URGENT_SLOT_DAYS = 7


def rank(
    providers: tuple[Provider, ...],
    insurance: str,
    language: str,
    female_only: bool,
    rejected_by: set[str],
) -> tuple[list[Provider], list[str]]:
    """The eligible units, best first, and why the others were excluded."""
    excluded: list[str] = []
    eligible: list[Provider] = []
    for p in providers:
        if not p.accepting:
            excluded.append(f"{p.org}: not accepting referrals")
        elif insurance not in p.networks:
            excluded.append(f"{p.org}: does not take {insurance}")
        elif p.org in rejected_by:
            excluded.append(f"{p.org}: already rejected this case")
        elif female_only and not p.female_endoscopist:
            excluded.append(f"{p.org}: no female endoscopist")
        else:
            eligible.append(p)
    eligible.sort(key=lambda p: (p.next_slot_days, language not in p.languages, p.org))
    return eligible, excluded


def route(box: Sandbox, case_id: str) -> Proposal:
    from itmam.sandbox import DIRECTORY

    case = box.case(case_id)
    profile = box.profiles[case_id]
    language = case.subjects[0].language
    rejected = {r.to_org for r in case.referrals if r.status == "rejected"}
    eligible, excluded = rank(
        DIRECTORY, profile.insurance, language, profile.prefers_female_endoscopist, rejected
    )
    checks = ["insurance network", "accepting referrals", "previous rejections"]
    if profile.prefers_female_endoscopist:
        checks.append("female endoscopist preference")
    if not eligible:
        return Proposal(
            "router",
            case_id,
            "No unit fits: escalate to the screening clinician to decide.",
            {"org": None, "excluded": excluded, "escalate": True},
            checks=checks,
        )
    best = eligible[0]
    if case.urgency == "urgent":
        checks.append(f"urgent: slot within {URGENT_SLOT_DAYS} days")
        if best.next_slot_days > URGENT_SLOT_DAYS:
            return Proposal(
                "router",
                case_id,
                f"Urgent, but the earliest suitable slot is in {best.next_slot_days} days: "
                "ask a clinician to expedite.",
                {"org": best.org, "excluded": excluded, "escalate": True},
                checks=checks,
            )
    reasons = [
        f"takes {profile.insurance}",
        f"next slot in {best.next_slot_days} days",
        "speaks the patient's language" if language in best.languages else "interpreter needed",
    ]
    if profile.prefers_female_endoscopist:
        reasons.append("has a female endoscopist")
    return Proposal(
        "router",
        case_id,
        f"Refer to {best.org}: " + "; ".join(reasons) + ".",
        {"org": best.org, "excluded": excluded, "alternatives": [p.org for p in eligible[1:]]},
        apply={"action": "next_step", "to_org": best.org}
        if case.stage.value == "acknowledged"
        else None,
        checks=checks,
    )
