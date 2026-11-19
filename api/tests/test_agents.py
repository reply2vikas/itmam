from fastapi.testclient import TestClient

from itmam.agents.bench import ALL, run
from itmam.agents.navigator import guard, tag_barriers
from itmam.app import app


def test_agent_benchmark_passes_every_case() -> None:
    results, _ = run(ALL)
    failed = [(case.agent, case.name, got) for case, got, ok in results if not ok]
    assert failed == []


def test_barrier_words_match_whole_words_only() -> None:
    assert tag_barriers(None, "-", "I am scared").details["codes"] == ["fear"]


def test_guard_blocks_diagnosis_language() -> None:
    assert guard("Your result shows cancer.")


def test_demo_api_walk_and_refusals() -> None:
    client = TestClient(app)
    assert client.get("/").status_code == 200
    state = client.post("/api/demo").json()
    sid = state["sid"]
    ids = {c["name"]: c["id"] for c in state["cases"]}
    state = client.post(f"/api/demo/{sid}/advance", json={"days": 5}).json()
    assert state["violations"] == [] and state["audit_chain_ok"]
    refused = client.post(f"/api/demo/{sid}/cases/{ids['Omar']}/mistake/ai_closes").json()
    assert refused["ok"] is False
    proposals = client.get(f"/api/demo/{sid}/cases/{ids['Sara']}/agents").json()["proposals"]
    chaser = next(p for p in proposals if p["agent"] == "chaser")
    assert chaser["details"]["action"] == "clinician_review"
