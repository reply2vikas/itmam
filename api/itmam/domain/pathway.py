"""Pathway configuration: one YAML file per pathway, validated at startup.

A pathway sets the clocks, owners, closure rules and pause rules for the engine's fixed stage
template (notify -> acknowledge -> refer -> accept -> book -> attend -> outcome -> close).
Adding a pathway adds a YAML file, not code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    WithJsonSchema,
    model_validator,
)

from itmam.domain.calendar import Duration

DurationField = Annotated[
    Duration,
    PlainValidator(Duration.parse),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^\d+(wd|d|h)$", "examples": ["2wd", "5d"]}),
]

Side = Literal["sender", "receiver"]
StageKey = Literal["notify", "acknowledge", "refer", "accept", "book", "attend", "outcome", "close"]
SignerToken = Literal[
    "coordinator", "clinician", "sender_clinician", "receiver_clinician", "supervisor"
]
ApproverToken = Literal["coordinator", "clinician"]

STAGE_ORDER: tuple[StageKey, ...] = (
    "notify",
    "acknowledge",
    "refer",
    "accept",
    "book",
    "attend",
    "outcome",
    "close",
)
SENDER_STAGES = frozenset({"notify", "acknowledge", "refer", "accept"})
PATIENT_FACING_STAGES = frozenset({"notify", "acknowledge"})
REQUIRED_MESSAGES = frozenset({"result_notice", "reminder"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Trigger(_Model):
    result_codes: list[str] = Field(min_length=1)
    urgency: Literal["standard", "urgent"] = "standard"


class Subjects(_Model):
    count: Literal[1, 2] = 1
    contact: Literal["self", "guardian"] = "self"
    joint_session_requires_consent: bool = False


class Roles(_Model):
    sender: str
    sender_clinician: str
    receiver: str
    receiver_clinician: str


class Retry(_Model):
    after: DurationField
    channel: Literal["sms"] | None = None
    task: Literal["phone_call"] | None = None

    @model_validator(mode="after")
    def _one_action(self) -> Retry:
        if (self.channel is None) == (self.task is None):
            raise ValueError("a retry needs exactly one of 'channel' or 'task'")
        return self


class OnReject(_Model):
    return_to: Literal["sender"] = "sender"
    reason_required: Literal[True] = True
    reroute_breach: DurationField


class NoShow(_Model):
    rebook_breach: DurationField
    escalate_after: int = Field(ge=1)


class StageClock(_Model):
    owner: Side
    expect: str
    warn: DurationField | None = None
    breach: DurationField
    from_: Literal["stage_start", "result_issued", "appointment"] = Field(
        default="stage_start", alias="from"
    )
    retries: list[Retry] = Field(default_factory=list)
    on_reject: OnReject | None = None
    no_show: NoShow | None = None


class CareClock(_Model):
    """Overall clock from result issued until the attend stage is recorded."""

    target: DurationField
    hard_limit: DurationField


class Econsult(_Model):
    allowed: bool = True
    reply_breach: DurationField


class SimpleClock(_Model):
    breach: DurationField


class ClosureRule(_Model):
    evidence: list[str] = Field(min_length=1)
    signers: list[SignerToken] = Field(min_length=1, max_length=2)
    counts_as: Literal["success", "neutral", "failure"]
    requires_stage: Literal["outcome_recorded"] | None = None
    creates_watch: bool = False
    min_contact_attempts: int | None = Field(default=None, ge=1)


class PauseRule(_Model):
    max: DurationField
    approver: ApproverToken
    note_required: bool = False


class Watch(_Model):
    grace: DurationField
    result_code: str


class EscalationLadder(_Model):
    l1_to_l2: DurationField = Duration(2, "wd")
    l2_to_l3: DurationField = Duration(2, "wd")
    repeat_l3: DurationField = Duration(2, "wd")


class PathwayConfig(_Model):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    version: int = Field(ge=1)
    name: str
    trigger: Trigger
    refer_after: Literal["acknowledged"] = "acknowledged"
    calendar: Literal["uae_working_days"] = "uae_working_days"
    subjects: Subjects = Field(default_factory=Subjects)
    roles: Roles
    languages: list[Literal["ar", "en"]] = Field(min_length=1)
    messages: list[str] = Field(min_length=1)
    stages: dict[StageKey, StageClock]
    care_clock: CareClock
    econsult: Econsult
    callback: SimpleClock
    call_task: SimpleClock
    closure_signoff: SimpleClock
    escalation: EscalationLadder = Field(default_factory=EscalationLadder)
    outcomes: list[str] = Field(min_length=1)
    closures: dict[str, ClosureRule]
    pauses: dict[str, PauseRule]
    barriers: list[str] = Field(min_length=1)
    watch: Watch | None = None

    @model_validator(mode="after")
    def _lint(self) -> PathwayConfig:
        problems = lint(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def stage(self, key: StageKey) -> StageClock:
        return self.stages[key]


def _duplicates(items: list[str]) -> set[str]:
    return {item for item in items if items.count(item) > 1}


def lint(p: PathwayConfig) -> list[str]:
    """Rules the schema alone cannot express. An empty list means the pathway is valid."""
    problems: list[str] = []
    missing = [key for key in STAGE_ORDER if key not in p.stages]
    if missing:
        problems.append(f"missing stages: {', '.join(missing)}")
    for key, clock in p.stages.items():
        expected_owner = "sender" if key in SENDER_STAGES else "receiver"
        if clock.owner != expected_owner:
            problems.append(
                f"stage {key}: owner must be {expected_owner} "
                "(ownership moves only when the referral is accepted)"
            )
        if clock.warn is not None and clock.warn.approx_days() >= clock.breach.approx_days():
            problems.append(f"stage {key}: warn must come before breach")
        if clock.on_reject is not None and key != "accept":
            problems.append(f"stage {key}: on_reject belongs to the accept stage only")
        if clock.no_show is not None and key != "attend":
            problems.append(f"stage {key}: no_show belongs to the attend stage only")
        if clock.from_ == "appointment" and key != "attend":
            problems.append(f"stage {key}: 'from: appointment' belongs to the attend stage only")
        if clock.retries and key not in PATIENT_FACING_STAGES:
            problems.append(f"stage {key}: retries belong to patient-facing stages only")
    accept = p.stages.get("accept")
    if accept is not None and accept.on_reject is None:
        problems.append("stage accept: on_reject is required")
    attend = p.stages.get("attend")
    if attend is not None:
        if attend.no_show is None:
            problems.append("stage attend: no_show is required")
        if attend.from_ != "appointment":
            problems.append("stage attend: clock must run 'from: appointment'")
    if p.care_clock.target.approx_days() >= p.care_clock.hard_limit.approx_days():
        problems.append("care_clock: target must come before hard_limit")
    if (
        len(
            {p.roles.sender, p.roles.sender_clinician, p.roles.receiver, p.roles.receiver_clinician}
        )
        < 4
    ):
        problems.append("roles: the four roles must be distinct")
    if "supervisor" in p.roles.model_dump().values():
        problems.append("roles: 'supervisor' is reserved")
    for label, items in (
        ("trigger.result_codes", p.trigger.result_codes),
        ("outcomes", p.outcomes),
        ("barriers", p.barriers),
        ("messages", p.messages),
    ):
        dupes = _duplicates(items)
        if dupes:
            problems.append(f"{label}: duplicates {sorted(dupes)}")
    absent = REQUIRED_MESSAGES - set(p.messages)
    if absent:
        problems.append(f"messages: missing {sorted(absent)}")
    if p.subjects.joint_session_requires_consent and p.subjects.count != 2:
        problems.append("subjects: joint-session consent needs two subjects")
    if not any(
        rule.counts_as == "success" and rule.requires_stage == "outcome_recorded"
        for rule in p.closures.values()
    ):
        problems.append("closures: at least one success closure must require outcome_recorded")
    for name, rule in p.closures.items():
        if rule.counts_as == "failure" and rule.min_contact_attempts is None:
            problems.append(f"closure {name}: a failure closure needs min_contact_attempts")
        if rule.creates_watch and p.watch is None:
            problems.append(f"closure {name}: creates_watch needs a 'watch' block")
        if rule.creates_watch and "next_due_date" not in rule.evidence:
            problems.append(f"closure {name}: creates_watch needs 'next_due_date' evidence")
    if p.watch is not None and p.watch.result_code not in p.trigger.result_codes:
        problems.append("watch.result_code must be one of trigger.result_codes")
    return problems


def default_pathways_dir() -> Path:
    configured = os.environ.get("ITMAM_PATHWAYS_DIR")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "pathways"


def load_pathway(path: Path) -> PathwayConfig:
    with path.open(encoding="utf-8") as handle:
        data: Any = yaml.safe_load(handle)
    return PathwayConfig.model_validate(data)


def load_pathways(directory: Path | None = None) -> dict[str, PathwayConfig]:
    """Load every *.yaml file; pathway IDs and trigger result codes must be unique."""
    directory = directory or default_pathways_dir()
    pathways: dict[str, PathwayConfig] = {}
    seen_codes: dict[str, str] = {}
    for path in sorted(directory.glob("*.yaml")):
        pathway = load_pathway(path)
        if pathway.id in pathways:
            raise ValueError(f"duplicate pathway id {pathway.id!r} in {path.name}")
        for code in pathway.trigger.result_codes:
            if code in seen_codes:
                raise ValueError(
                    f"result code {code!r} is used by both {seen_codes[code]} and {pathway.id}"
                )
            seen_codes[code] = pathway.id
        pathways[pathway.id] = pathway
    if not pathways:
        raise ValueError(f"no pathway files found in {directory}")
    return pathways


def json_schema() -> dict[str, Any]:
    return PathwayConfig.model_json_schema(by_alias=True)
