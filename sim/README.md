# Itmam Twin (simulation)

A what-if simulation of a FIT-positive cohort, run through the real case engine with its real
clocks, escalations and fifteen rules. Code: [`api/itmam/simulation.py`](../api/itmam/simulation.py).
Parameters: [`parameters.yaml`](parameters.yaml). Latest results: [`docs/simulation.md`](../docs/simulation.md).

```bash
make sim            # or: cd api && python -m itmam.simulation --cohort 1000 --days 365
```

Two scenarios share the same synthetic patients and random seed: without follow-up (the engine
raises reminders and escalations, but nobody acts on them) and with Itmam (each one leads to a
contact). The no-follow-up rates are set so completion lands near the published Abu Dhabi
baseline of 30.5%. **Every other rate is an assumption; the result is a what-if, not evidence.**
A pilot would replace the assumptions with measured rates.
