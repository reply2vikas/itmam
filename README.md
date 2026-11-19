# Itmam (إتمام)

**Every actionable screening result, owned until care is complete.**

Itmam is a prototype for the Future Health Digital Innovation Challenge, Statement 05: *Closing
the Screening-to-Care Loop*. Every actionable screening result becomes a case with exactly one
accountable owner and a clock on the next expected event. When something that should happen
does not — a message unopened, a referral not accepted, an appointment missed — Itmam notices
the silence and escalates to qualified staff. Ownership moves to another organisation only when
that organisation accepts the referral, and a case closes only with evidence or two authorised
signatures.

> **All data in this repository is synthetic.** No real person's data is used anywhere.
> Synthetic identifiers start with `TEST-`; organisations are fictional.

## Status

Week 1 of 5 — the case engine, the pathway format and the rule tests.

| Week | Scope |
| --- | --- |
| 1 | Case engine, pathway configuration, invariants, property-based tests ✅ |
| 2 | API, persistence, synthetic cohort, coordinator and clinician screens |
| 3 | Patient journey page (Arabic and English), AI barrier tagging and hand-over drafts |
| 4 | Simulation, AI evaluation, load, accessibility and security checks |
| 5 | Documentation, deck, demo video |

## Try it

Open the live demo, or run it yourself with `make run` and open http://localhost:8080. Each
visitor gets a private sandbox with four synthetic patients and a demo clock:

| Patient | What the demo shows |
| --- | --- |
| Mariam (Arabic, Thiqa) | The happy path, step by step, to a surveillance closure |
| Omar (English, Daman Basic) | Silence: automatic reminders, then escalation L1 → L2 → L3 |
| Sara (Arabic, urgent, prefers a female endoscopist) | Urgency first; routing that respects her preference |
| Rashid (English, Thiqa) | A missed appointment, barriers, and rebooking with support |

"Try a mistake" makes the AI, the wrong unit or the wrong person attempt an unsafe action; the
engine refuses each one and the refusal changes nothing.

## Agents

Six agents read a case and **propose**; none of them can change it. A person applies a proposal,
and the case engine still checks all ten rules.

| Agent | Job | Guardrails |
| --- | --- | --- |
| Orchestrator | Runs the right agents for the case, in order; reports an evidence score and what is missing | Decides nothing clinical; logs every proposal |
| Chaser | What should happen next on a late or stuck case, and who does it | Urgent cases always go to a clinician first |
| Router | Which endoscopy unit to refer to | Insurance network, accepting referrals, no unit that already rejected, female-endoscopist preference, urgent slot within 7 days |
| Navigator | Drafts the patient message; tags barriers in free-text replies (English and Arabic) | Approved templates; no names sent to the model; drafts checked for length and diagnosis words; tags are suggestions until a coordinator approves |
| Verifier | Checks a closure before anyone signs | Placeholder evidence, due-date range, closure type that contradicts the outcome |
| Guard | AI security for everything sent to or from a model | Prompt-injection screen (English and Arabic); Emirates ID, phone and email redaction |

**AI security and governance:** see [docs/ai-governance.md](docs/ai-governance.md). In the demo,
the **AI governance** button shows the controls and the session's AI log. `ITMAM_AI=off` is a
kill switch that runs every agent on rules only.

**Testing:** 90 automated tests, a 67-case agent benchmark (24 safety-critical cases;
[docs/agent-benchmark.md](docs/agent-benchmark.md)), 10,000 seeded random care journeys with no
rule broken, and 14 of 14 planted bugs caught. With `GEMINI_API_KEY` set (and optionally
`GEMINI_MODEL`), the Navigator uses Gemini and falls back to its rules on any failure or
guardrail breach; `python -m itmam.agents.bench --live` benchmarks that mode.

## Quick start

```bash
make install        # Python 3.11+
make test           # every test, with a quick random-journey run
make bench          # the 67-case agent benchmark
make deploy         # Cloud Run, one instance
make demo           # a narrated run: one patient reaches closure, one goes silent and escalates
make proptest       # 600 random care journeys, with statistics on how deep they go
make check          # validate the pathway files
make run            # API on http://localhost:8080 (/healthz, /api/pathways)
```

## How it works

