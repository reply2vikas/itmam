# AI security and governance

Itmam uses AI to help people, never to replace their decisions. This page explains what each AI
part does, which controls surround it, and how they are tested. It maps the controls to public
frameworks to show the reasoning; it does not claim certification against any of them.

## Principle: propose, never act

Six agents read a case and **propose**. A person applies a proposal, and the case engine checks
all ten rules (I1–I10, see the README) before anything changes. The engine refuses every command
from the AI actor except suggestions, so an agent bug or a manipulated model cannot close,
accept, pause or reassign a case. This is enforced in code (`engine.py`) and tested on 10,000
random journeys, including the AI trying the wrong thing.

## The agents

| Agent | Reads | Proposes | Uses a model? | If it fails |
| --- | --- | --- | --- | --- |
| Chaser | Case stage, clocks, escalations, barriers | The next action and who owns it | No: decision table | Not applicable |
| Router | Provider directory, insurance, preference | One endoscopy unit, with reasons and exclusions | No: ranked rules | Escalates to a clinician |
| Navigator | Stage, language; patient's free-text reply | A message draft; barrier codes | Optional (Gemini) | Approved template; keyword rules |
| Verifier | Closure rule, evidence, outcome | Ready to sign, or the list of problems | No: checks | Not applicable |
| Guard | Every text going to or from a model | Redactions; injection flags | No: patterns | Text is kept away from the model |
| Orchestrator | The case | Which agents run, in which order; evidence score | No | Not applicable |

Clinical decisions stay deterministic and explainable: the model only rephrases approved text
and classifies replies into a closed list of barrier codes.

## Controls

| Risk | Control | Where | Tested by |
| --- | --- | --- | --- |
| Prompt injection in patient replies | Guard flags it in English and Arabic; flagged text never reaches the model; the reply is treated as data inside tags | `agents/guard.py`, `agents/navigator.py` | 6 benchmark cases; `test_injection_never_reaches_the_case` |
| Personal data sent to a model | Emirates ID, phone and email redacted; no patient names or IDs in prompts | `agents/guard.py`, `agents/navigator.py` | 5 benchmark cases |
| Harmful or alarming wording | Drafts with diagnosis words, over 320 characters or missing the next step are rejected | `agents/navigator.py` | 4 benchmark cases |
| Model output outside the allowed set | Barrier codes are filtered to the pathway's list | `agents/navigator.py` | Benchmark |
| AI acting on its own | Engine refuses AI commands other than suggestions; suggestions need a coordinator | `domain/engine.py` | Invariant I3, property test, planted-bug check |
| Decisions on thin information | Orchestrator reports an evidence score and names missing inputs | `agents/orchestrator.py` | 2 benchmark cases |
| Model unavailable or misbehaving | Every agent falls back to rules; `ITMAM_AI=off` is a kill switch | `agents/base.py` | `test_kill_switch_turns_the_model_off` |
| No record of what the AI did | Every proposal and review is logged; the audit log is hash-chained | `sandbox.py`, `domain/audit.py` | Invariant checks, audit verification |
| Unfair routing | Routing rules are explicit and visible; exclusions are listed with reasons | `agents/router.py` | 10 benchmark cases |

The demo's **AI governance** button shows these controls live, with the AI log for the session.

## Data

- The demo uses synthetic data only. No real patient data is used or stored.
- In production, text sent to a model would leave the hospital network; a UAE deployment would
  use a model hosted in the UAE, behind the same Guard. The model call is one small function
  (`agents/base.py`), so it can be swapped without touching the agents.
- Secrets (the Gemini key) live in environment variables or Secret Manager, never in the code.

## Framework mapping (reasoning, not certification)

| Framework | What it asks | Itmam's answer |
| --- | --- | --- |
| WHO guidance on ethics and governance of AI for health | Human autonomy, transparency, accountability | People decide; every proposal is explained and logged |
| NIST AI Risk Management Framework | Govern, map, measure, manage | Risk table above; benchmark; kill switch |
| OWASP Top 10 for LLM applications | Prompt injection, sensitive data disclosure, excessive agency | Guard; redaction; the engine's refusal of AI actions |
| Abu Dhabi Department of Health AI and data policies | Accountability, data protection | Needs review with the Department of Health before a pilot |

## Limits

- The Guard's patterns catch known injection phrasing; new phrasings can slip through. Because
  the AI cannot act on a case, a missed injection can at worst produce a wrong suggestion, which
  a coordinator must still approve.
- Arabic text is a draft pending native review.
- The benchmark is synthetic; a pilot would measure the agents on real, consented cases.
