"""HTTP entry point: health, pathways, and the interactive demo with its agents.

Every visitor gets a private sandbox (synthetic data only), kept in memory. Run one instance on
Cloud Run (`--max-instances 1`) so a visitor's requests always reach their sandbox.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from itmam import __version__
from itmam.agents import tag_barriers
from itmam.agents.base import ai_enabled, model_name
from itmam.agents.orchestrator import run_agents
from itmam.domain import Actor, CommandRejected, Stage
from itmam.domain.calendar import UAE_TZ
from itmam.domain.model import Case, ExpectationStatus
from itmam.domain.pathway import load_pathways
from itmam.insights import stuck_point
from itmam.sandbox import DIRECTORY, SCREENING, Sandbox, new_sandbox

PATHWAYS = load_pathways()
WEB = Path(__file__).parent / "web"
MAX_SANDBOXES = 300

app = FastAPI(
    title="Itmam",
    version=__version__,
    description="Every actionable screening result, owned until care is complete. "
    "Synthetic data only.",
)

_sandboxes: OrderedDict[str, Sandbox] = OrderedDict()
_lock = threading.Lock()

STAGES = [s.value for s in Stage]
EVENT_TEXT = {
    "case_opened": "Result received; case opened",
    "message_delivered": "Result notice delivered",
    "acknowledged": "Patient confirmed they understood",
    "understanding_check_failed": "Patient did not pass the understanding check",
    "referral_sent": "Referral sent",
    "referral_accepted": "Referral accepted: ownership moved",
    "referral_rejected": "Referral rejected",
    "appointment_booked": "Appointment booked",
    "attended": "Patient attended",
    "no_show": "Patient missed the appointment",
    "outcome_recorded": "Outcome recorded",
    "closure_proposed": "Closure proposed",
    "closure_signed": "Closure signed",
    "case_closed": "Case closed",
    "warning": "Clock warning",
    "escalated": "Escalated",
    "retry_fired": "Automatic reminder sent",
    "urgency_raised": "Marked urgent by a clinician",
    "ai_suggestion_created": "AI suggested barriers (pending review)",
    "ai_suggestion_reviewed": "Coordinator reviewed the AI suggestion",
    "paused": "Paused",
    "resumed": "Resumed",
    "stage_changed": "Stage changed",
    "consent_recorded": "Consent recorded by the person themselves",
}


# ------------------------------------------------------------------ helpers


def sandbox(sid: str) -> Sandbox:
    with _lock:
        box = _sandboxes.get(sid)
        if box is None:
            raise HTTPException(404, "This demo session expired. Start a fresh demo.")
        _sandboxes.move_to_end(sid)
        return box


def local(dt: Any) -> str:
    return dt.astimezone(UAE_TZ).strftime("%a %d %b, %H:%M") if dt else ""


def case_view(box: Sandbox, c: Case) -> dict[str, Any]:
    profile = box.profiles.get(c.id)
    armed = [e for e in c.expectations if e.status == ExpectationStatus.ARMED]
    nxt = min(armed, key=lambda e: e.breach_at, default=None)
    events = [e for e in box.engine.audit.events if e.case_id == c.id]
    timeline = [
        {
            "at": local(_parse(e.at)),
            "text": EVENT_TEXT.get(e.action, e.action.replace("_", " ")),
            "action": e.action,
            "by": e.actor_role or e.actor_kind,
            "detail": _detail(e.details),
        }
        for e in events
        if e.action != "stage_changed"
    ]
    pending = [
        {"id": s.id, "codes": s.codes}
        for s in box.engine.suggestions.values()
        if s.case_id == c.id and s.status == "pending"
    ]
    point = stuck_point(box.engine, c, box.clock.now())
    return {
        "id": c.id,
        "pathway": PATHWAYS[c.pathway_id].name,
        "people": len(c.subjects),
        "stuck": point.as_dict() if point else None,
        "consents": sum("joint_session" in s.consents for s in c.subjects),
        "name": profile.name if profile else c.id,
        "note": profile.note if profile else "",
        "insurance": profile.insurance if profile else "",
        "female_endoscopist": bool(profile and profile.prefers_female_endoscopist),
        "language": c.subjects[0].language,
        "urgency": c.urgency,
        "stage": "closed" if not c.is_open else c.stage.value,
        "stage_index": len(STAGES) - 1 if not c.is_open else STAGES.index(c.stage.value),
        "owner": c.owner.org,
        "owner_role": c.owner.role.replace("_", " "),
        "level": max((x.level for x in c.escalations), default=0),
        "paused": c.is_paused,
        "barriers": c.barriers,
        "no_shows": c.no_shows,
        "next_deadline": {"what": nxt.kind.replace("stage:", ""), "at": local(nxt.breach_at)}
        if nxt
        else None,
        "overdue": bool(nxt and nxt.breach_at <= box.clock.now()),
        "closure": c.closure.type if c.closure else None,
        "timeline": timeline,
        "pending_suggestions": pending,
    }


def _parse(at: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(at)


def _detail(details: dict[str, Any]) -> str:
    keep = ("to_org", "reason", "level", "target", "outcome", "type", "codes", "channel", "tiers")

    def show(value: Any) -> str:
        if isinstance(value, dict):
            return ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in value.items())
        return str(value)

    parts = [f"{k.replace('_', ' ')}: {show(details[k])}" for k in keep if details.get(k)]
    return "; ".join(str(p) for p in parts)


def state(box: Sandbox, sid: str, message: str = "", ok: bool = True) -> dict[str, Any]:
    return {
        "sid": sid,
        "ok": ok,
        "message": message,
        **box.summary(),
        "cases": [case_view(box, c) for c in box.engine.cases.values()],
    }


def act(sid: str, fn: Any) -> dict[str, Any]:
    box = sandbox(sid)
    with _lock:
        try:
            message = fn(box) or "Done."
            return state(box, sid, message)
        except CommandRejected as refused:
            return state(box, sid, f"Refused by the rules: {refused.message}", ok=False)


# ---------------------------------------------------------------- endpoints


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/pathways")
def list_pathways() -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "version": p.version,
            "name": p.name,
            "result_codes": p.trigger.result_codes,
            "subjects": p.subjects.count,
            "care_clock": {
                "target": str(p.care_clock.target),
                "hard_limit": str(p.care_clock.hard_limit),
            },
        }
        for p in PATHWAYS.values()
    ]


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.post("/api/demo")
def start_demo() -> dict[str, Any]:
    sid = secrets.token_urlsafe(12)
    box = new_sandbox()
    with _lock:
        _sandboxes[sid] = box
        while len(_sandboxes) > MAX_SANDBOXES:
            _sandboxes.popitem(last=False)
    return state(box, sid, "A fresh demo with five synthetic cases.")


@app.get("/api/demo/{sid}")
def get_demo(sid: str) -> dict[str, Any]:
    box = sandbox(sid)
    with _lock:
        return state(box, sid)


class Advance(BaseModel):
    days: float = 1


@app.post("/api/demo/{sid}/advance")
def advance(sid: str, body: Advance) -> dict[str, Any]:
    days = max(0.0, min(body.days, 60.0))

    def run(box: Sandbox) -> str:
        before = len(box.engine.audit)
        box.advance(days)
        fired = [e.action for e in box.engine.audit.events[before:]]
        n_esc, n_rem = fired.count("escalated"), fired.count("retry_fired")
        return f"Clock moved {days:g} day(s). {n_rem} automatic reminder(s), {n_esc} escalation(s)."

    return act(sid, run)


class Next(BaseModel):
    to_org: str | None = None


@app.post("/api/demo/{sid}/cases/{cid}/next")
def next_step(sid: str, cid: str, body: Next) -> dict[str, Any]:
    return act(sid, lambda box: box.next_step(cid, body.to_org))


@app.get("/api/demo/{sid}/cases/{cid}/agents")
def agents(sid: str, cid: str) -> dict[str, Any]:
    box = sandbox(sid)
    with _lock:
        return run_agents(box, cid)


class Reply(BaseModel):
    text: str


@app.post("/api/demo/{sid}/cases/{cid}/reply")
def patient_reply(sid: str, cid: str, body: Reply) -> dict[str, Any]:
    def run(box: Sandbox) -> str:
        proposal = tag_barriers(box, cid, body.text)
        codes = proposal.details["codes"]
        box.ai_log.append(
            {
                "at": box.clock.now().isoformat(),
                "case_id": cid,
                "agent": "navigator+guard",
                "source": proposal.source,
                "summary": proposal.summary,
                "guard": proposal.details["guard"],
                "applied": False,
            }
        )
        if proposal.details["injection"]:
            return (
                "The Guard flagged this reply as a possible prompt injection: it was kept away "
                "from the AI and nothing changed. A coordinator should read it."
            )
        if not codes:
            return "The Navigator found no barrier in that reply."
        box.engine.suggest_barriers(Actor.ai(), cid, codes, f"navigator/{proposal.source}")
        return f"The Navigator suggests: {', '.join(codes)}. A coordinator must approve it."

    return act(sid, run)


class Review(BaseModel):
    accept: bool


@app.post("/api/demo/{sid}/cases/{cid}/suggestions/{sug}")
def review(sid: str, cid: str, sug: str, body: Review) -> dict[str, Any]:
    def run(box: Sandbox) -> str:
        c = box.case(cid)
        box.engine.review_suggestion(box.staff(c).coordinator(c.owner.org), sug, body.accept)
        return "Approved by the coordinator." if body.accept else "Rejected by the coordinator."

    return act(sid, run)


MISTAKES = {
    "ai_closes": "The AI tries to close the case",
    "wrong_org_accepts": "A unit that was not sent the referral tries to accept it",
    "close_without_evidence": "Someone closes the case without evidence",
    "coordinator_pauses_urgent": "A coordinator pauses an urgent case",
    "ai_evidence": "Someone uses an AI-written summary as closure evidence",
    "joint_without_consent": "A joint session is booked before both partners consent",
}


@app.post("/api/demo/{sid}/cases/{cid}/mistake/{kind}")
def mistake(sid: str, cid: str, kind: str) -> dict[str, Any]:
    if kind not in MISTAKES:
        raise HTTPException(404, "unknown mistake")

    def run(box: Sandbox) -> str:
        c = box.case(cid)
        e, staff = box.engine, box.staff(c)
        if kind == "ai_closes":
            e.propose_closure(Actor.ai(), cid, "not_actionable", {"clinical_review_note": "x"})
        elif kind == "wrong_org_accepts":
            other = next(p.org for p in DIRECTORY if p.org != c.owner.org)
            e.accept_referral(staff.coordinator(other), cid)
        elif kind == "close_without_evidence":
            e.propose_closure(staff.coordinator(c.owner.org), cid, "not_actionable", {})
        elif kind == "ai_evidence":
            e.propose_closure(
                staff.coordinator(c.owner.org),
                cid,
                "not_actionable",
                {"clinical_review_note": "ai: model summary says no follow-up is needed"},
            )
        elif kind == "joint_without_consent":
            if len(c.subjects) < 2:
                return "Try this on Aisha & Khalid: only couple cases have joint sessions."
            if all("joint_session" in x.consents for x in c.subjects):
                return "Both partners have already consented, so a joint session is allowed."
            e.book_appointment(
                staff.coordinator(c.owner.org), cid, box.clock.now() + timedelta(days=3), True
            )
        else:
            if c.urgency != "urgent":
                return "Try this on Sara: only urgent cases need a clinician to pause them."
            wake = box.clock.now() + timedelta(days=2)
            e.pause(staff.coordinator(c.owner.org), cid, "travelling", wake)
        return "The engine accepted it. That should never happen."

    result = act(sid, run)
    if result["ok"] is False:
        result["message"] = f"{MISTAKES[kind]}: {result['message']}"
    return result


@app.get("/api/demo/{sid}/governance")
def governance(sid: str) -> dict[str, Any]:
    """What the AI did in this sandbox, and the controls around it."""
    box = sandbox(sid)
    with _lock:
        suggestions = list(box.engine.suggestions.values())
        return {
            "ai_enabled": ai_enabled(),
            "model": model_name() or "none (rules only)",
            "controls": [
                "Agents propose; only people act, through the case engine's fifteen rules",
                "AI-written text is never accepted as closure evidence (evidence ladder, I15)",
                "AI actor refused for every command except suggestions",
                "Guard screens patient text for prompt injection and personal data",
                "No patient names or identifiers are sent to the model",
                "Kill switch: ITMAM_AI=off runs every agent on rules only",
                "Every proposal and review is logged; the audit log is hash-chained",
            ],
            "proposals_logged": len(box.ai_log),
            "suggestions": {
                "pending": sum(s.status == "pending" for s in suggestions),
                "accepted": sum(s.status == "accepted" for s in suggestions),
                "rejected": sum(s.status == "rejected" for s in suggestions),
            },
            "log": box.ai_log[-50:][::-1],
        }


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(WEB / "sw.js", media_type="text/javascript")


@app.get("/icon.svg", include_in_schema=False)
def icon() -> FileResponse:
    return FileResponse(WEB / "icon.svg", media_type="image/svg+xml")


@app.get("/api/directory")
def directory() -> list[dict[str, Any]]:
    return [p.__dict__ | {"screening_clinic": SCREENING} for p in DIRECTORY]
