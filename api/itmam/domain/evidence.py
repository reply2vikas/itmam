"""The evidence ladder: how strong a piece of closure evidence is.

Evidence values may name their source as a prefix, for example ``malaffi:ENC-123`` or
``self:patient says it was done``. The source sets the tier:

    T0  self        the patient's own report
    T1  navigator   a navigator's or coordinator's attestation
    T2  staff       a record entered by the owning clinical team (the default, no prefix)
    T2  scheduling  the scheduling system shows the visit
    T3  emr         the procedure or counselling report in the provider's record
    T3  malaffi     the same, confirmed through the health information exchange
    T4  pathology   a pathology or laboratory result

Two rules, enforced by the engine when a closure is proposed (invariant I15):

- AI-generated text (``ai:`` prefix) is never closure evidence, at any tier.
- A closure that counts as success needs every piece of evidence at T2 or above, so a case
  cannot close as "care completed" on the patient's word or an attestation alone.
"""

from __future__ import annotations

from collections.abc import Mapping

TIERS: dict[str, int] = {
    "self": 0,
    "navigator": 1,
    "staff": 2,
    "scheduling": 2,
    "emr": 3,
    "malaffi": 3,
    "pathology": 4,
}
AI_SOURCE = "ai"
SUCCESS_MIN_TIER = 2


def source_of(value: str) -> str:
    """The evidence source named by a value's prefix; unprefixed values are staff records."""
    prefix, sep, _ = str(value).partition(":")
    prefix = prefix.strip().lower()
    if sep and (prefix in TIERS or prefix == AI_SOURCE):
        return prefix
    return "staff"


def tier_of(value: str) -> int | None:
    """The value's tier, or None for AI-generated text (which has no tier)."""
    source = source_of(value)
    return None if source == AI_SOURCE else TIERS[source]


def ladder_problems(evidence: Mapping[str, str], counts_as: str) -> list[str]:
    """Why this evidence cannot close a case; empty when it can."""
    problems: list[str] = []
    for kind, value in evidence.items():
        tier = tier_of(value)
        if tier is None:
            problems.append(f"{kind} is AI-generated text, which is never closure evidence")
        elif counts_as == "success" and tier < SUCCESS_MIN_TIER:
            problems.append(
                f"{kind} is tier T{tier} ({source_of(value)}); care can only be marked complete "
                f"with T{SUCCESS_MIN_TIER} or stronger evidence"
            )
    return problems


def tiers(evidence: Mapping[str, str]) -> dict[str, str]:
    """A readable summary for the audit log, e.g. {'outcome_record': 'T3 malaffi'}."""
    out: dict[str, str] = {}
    for kind, value in evidence.items():
        tier = tier_of(value)
        out[kind] = "AI (rejected)" if tier is None else f"T{tier} {source_of(value)}"
    return out
