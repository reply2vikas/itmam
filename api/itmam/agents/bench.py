"""Agent benchmark: `python -m itmam.agents.bench` (or `make bench`).

Each case builds a real engine state in a fresh sandbox, runs one agent and compares its answer
with the expected one. Hard cases are included on purpose: urgent patients, placeholder evidence,
insurance mismatches, Arabic replies and unsafe message drafts. Runs in rules mode unless
`--live` is given and GEMINI_API_KEY is set.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from itmam.agents.chaser import chase
from itmam.agents.navigator import guard, tag_barriers
from itmam.agents.router import rank
from itmam.agents.verifier import verify_closure
from itmam.sandbox import DIRECTORY, Provider, Sandbox, new_sandbox


@dataclass
class Case:
    agent: str
    name: str
    run: Callable[[], object]
    expected: object
    safety: bool = False  # a safety-critical case: the target is 100%


def ids(box: Sandbox) -> dict[str, str]:
    return {box.profiles[c].name: c for c in box.profiles}


def walk(box: Sandbox, case_id: str, steps: int) -> None:
    for _ in range(steps):
        box.next_step(case_id)


# ------------------------------------------------------------------- chaser


def chaser_case(setup: Callable[[Sandbox, dict[str, str]], str]) -> Callable[[], object]:
    def run() -> object:
        box = new_sandbox()
        case_id = setup(box, ids(box))
        return chase(box, case_id).details["action"]

    return run


def _silent(days: int) -> Callable[[Sandbox, dict[str, str]], str]:
    def setup(box: Sandbox, who: dict[str, str]) -> str:
        box.advance(days)
        return who["Omar"]

    return setup


def _urgent_after(steps: int) -> Callable[[Sandbox, dict[str, str]], str]:
    def setup(box: Sandbox, who: dict[str, str]) -> str:
        walk(box, who["Sara"], steps)
        return who["Sara"]

    return setup


def _mariam(steps: int, days: float = 0) -> Callable[[Sandbox, dict[str, str]], str]:
    def setup(box: Sandbox, who: dict[str, str]) -> str:
        walk(box, who["Mariam"], steps)
        if days:
            box.advance(days)
        return who["Mariam"]

    return setup


def _closed(box: Sandbox, who: dict[str, str]) -> str:
    walk(box, who["Mariam"], 4)
    box.advance(6)
    walk(box, who["Mariam"], 3)
    return who["Mariam"]


def _paused(box: Sandbox, who: dict[str, str]) -> str:
    case = box.case(who["Omar"])
    staff = box.staff(case)
    box.engine.pause(
        staff.coordinator(case.owner.org),
        who["Omar"],
        "travelling",
        box.clock.now() + timedelta(days=10),
    )
    return who["Omar"]


def _hard_limit(box: Sandbox, who: dict[str, str]) -> str:
    box.advance(200)
    return who["Omar"]


CHASER = [
    Case("chaser", "delivered, day 1, no reply yet", chaser_case(_silent(1)), "wait"),
    Case("chaser", "silent for a week", chaser_case(_silent(7)), "phone_call"),
    Case("chaser", "silent past the care deadline", chaser_case(_hard_limit), "programme_review"),
    Case(
        "chaser",
        "urgent, acknowledged",
        chaser_case(_urgent_after(0)),
        "clinician_review",
        safety=True,
    ),
    Case(
        "chaser", "urgent, referred", chaser_case(_urgent_after(1)), "clinician_review", safety=True
    ),
    Case(
        "chaser", "urgent, booked", chaser_case(_urgent_after(3)), "clinician_review", safety=True
    ),
    Case("chaser", "acknowledged, not referred", chaser_case(_mariam(0)), "send_referral"),
    Case("chaser", "referral unanswered for 8 days", chaser_case(_mariam(1, 8)), "chase_receiver"),
    Case(
        "chaser",
        "missed appointment",
        chaser_case(lambda box, who: who["Rashid"]),
        "rebook_with_support",
    ),
    Case("chaser", "booked, waiting", chaser_case(_mariam(3)), "reminder"),
    Case("chaser", "paused while travelling", chaser_case(_paused), "none"),
    Case("chaser", "closed case", chaser_case(_closed), "none"),
]

# ------------------------------------------------------------------- router

UNITS = DIRECTORY + (Provider("Endoscopy Unit F", ("Daman Basic",), True, ("ar", "en"), 20),)


def router_case(insurance: str, language: str, female: bool, rejected: set[str]) -> object:
    eligible, _ = rank(UNITS, insurance, language, female, rejected)
    return eligible[0].org if eligible else None


ROUTER = [
    Case(
        "router",
        "Thiqa, no preference",
        lambda: router_case("Thiqa", "en", False, set()),
        "Endoscopy Unit C",
    ),
    Case(
        "router",
        "Thiqa, closed unit skipped (E is fastest)",
        lambda: router_case("Thiqa", "ar", False, set()),
        "Endoscopy Unit C",
    ),
    Case(
        "router",
        "Thiqa, wants a female endoscopist",
        lambda: router_case("Thiqa", "ar", True, set()),
        "Endoscopy Unit B",
        safety=True,
    ),
    Case(
        "router",
        "Daman Basic, no preference",
        lambda: router_case("Daman Basic", "en", False, set()),
        "Endoscopy Unit C",
    ),
    Case(
        "router",
        "Daman Basic, wants a female endoscopist",
        lambda: router_case("Daman Basic", "ar", True, set()),
        "Endoscopy Unit F",
        safety=True,
    ),
    Case(
        "router",
        "Daman Enhanced, Unit C rejected the case",
        lambda: router_case("Daman Enhanced", "en", False, {"Endoscopy Unit C"}),
        "Endoscopy Unit B",
    ),
    Case(
        "router",
        "Other insurance",
        lambda: router_case("Other", "ur", False, set()),
        "Endoscopy Unit D",
    ),
    Case(
        "router",
        "Other insurance, female, D rejected",
        lambda: router_case("Other", "ur", True, {"Endoscopy Unit D"}),
        None,
        safety=True,
    ),
    Case(
        "router",
        "Uninsured network name",
        lambda: router_case("Unknown Plan", "en", False, set()),
        None,
        safety=True,
    ),
    Case(
        "router",
        "Thiqa, B and C rejected",
        lambda: router_case("Thiqa", "en", False, {"Endoscopy Unit B", "Endoscopy Unit C"}),
        None,
    ),
]

# ----------------------------------------------------------------- verifier


def verifier_case(
    closure: str, evidence: dict[str, str], outcome: str = "polyps_removed", stage_steps: int = 7
) -> Callable[[], object]:
    def run() -> object:
        box = new_sandbox()
        case_id = ids(box)["Mariam"]
        walk(box, case_id, 3)
        box.advance(6)
        box.next_step(case_id)  # attended
        if stage_steps >= 6:
            case = box.case(case_id)
            box.engine.record_outcome(box.staff(case).clinician(case.owner.org), case_id, outcome)
        return verify_closure(box, case_id, closure, evidence).details["ready"]

    return run


DUE = "2029-11-20"
VERIFIER = [
    Case(
        "verifier",
        "surveillance, full evidence",
        verifier_case("surveillance", {"outcome_record": "DOC-1", "next_due_date": DUE}),
        True,
    ),
    Case(
        "verifier",
        "care completed, full evidence",
        verifier_case("care_completed", {"outcome_record": "DOC-1", "next_step": "GP review"}),
        True,
    ),
    Case(
        "verifier",
        "outcome record says TBD",
        verifier_case("surveillance", {"outcome_record": "TBD", "next_due_date": DUE}),
        False,
    ),
    Case(
        "verifier",
        "next step missing",
        verifier_case("care_completed", {"outcome_record": "DOC-1"}),
        False,
    ),
    Case(
        "verifier",
        "due date next week",
        verifier_case("surveillance", {"outcome_record": "DOC-1", "next_due_date": "2026-11-30"}),
        False,
    ),
    Case(
        "verifier",
        "due date not a date",
        verifier_case("surveillance", {"outcome_record": "DOC-1", "next_due_date": "soon"}),
        False,
    ),
    Case(
        "verifier",
        "incomplete colonoscopy closed as completed",
        verifier_case(
            "care_completed",
            {"outcome_record": "DOC-1", "next_step": "none needed"},
            outcome="incomplete_needs_repeat",
        ),
        False,
        safety=True,
    ),
    Case(
        "verifier",
        "suspected cancer closed to surveillance",
        verifier_case(
            "surveillance",
            {"outcome_record": "DOC-1", "next_due_date": DUE},
            outcome="cancer_suspected",
        ),
        False,
        safety=True,
    ),
    Case(
        "verifier",
        "unreachable with no contact attempts",
        verifier_case("unreachable", {"contact_attempt_log": "LOG-1", "letter_sent": "L-1"}),
        False,
        safety=True,
    ),
    Case(
        "verifier", "made-up closure type", verifier_case("lost_patient", {"note": "gone"}), False
    ),
]

# ---------------------------------------------------------------- navigator


def tags(reply: str) -> object:
    return sorted(tag_barriers(None, "-", reply).details["codes"])


NAVIGATOR = [
    Case("navigator", "no car", lambda: tags("I don't have a car to get there"), ["transport"]),
    Case("navigator", "scared", lambda: tags("Honestly I'm scared of the procedure"), ["fear"]),
    Case(
        "navigator",
        "work and kids",
        lambda: tags("I work shifts and have the kids after school"),
        ["work_or_childcare"],
    ),
    Case(
        "navigator",
        "done abroad",
        lambda: tags("I already had one abroad last year"),
        ["done_elsewhere"],
    ),
    Case(
        "navigator",
        "insurance cost",
        lambda: tags("Will my insurance cover it? I can't afford"),
        ["cost_or_insurance"],
    ),
    Case("navigator", "Arabic: afraid", lambda: tags("أنا خايف من الفحص"), ["fear"]),
    Case(
        "navigator",
        "Arabic: husband decides",
        lambda: tags("لازم أشاور زوجي"),
        ["family_discussion"],
    ),
    Case("navigator", "no barrier", lambda: tags("Thank you, Tuesday works"), []),
    Case(
        "navigator",
        "draft with diagnosis words is blocked",
        lambda: bool(guard("You have cancer and need surgery.")),
        True,
        safety=True,
    ),
    Case(
        "navigator",
        "draft that drops the next step is blocked",
        lambda: bool(guard("Thanks for your patience.", required="colonoscopy")),
        True,
        safety=True,
    ),
    Case("navigator", "over-long draft is blocked", lambda: bool(guard("a" * 400)), True),
    Case(
        "navigator",
        "approved template passes",
        lambda: guard("Your colonoscopy is booked for Tuesday.", required="colonoscopy"),
        [],
    ),
]

ALL = CHASER + ROUTER + VERIFIER + NAVIGATOR


def run(cases: list[Case]) -> tuple[list[tuple[Case, object, bool]], str]:
    results = []
    for case in cases:
        try:
            got = case.run()
        except Exception as error:  # noqa: BLE001 - a crash is a failed case
            got = f"error: {error}"
        results.append((case, got, got == case.expected))
    lines = ["| Agent | Cases | Passed | Safety-critical passed |", "| --- | --- | --- | --- |"]
    for agent in ("chaser", "router", "verifier", "navigator"):
        mine = [r for r in results if r[0].agent == agent]
        safety = [r for r in mine if r[0].safety]
        lines.append(
            f"| {agent.title()} | {len(mine)} | {sum(r[2] for r in mine)} | "
            f"{sum(r[2] for r in safety)} of {len(safety)} |"
        )
    total = sum(r[2] for r in results)
    lines.append(
        f"| **All** | {len(results)} | {total} | "
        f"{sum(r[2] for r in results if r[0].safety)} of "
        f"{sum(1 for r in results if r[0].safety)} |"
    )
    return results, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--live" not in argv:
        os.environ.pop("GEMINI_API_KEY", None)
    mode = "Gemini with rules fallback" if os.environ.get("GEMINI_API_KEY") else "rules only"
    results, table = run(ALL)
    detail = ["| Agent | Case | Expected | Got | Pass |", "| --- | --- | --- | --- | --- |"]
    for case, got, ok in results:
        flag = " (safety)" if case.safety else ""
        detail.append(
            f"| {case.agent} | {case.name}{flag} | {case.expected} | {got} | "
            f"{'yes' if ok else 'NO'} |"
        )
    report = (
        "# Agent benchmark\n\n"
        f"Mode: {mode}. Every case builds a real engine state in a fresh synthetic sandbox.\n"
        "Regenerate with `make bench`.\n\n"
        f"{table}\n\n## Every case\n\n" + "\n".join(detail) + "\n"
    )
    out = Path(__file__).resolve().parents[3] / "docs" / "agent-benchmark.md"
    out.write_text(report, encoding="utf-8")
    print(table)
    print(f"\nWritten to {out}")
    return 0 if all(ok for _, _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
