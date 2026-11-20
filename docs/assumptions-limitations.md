# Assumptions, dependencies and limitations

Current as of submission, 20 November 2026.

## Assumptions

- Every clock length in `api/pathways/*.yaml` is a prototype default, to be confirmed with the
  clinical advisor and set by each programme in a pilot.
- Working days are Monday to Friday in UAE time. No public holidays are configured yet.
- A result code identifies its pathway uniquely.
- Urgency comes from programme rules or a clinician; it can rise but never fall.

## Dependencies

- Python 3.11+, Pydantic, PyYAML, FastAPI, Uvicorn; Hypothesis and pytest for tests.
- Deployed demo: Google Cloud Run (one instance, in-memory state); optional Gemini API for the
  Navigator's wording, synthetic inputs only. Without a key, or with `ITMAM_AI=off`, it runs on rules.
- Planned for the pilot: a database, sign-in, scheduled clock ticks, UAE hosting.

## Limitations

- The stage template is fixed (notify → acknowledge → refer → accept → book → attend →
  outcome → close). Pathways configure it but cannot add stages yet; a confirmatory repeat
  test, for example, would need a new stage type.
- Referral happens only after acknowledgement (`refer_after: acknowledged`); programmes that
  refer automatically on result issue are not modelled yet. The referral clock still counts from
  the result (`from: result_issued`), so a slow acknowledgement does not buy extra time.
- Paused clocks resume shifted by calendar time, even when the original clock counted working
  days.
- State lives in memory; each demo session is separate and is lost on restart. Persistence is planned for the pilot.
- The template approval registry is in memory and has no approval workflow screen yet.
- The engine does not measure clinical outcomes; it measures process (time to each step,
  closure, drop-off). "Closed with evidence" means the required records and sign-offs exist,
  not that the clinical outcome was good.
- "Understood" is recorded when the patient confirms the result and next step (the
  `acknowledged` stage). A teach-back check in the patient's language is planned for the pilot.
- No clinical validation has been done. Tests show the software follows its rules; only a pilot
  can show whether Itmam improves follow-up.
- Random journeys test the rules; they do not prove them. The property-based test checks every
  invariant after every step, and the planted-bug check confirms it notices 14 known kinds of
  rule break, but a break the journey generator never reaches could go unnoticed.
- Hypothesis keeps every generated journey in memory for the length of a run, so long runs are
  split into seeded batches of 10,000.
