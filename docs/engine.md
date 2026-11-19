# Engine guide

The engine (`api/itmam/domain/`) holds every rule about who may do what, when, and what
happens when nothing happens. It has no network or database code: adapters for storage,
messaging and AI wrap it.

## Design rules

1. **Validate, then apply.** Each command checks all of its guards before changing state. A
   refused command raises `CommandRejected(code, message)`, is logged in `engine.rejections`,
   and changes nothing else. The property-based test asserts this on every refusal.
2. **Injected time.** The engine reads time only from its `Clock`. Tests and per-visitor demo
   sandboxes use `ManualClock`; production uses `SystemClock` plus a scheduler calling `tick`.
3. **Configuration, not code.** Pathways set clocks, roles, closures and pauses; the stage
   template is fixed.
4. **AI has no authority.** `_no_ai` is the first guard of every state-changing command.

## Stages and commands

| Command | From → to | Who |
| --- | --- | --- |
| `ingest_result` | — → issued | System feed or staff upload; idempotent on result ID |
| `record_delivery` | issued → notified (once every subject is notified) | Channel adapter (system) |
| `acknowledge` | notified → acknowledged (once every subject understood) | The subject's own link, or owning staff after a call |
| `request_econsult` / `answer_econsult` | acknowledged, owner unchanged | Sending clinician / receiving clinician |
| `send_referral` | acknowledged → referred | Owning staff; hand-over note approved by a person |
| `accept_referral` | referred → accepted, **ownership moves** | Receiving organisation |
| `reject_referral` | referred → acknowledged, back to the sender with a reason | Receiving organisation |
| `book_appointment` | accepted → booked | Receiving staff; joint sessions need every partner's consent |
| `record_attendance` | booked → attended, or back to accepted on a no-show | Receiving staff |
| `record_outcome` | attended → outcome recorded | Receiving clinician |
| `propose_closure` / `sign_closure` / `withdraw_closure` | any open stage → closed | Owning staff; signers per closure type |
| `pause` / `resume` | clocks stop and restart | Approver per reason; clinician for urgent cases |
| `reassign` | owner changes queue, same organisation | Supervisor |
| `request_callback` / `complete_callback` | own clock | Patient / owning staff |
| `raise_urgency` | standard → urgent, never back | Owning clinician |
| `suggest_barriers` / `review_suggestion` | no state change until a person accepts | AI / owning staff |
| `tick` | warnings, retries, escalations, wake-ups, watches | System clock |

## Clocks

Every expectation has a kind (`stage:notify`, `care_clock`, `callback`, `econsult_reply`,
`call_task`, `closure_signoff`, `pause_wake`), a warning time, a breach time and a level.

- **Stage clocks** run from stage start, from the result, or (attend) from the appointment.
- **Care clock**: from the result to the attend stage; breach at the target, L3 at the hard limit.
- **Escalation**: first breach → L1 (L2 if urgent); then L2, then L3 every `escalation` step.
  Long gaps are summarised in one audit event.
- **Retries** (patient-facing stages): reminders on a channel, then a phone task. If no approved
  reminder template exists, a person is asked to call instead.
- **Pauses** suspend every armed clock and shift it by the paused time on wake-up.

## Closure

| Type | Evidence (P1) | Signers |
| --- | --- | --- |
| care_completed | outcome record, next step | receiving clinician |
| surveillance | outcome record, next due date; creates a watch | receiving clinician |
| informed_decline | counselling note, decision record | two different clinicians |
| transferred_out | receiving organisation's acceptance | coordinator |
| not_actionable | clinical review note | two different clinicians |
| unreachable | contact log, letter; at least 6 attempts | clinician and supervisor |

A watch opens a new linked case if the next check is not recorded within the grace period;
the closed case never reopens.

## Adding a pathway

1. Copy a file in `api/pathways/` and change `id`, `trigger.result_codes` (unique across
   pathways), roles, clocks and closures.
2. Run `make check`. The linter refuses unsafe settings, for example a receiver-owned stage
   before acceptance, a warning after its breach, or a failure closure with no contact protocol.
3. Add the pathway's message templates to the approval registry.
