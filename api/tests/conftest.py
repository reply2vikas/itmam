import os

from hypothesis import HealthCheck, Phase, settings

# dev: quick local runs; ci: every push; release: the 100,000-journey evidence run.
settings.register_profile(
    "dev",
    max_examples=150,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=600,
    stateful_step_count=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "release",
    max_examples=int(os.environ.get("ITMAM_JOURNEYS", "100000")),
    stateful_step_count=50,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)
# mutation: checks the tests catch deliberately planted bugs. Derandomised, so every run of the
# check tries the same journeys; no shrinking, so it is fast.
settings.register_profile(
    "mutation",
    max_examples=500,
    stateful_step_count=50,
    deadline=None,
    database=None,
    derandomize=True,
    phases=[Phase.generate],
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))
