API := api
PY ?= python3

.PHONY: install lint format typecheck test demo bench deploy proptest proptest-10k proptest-100k mutation-check check schema run docker

install:            ## install the API package with test tools (Python 3.11+)
	cd $(API) && $(PY) -m pip install -e ".[dev]"

lint:               ## style and correctness checks
	cd $(API) && ruff check . && ruff format --check .

format:
	cd $(API) && ruff check --fix . && ruff format .

typecheck:
	cd $(API) && mypy

test:               ## all tests, with a quick property-based run
	cd $(API) && HYPOTHESIS_PROFILE=dev $(PY) -m pytest

demo:               ## a narrated run: two synthetic patients, one closes, one escalates
	cd $(API) && $(PY) -m itmam.demo

bench:              ## run the 67-case agent benchmark; writes docs/agent-benchmark.md
	cd $(API) && $(PY) -m itmam.agents.bench

deploy:             ## deploy to Cloud Run (one instance keeps each visitor's sandbox)
	gcloud run deploy itmam --source . --region europe-west3 --allow-unauthenticated \
		--max-instances 1 --min-instances 1 --memory 1Gi

proptest:           ## 600 random care journeys, with statistics
	cd $(API) && HYPOTHESIS_PROFILE=ci $(PY) -m pytest tests/test_invariants_property.py \
		--hypothesis-show-statistics

proptest-10k:       ## gate 1: one seeded batch of 10,000 journeys (about 10 minutes)
	cd $(API) && HYPOTHESIS_PROFILE=release ITMAM_JOURNEYS=10000 $(PY) -m pytest -p no:cacheprovider \
		tests/test_invariants_property.py --hypothesis-seed=42 --hypothesis-show-statistics

# Ten batches rather than one run: Hypothesis remembers every journey it generates within a run,
# so memory grows with run length. Batches of 10,000 keep each run to about 3 GB.
proptest-100k:      ## the evidence run: ten seeded batches of 10,000 journeys (about 100 minutes)
	cd $(API) && for seed in 1 2 3 4 5 6 7 8 9 10; do \
		HYPOTHESIS_PROFILE=release ITMAM_JOURNEYS=10000 $(PY) -m pytest -q -p no:cacheprovider \
			tests/test_invariants_property.py --hypothesis-seed=$$seed || exit 1; \
	done

mutation-check:     ## plant deliberate bugs and confirm the tests catch every one
	cd $(API) && $(PY) scripts/mutation_check.py

check:              ## validate every pathway file
	cd $(API) && $(PY) -m itmam.cli check

schema:             ## regenerate the pathway JSON Schema
	cd $(API) && $(PY) -m itmam.cli schema > schema/pathway.schema.json

run:                ## local API on http://localhost:8080
	cd $(API) && uvicorn itmam.app:app --reload --port 8080

docker:
	docker build -t itmam .
