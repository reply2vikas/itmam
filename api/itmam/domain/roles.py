"""Who counts as what. Pathways name their own roles; these helpers map them to the
logical tokens used by closure sign-off and pause approval rules."""

from __future__ import annotations

from itmam.domain.model import Actor, ActorKind, Owner, Side
from itmam.domain.pathway import PathwayConfig

SUPERVISOR = "supervisor"  # supervising clinician of an organisation; reserved role name


def side_roles(p: PathwayConfig, side: Side) -> tuple[str, str]:
    """(coordinator role, clinician role) for one side of the hand-off."""
    if side == Side.SENDER:
        return p.roles.sender, p.roles.sender_clinician
    return p.roles.receiver, p.roles.receiver_clinician


def is_clinician(p: PathwayConfig, actor: Actor) -> bool:
    return actor.role in {p.roles.sender_clinician, p.roles.receiver_clinician, SUPERVISOR}


def owner_staff(p: PathwayConfig, owner: Owner, actor: Actor) -> bool:
    """A person in the owning organisation, on the owning side of the hand-off."""
    coordinator, clinician = side_roles(p, owner.side)
    return (
        actor.kind == ActorKind.HUMAN
        and actor.org == owner.org
        and actor.role in {coordinator, clinician, SUPERVISOR}
    )


def token_satisfied(p: PathwayConfig, owner: Owner, actor: Actor, token: str) -> bool:
    """Does this person fill a sign-off or approval slot of the given kind?"""
    if not owner_staff(p, owner, actor):
        return False
    if token == "coordinator":
        return True
    if token == "clinician":
        return is_clinician(p, actor)
    if token == "sender_clinician":
        return actor.role == p.roles.sender_clinician
    if token == "receiver_clinician":
        return actor.role == p.roles.receiver_clinician
    if token == "supervisor":
        return actor.role == SUPERVISOR
    return False
