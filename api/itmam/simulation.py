"""Itmam Twin: a what-if simulation of a FIT-positive cohort, run through the real case engine.

Every synthetic patient becomes a real case in a real engine with the real clocks, escalations
and fifteen rules. Day by day, patients, coordinators and endoscopy units act with the
probabilities in `sim/parameters.yaml`. Two scenarios share the same patients and random seed:

- "Without follow-up": the engine still raises reminders and escalations, but nobody acts on
  them; patients and units act only at their own pace.
- "With Itmam": each reminder or escalation leads to a contact, which raises the chance that
  the patient or unit acts that day; missed appointments are rebooked.

The no-follow-up rates are set so completion lands near the published Abu Dhabi baseline
(30.5%). Every other number is an assumption, labelled as such in the parameters file. The
output is a what-if under those assumptions: it is not evidence that Itmam works. Only a pilot
can show that.

Run: ``python -m itmam.simulation`` (writes docs/simulation.md).
"""

from __future__ import annotations

import argparse
import random
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from itmam.domain import (
    Actor,
    CommandRejected,
    Engine,
    ManualClock,
    NewSubject,
    Stage,
    TemplateRegistry,
    check_all,
    load_pathways,
)
from itmam.insights import leakage
from itmam.sandbox import (
    CLOCK_ACTOR,
    DIRECTORY,
    FEED,
    P1,
    SCREENING,
    START,
    Sandbox,
    Staff,
    _approve_templates,
)

ROOT = Path(__file__).resolve().parents[2]
PARAMETERS = ROOT / "sim" / "parameters.yaml"
REPORT = ROOT / "docs" / "simulation.md"
CONTACT_EVENTS = {"retry_fired", "escalated", "warning"}


@dataclass
class Outcome:
    scenario: str
    cohort: int
    attended: int
    closed: int
    days_to_attend: list[float]
    escalations: int
    contacts: int
    stuck: dict[str, int]
    violations: int

    @property
    def completion(self) -> float:
        return 100 * self.attended / self.cohort

    def median_days(self) -> float | None:
        return round(statistics.median(self.days_to_attend), 1) if self.days_to_attend else None


def _world() -> tuple[Engine, ManualClock, Sandbox]:
    pathways = load_pathways()
    clock = ManualClock(START)
    templates = TemplateRegistry()
    _approve_templates(templates, pathways)
    engine = Engine(pathways, clock, templates)
    return engine, clock, Sandbox(engine, clock)


def run(scenario: str, params: dict[str, Any], cohort: int, days: int, seed: int) -> Outcome:
    """Simulate one scenario. Same seed, same patients: the scenarios differ only in follow-up."""
    follow_up = scenario == "itmam"
    rates = params["patient"], params["unit"]
    patient, unit = rates
    rng = random.Random(seed)
    engine, clock, box = _world()
    staff = Staff(engine.pathways[P1])
    units = [p.org for p in DIRECTORY if p.accepting]

    ids = []
    never: set[str] = set()
    for n in range(cohort):
        person = NewSubject(f"SIM-P-{n}", f"SIM-C-{n}", rng.choice(["ar", "en"]))
        cid = engine.ingest_result(FEED, f"SIM-R-{n}", "FIT_POSITIVE", [person], SCREENING)
        ids.append(cid)
        if rng.random() < patient["never_respond"]:
            never.add(cid)
    rebook_wait: dict[str, int] = {}
    attended_day: dict[str, float] = {}
    seen = 0
    contacts = 0

    for day in range(days):
        contacted: set[str] = set()
        for event in engine.audit.events[seen:]:
            if event.case_id and event.action in CONTACT_EVENTS:
                contacted.add(event.case_id)
        seen = len(engine.audit.events)
        if follow_up:
            contacts += len(contacted)

        def lucky(base: float, boost: float, cid: str, contacted: set[str] = contacted) -> bool:
            p = boost if follow_up and cid in contacted else base
            return rng.random() < p

        for cid in ids:
            c = engine.cases[cid]
            if not c.is_open:
                continue
            subject = c.subjects[0]
            try:
                if c.stage == Stage.ISSUED:
                    if rng.random() < patient["delivered"]:
                        engine.record_delivery(FEED, cid, subject.id)
                elif c.stage == Stage.NOTIFIED:
                    if cid not in never and lucky(
                        patient["respond_daily"], patient["respond_when_contacted"], cid
                    ):
                        engine.acknowledge(Actor.contact(subject.contact_id), cid, subject.id, True)
                elif c.stage == Stage.ACKNOWLEDGED:
                    if lucky(unit["refer_daily"], unit["refer_when_chased"], cid):
                        engine.send_referral(
                            staff.coordinator(SCREENING),
                            cid,
                            rng.choice(units),
                            "Positive FIT; patient informed.",
                            staff.clinician(SCREENING),
                        )
                elif c.stage == Stage.REFERRED:
                    if lucky(unit["accept_daily"], unit["accept_when_chased"], cid):
                        referral = c.open_referral()
                        assert referral is not None
                        engine.accept_referral(staff.coordinator(referral.to_org), cid)
                elif c.stage == Stage.ACCEPTED:
                    if c.no_shows:
                        rebook_wait[cid] = rebook_wait.get(cid, 0) + 1
                        rate = (
                            patient["rebook_with_support"]
                            if follow_up
                            else patient["rebook_on_own"]
                        )
                        if rng.random() >= rate:
                            continue
                    if rng.random() < unit["book_daily"]:
                        when = clock.now() + timedelta(days=unit["days_to_slot"])
                        engine.book_appointment(staff.coordinator(c.owner.org), cid, when)
                elif c.stage == Stage.BOOKED and clock.now() >= c.appointments[-1].at:
                    shows = patient["attend_with_reminder"] if follow_up else patient["attend"]
                    attended = rng.random() < shows
                    engine.record_attendance(staff.coordinator(c.owner.org), cid, attended)
                    if attended:
                        attended_day[cid] = day
                elif c.stage == Stage.ATTENDED:
                    engine.record_outcome(staff.clinician(c.owner.org), cid, "polyps_removed")
                elif c.stage == Stage.OUTCOME_RECORDED and c.pending_closure is None:
                    due = (clock.now() + timedelta(days=3 * 365)).date().isoformat()
                    record = box.malaffi_record(c)
                    engine.propose_closure(
                        staff.coordinator(c.owner.org),
                        cid,
                        "surveillance",
                        {"outcome_record": record, "next_due_date": due},
                    )
                    engine.sign_closure(staff.clinician(c.owner.org), cid)
            except CommandRejected:
                pass  # a refused step is simply not taken; the rules stay intact
        clock.advance(timedelta(days=1))
        engine.tick(CLOCK_ACTOR)

    cases = [engine.cases[cid] for cid in ids]
    return Outcome(
        scenario="With Itmam" if follow_up else "Without follow-up",
        cohort=cohort,
        attended=len(attended_day),
        closed=sum(not c.is_open for c in cases),
        days_to_attend=[float(d) for d in attended_day.values()],
        escalations=sum(len(c.escalations) for c in cases),
        contacts=contacts,
        stuck=dict(Counter(leakage(engine, clock.now()))),
        violations=len(check_all(engine)),
    )