- **Pathways are configuration.** Each file in [`api/pathways/`](api/pathways) sets the clocks,
  owners, closure rules, pause rules and barrier codes for one pathway. The three prototype
  pathways are colorectal screening → colonoscopy, premarital at-risk couple → genetic
  counselling, and Well-Child developmental flag → specialist assessment. A JSON Schema
  ([`api/schema/pathway.schema.json`](api/schema/pathway.schema.json)) and a linter reject
  unsafe configurations at startup.
- **The engine is pure Python with no I/O** ([`api/itmam/domain/engine.py`](api/itmam/domain/engine.py)).
  Every command checks all its guards before changing anything, so a refused command leaves no
  trace beyond a logged refusal. Time comes from an injected clock, so tests and demo sandboxes
  can move it forward.
- **Silence is the alarm.** Each stage arms an expectation with warning and breach times. The
  clock tick warns, then escalates: coordinator queue (L1), supervising clinician (L2),
  programme manager (L3). Pauses need a reason, an approver and an automatic wake-up.
- **AI suggests, people decide.** The AI actor can only file suggestions; every state-changing
  command from it is refused.

More detail: [`docs/engine.md`](docs/engine.md). Known limits:
[`docs/assumptions-limitations.md`](docs/assumptions-limitations.md).

## The ten invariants

Checked after every step of every randomly generated journey ([`api/itmam/domain/invariants.py`](api/itmam/domain/invariants.py)).

| ID | Rule |
| --- | --- |
| I1 | Every open case has exactly one accountable owner, on the correct side of the hand-off |
| I2 | Every open, unpaused case has at least one armed clock |
| I3 | Every closed case carries its closure type's evidence and its required, distinct signers |
| I4 | Ownership changes only by acceptance or same-organisation reassignment |
| I5 | Every pause has a valid reason, a permitted approver and a bounded wake-up time, and stops the clocks |
| I6 | The AI actor never changes a case |
| I7 | The audit log's hash chain verifies, and every change points at its audit event |
| I8 | Every actionable result maps to exactly one case: received = open + closed |
| I9 | A closed case never changes |
| I10 | Patients only ever receive clinically approved template versions |

The property-based test ([`api/tests/test_invariants_property.py`](api/tests/test_invariants_property.py))
generates random care journeys across all three pathways — right and wrong people, the AI
actor, known mistakes at every stage, clock jumps from a minute to four months — and checks all
ten invariants after every step. It also asserts that every refused command changes nothing.

```bash
make proptest-10k    # one seeded batch of 10,000 journeys, about 10 minutes
make proptest-100k   # ten seeded batches of 10,000, about 100 minutes; or the "evidence" workflow
make mutation-check  # plants 14 deliberate bugs and confirms the tests catch every one
```

### Results so far

| Check | Result | Date |
| --- | --- | --- |
| 10,000 seeded journeys (`make proptest-10k`, seed 42) | Passed: 0 invariant violations (9 min 41 s, peak memory 3 GB) | 18 Oct 2026 |
| 14 planted bugs (`make mutation-check`) | 14 of 14 caught, in each of three runs | 18 Oct 2026 |

In the 10,000-journey run, 45% of journeys closed at least one case, 27% reached a recorded
outcome, 20% escalated a case to L3 and 19% paused and resumed one; every closure type occurred.
The 100,000-journey evidence quoted in the submission is run on the final code, as ten seeded
batches of 10,000.

## Repository layout

```text
api/        engine (itmam/domain), pathway files, JSON Schema, tests, FastAPI app
web/        React app (week 2)
sim/        discrete-event simulation (week 4)
eval/       AI evaluation sets and runner (week 4)
docs/       engine guide, assumptions and limitations
```

## Licence

Apache-2.0 — see [LICENSE](LICENSE). Copyright 2026 Vikas Kumar Sharma.

## Mobile

Itmam installs on a phone like an app, with no store download:

- **Android (Chrome):** open the demo link, tap the menu (⋮), then **Install app** or **Add to Home screen**.
- **iPhone (Safari):** open the demo link, tap **Share**, then **Add to Home Screen**.

It opens full-screen with its own icon. Native Google Play and App Store apps are planned for the
pilot phase, after Department of Health review; store links will be added here when published.
