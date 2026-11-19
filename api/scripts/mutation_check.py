"""Planted-bug check: do the tests notice when a rule is broken on purpose?

Each mutant removes or bends one rule in a temporary copy of the code, then runs the
property-based test against it. Every mutant should be caught. The unmodified code is run
first and must pass, so a "caught" result always means the test noticed the planted bug.

    python scripts/mutation_check.py            # from the api/ folder
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = Path(__file__).resolve().parents[1]
ENGINE = "itmam/domain/engine.py"
AUDIT = "itmam/domain/audit.py"
STAGE_CLOCK_CALL = (
    "        return self._arm_stage_clock(\n            case,\n            CLOCK_FOR_STAGE[stage],"
)

Mutant = tuple[str, str, str, str]  # (what the bug does, file, exact original text, replacement)

MUTANTS: list[Mutant] = [
    (
        "AI is allowed to act",
        ENGINE,
        "        if actor.kind == ActorKind.AI:\n"
        '            raise CommandRejected("ai_not_permitted"',
        '        if False:\n            raise CommandRejected("ai_not_permitted"',
    ),
    (
        "the next stage clock is never armed",
        ENGINE,
        STAGE_CLOCK_CALL,
        "        return Expectation(id='x', kind='none', created_at=self.clock.now(),\n"
        "                           breach_at=self.clock.now())\n" + STAGE_CLOCK_CALL,
    ),
    (
        "the same person can sign twice",
        ENGINE,
        "        if any(s.actor.id == actor.id for s in proposal.signatures):",
        "        if False:",
    ),
    (
        "pause length is not checked",
        ENGINE,
        "        if wake_at > rule.max.add_to(now, self.calendar):",
        "        if False:",
    ),
    (
        "any organisation can accept a referral",
        ENGINE,
        "        self._require_receiver(p, referral, actor)\n        now = self.clock.now()",
        "        now = self.clock.now()",
    ),
    (
        "closure evidence is not checked",
        ENGINE,
        '        if missing:\n            raise CommandRejected("evidence_missing"',
        '        if False:\n            raise CommandRejected("evidence_missing"',
    ),
    (
        "a refused referral still changes the case",
        ENGINE,
        '            raise CommandRejected("invalid_target", "refer to a different organisation")',
        "            case.rejections += 100\n"
        '            raise CommandRejected("invalid_target", "refer to a different organisation")',
    ),
    (
        "ownership moves when the referral is sent",
        ENGINE,
        "        case.referrals.append(referral)\n",
        "        case.referrals.append(referral)\n"
        "        case.owner = Owner(to_org, p.roles.receiver, Side.RECEIVER)\n",
    ),
    (
        "closed cases can still be changed",
        ENGINE,
        '        if not case.is_open:\n            raise CommandRejected("case_closed"',
        '        if False:\n            raise CommandRejected("case_closed"',
    ),
    (
        "results are not entered in the ledger",
        ENGINE,
        "        self.results[result_id] = case.id\n",
        "",
    ),
    (
        "clocks keep running during a pause",
        ENGINE,
        "        for exp in case.armed():\n            exp.status = SUSPENDED",
        "        for exp in []:\n            exp.status = SUSPENDED",
    ),
    (
        "a two-signature closure completes with one",
        ENGINE,
        "        if len(proposal.signatures) == len(rule.signers):",
        "        if len(proposal.signatures) >= 1:",
    ),
    (
        "urgent cases can be paused by a coordinator",
        ENGINE,
        '        clinician_needed = rule.approver == "clinician" or case.urgency == "urgent"',
        '        clinician_needed = rule.approver == "clinician"',
    ),
    (
        "audit events are not chained",
        AUDIT,
        "        prev_hash = self._events[-1].hash if self._events else GENESIS",
        "        prev_hash = GENESIS",
    ),
]


def fails(mutant: Mutant | None) -> bool:
    """Run the property test on a copy of the code with one planted bug (or none)."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "api"
        shutil.copytree(
            API,
            copy,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".pytest_cache", ".hypothesis", "*.egg-info"
            ),
        )
        if mutant is not None:
            label, relative, old, new = mutant
            target = copy / relative
            source = target.read_text(encoding="utf-8")
            if source.count(old) != 1:
                raise SystemExit(f"mutant '{label}' no longer matches the code; update it")
            target.write_text(source.replace(old, new), encoding="utf-8")
        env = {**os.environ, "HYPOTHESIS_PROFILE": "mutation", "PYTHONPATH": str(copy)}
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-x",
                "-q",
                "-p",
                "no:cacheprovider",
                "tests/test_invariants_property.py",
            ],
            cwd=copy,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode != 0


def main() -> int:
    if fails(None):
        print("The unmodified code fails the property test; fix that before planting bugs.")
        return 2
    print("baseline: the unmodified code passes", flush=True)
    missed = 0
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
        for mutant, caught in zip(MUTANTS, pool.map(fails, MUTANTS), strict=True):
            missed += not caught
            print(f"{'caught' if caught else 'MISSED':7} {mutant[0]}", flush=True)
    print(f"\n{len(MUTANTS) - missed} of {len(MUTANTS)} planted bugs caught")
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