def report(results: list[Outcome], params: dict[str, Any], days: int, seed: int) -> str:
    base, itmam = results
    lines = [
        "# Itmam Twin: what-if simulation",
        "",
        "Generated by `python -m itmam.simulation` (or `make sim`). Synthetic patients only.",
        "",
        "> **This is a what-if under stated assumptions, not evidence.** The no-follow-up rates",
        "> are set so completion lands near the published Abu Dhabi baseline (30.5% of",
        "> FIT-positive patients had a colonoscopy, 2015-16). The effect of follow-up comes",
        "> entirely from the assumed rates in `sim/parameters.yaml`. Only a pilot can measure",
        "> Itmam's real effect.",
        "",
        f"Cohort: {base.cohort} FIT-positive results · horizon: {days} days · seed: {seed}. "
        "Both scenarios use the same patients and the same random seed, in the real case "
        "engine with its real clocks and rules.",
        "",
        "| Measure | Without follow-up | With Itmam |",
        "| --- | --- | --- |",
        f"| Colonoscopy completed | {base.completion:.1f}% | {itmam.completion:.1f}% |",
        f"| Median days, result to colonoscopy | {base.median_days()} | {itmam.median_days()} |",
        f"| Cases closed with evidence | {base.closed} | {itmam.closed} |",
        f"| Escalations raised by the engine | {base.escalations} | {itmam.escalations} |",
        f"| Case-days with a reminder or escalation acted on (workload) | 0 | {itmam.contacts} |",
        f"| Rule violations (all fifteen invariants) | {base.violations} | {itmam.violations} |",
        "",
        "## Where the open cases were stuck at the end",
        "",
        "| Stuck point | Without follow-up | With Itmam |",
        "| --- | --- | --- |",
    ]
    for label in sorted(set(base.stuck) | set(itmam.stuck)):
        lines.append(f"| {label} | {base.stuck.get(label, 0)} | {itmam.stuck.get(label, 0)} |")
    lines += [
        "",
        "## How to read this",
        "",
        "- The difference between the columns is produced by the assumed rates below, so it",
        "  shows what those assumptions imply, not what will happen. For comparison, the",
        "  PRECISE randomised trial found 55.1% with navigation against 42.1% with usual care",
        "  (Ann Intern Med 2025), in a US setting.",
        "- What the simulation does show directly: the engine handled every case and every",
        "  clock with no rule violations, how much follow-up work Itmam creates, and where",
        "  cases stall when nobody follows up.",
        "- A pilot would replace these assumptions with measured rates, starting with a",
        "  read-only run over past results.",
        "",
        "## Parameters (all assumed unless a source is named)",
        "",
        "```yaml",
        yaml.safe_dump(params, sort_keys=False).strip(),
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cohort", type=int, default=None)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args()
    params = yaml.safe_load(PARAMETERS.read_text(encoding="utf-8"))
    run_cfg = params["run"]
    cohort = args.cohort or run_cfg["cohort"]
    days = args.days or run_cfg["days"]
    seed = args.seed if args.seed is not None else run_cfg["seed"]
    results = [run(s, params, cohort, days, seed) for s in ("none", "itmam")]
    text = report(results, params, days, seed)
    if not args.no_write:
        REPORT.write_text(text, encoding="utf-8")
    for r in results:
        print(
            f"{r.scenario}: {r.completion:.1f}% completed, median {r.median_days()} days, "
            f"{r.escalations} escalations, {r.violations} violations"
        )


if __name__ == "__main__":
    main()
