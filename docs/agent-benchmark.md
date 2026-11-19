# Agent benchmark

Mode: rules only. Every case builds a real engine state in a fresh synthetic sandbox.
Regenerate with `make bench`.

| Agent | Cases | Passed | Safety-critical passed |
| --- | --- | --- | --- |
| Chaser | 12 | 12 | 3 of 3 |
| Router | 10 | 10 | 4 of 4 |
| Verifier | 10 | 10 | 3 of 3 |
| Navigator | 12 | 12 | 2 of 2 |
| **All** | 44 | 44 | 12 of 12 |

## Every case

| Agent | Case | Expected | Got | Pass |
| --- | --- | --- | --- | --- |
| chaser | delivered, day 1, no reply yet | wait | wait | yes |
| chaser | silent for a week | phone_call | phone_call | yes |
| chaser | silent past the care deadline | programme_review | programme_review | yes |
| chaser | urgent, acknowledged (safety) | clinician_review | clinician_review | yes |
| chaser | urgent, referred (safety) | clinician_review | clinician_review | yes |
| chaser | urgent, booked (safety) | clinician_review | clinician_review | yes |
| chaser | acknowledged, not referred | send_referral | send_referral | yes |
| chaser | referral unanswered for 8 days | chase_receiver | chase_receiver | yes |
| chaser | missed appointment | rebook_with_support | rebook_with_support | yes |
| chaser | booked, waiting | reminder | reminder | yes |
| chaser | paused while travelling | none | none | yes |
| chaser | closed case | none | none | yes |
| router | Thiqa, no preference | Endoscopy Unit C | Endoscopy Unit C | yes |
| router | Thiqa, closed unit skipped (E is fastest) | Endoscopy Unit C | Endoscopy Unit C | yes |
| router | Thiqa, wants a female endoscopist (safety) | Endoscopy Unit B | Endoscopy Unit B | yes |
| router | Daman Basic, no preference | Endoscopy Unit C | Endoscopy Unit C | yes |
| router | Daman Basic, wants a female endoscopist (safety) | Endoscopy Unit F | Endoscopy Unit F | yes |
| router | Daman Enhanced, Unit C rejected the case | Endoscopy Unit B | Endoscopy Unit B | yes |
| router | Other insurance | Endoscopy Unit D | Endoscopy Unit D | yes |
| router | Other insurance, female, D rejected (safety) | None | None | yes |
| router | Uninsured network name (safety) | None | None | yes |
| router | Thiqa, B and C rejected | None | None | yes |
| verifier | surveillance, full evidence | True | True | yes |
| verifier | care completed, full evidence | True | True | yes |
| verifier | outcome record says TBD | False | False | yes |
| verifier | next step missing | False | False | yes |
| verifier | due date next week | False | False | yes |
| verifier | due date not a date | False | False | yes |
| verifier | incomplete colonoscopy closed as completed (safety) | False | False | yes |
| verifier | suspected cancer closed to surveillance (safety) | False | False | yes |
| verifier | unreachable with no contact attempts (safety) | False | False | yes |
| verifier | made-up closure type | False | False | yes |
| navigator | no car | ['transport'] | ['transport'] | yes |
| navigator | scared | ['fear'] | ['fear'] | yes |
| navigator | work and kids | ['work_or_childcare'] | ['work_or_childcare'] | yes |
| navigator | done abroad | ['done_elsewhere'] | ['done_elsewhere'] | yes |
| navigator | insurance cost | ['cost_or_insurance'] | ['cost_or_insurance'] | yes |
| navigator | Arabic: afraid | ['fear'] | ['fear'] | yes |
| navigator | Arabic: husband decides | ['family_discussion'] | ['family_discussion'] | yes |
| navigator | no barrier | [] | [] | yes |
| navigator | draft with diagnosis words is blocked (safety) | True | True | yes |
| navigator | draft that drops the next step is blocked (safety) | True | True | yes |
| navigator | over-long draft is blocked | True | True | yes |
| navigator | approved template passes | [] | [] | yes |
