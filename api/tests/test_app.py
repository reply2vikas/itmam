from fastapi.testclient import TestClient

from itmam.app import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json()["status"] == "ok"


def test_pathways_are_listed() -> None:
    ids = {p["id"] for p in client.get("/api/pathways").json()}
    assert ids == {
        "crc-positive-to-colonoscopy",
        "premarital-at-risk-to-counselling",
        "wellchild-developmental-to-assessment",
    }
