"""Pure domain core: no network, no database. Adapters live outside this package."""

from itmam.domain.calendar import UAE_CALENDAR, Duration, WorkCalendar
from itmam.domain.clock import Clock, ManualClock, SystemClock
from itmam.domain.engine import Engine, NewSubject
from itmam.domain.errors import CommandRejected
from itmam.domain.invariants import Violation, check_all
from itmam.domain.model import Actor, ActorKind, Stage
from itmam.domain.pathway import PathwayConfig, load_pathways
from itmam.domain.templates import TemplateRef, TemplateRegistry

__all__ = [
    "UAE_CALENDAR",
    "Actor",
    "ActorKind",
    "Clock",
    "CommandRejected",
    "Duration",
    "Engine",
    "ManualClock",
    "NewSubject",
    "PathwayConfig",
    "Stage",
    "SystemClock",
    "TemplateRef",
    "TemplateRegistry",
    "Violation",
    "WorkCalendar",
    "check_all",
    "load_pathways",
]
