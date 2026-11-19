# Assumptions, dependencies and limitations

Kept up to date during the build; the final version goes into the submission.

## Assumptions

- Every clock length in `api/pathways/*.yaml` is a prototype default, to be confirmed with the
  clinical advisor and set by each programme in a pilot.
- Working days are Monday to Friday in UAE time. No public holidays are configured yet.
- A result code identifies its pathway uniquely.
- Urgency comes from programme rules or a clinician; it can rise but never fall.

## Dependencies

- Python 3.11+, Pydantic, PyYAML, FastAPI, Uvicorn; Hypothesis and pytest for tests.
- Week 2 onward: Postgres (Neon free tier), Google Cloud Run, Cloud Scheduler.
- Week 3 onward: Gemini API on the free tier, synthetic inputs only.

## Limitations (engine, week 1)

- The stage template is fixed (notify → acknowledge → refer → accept → book → attend →
  outcome → close). Pathways configure it but cannot add stages yet; a confirmatory repeat
  test, for example, would need a new stage type.
- Referral happens only after acknowledgement (`refer_after: acknowledged`); programmes that
  refer automatically on result issue are not modelled yet.
- Paused clocks resume shifted by calendar time, even when the original clock counted working
  days.
- State lives in memory; persistence arrives in week 2.
- The template approval registry is in memory and has no approval workflow screen yet.
- The engine does not measure clinical outcomes; it measures process (time to each step,
  closure, drop-off).
- Random journeys test the rules; they do not prove them. The property-based test checks every
  invariant after every step, and the planted-bug check confirms it notices 14 known kinds of
  rule break, but a break the journey generator never reaches could go unnoticed.
- Hypothesis keeps every generated journey in memory for the length of a run, so long runs are
  split into seeded batches of 10,000.
