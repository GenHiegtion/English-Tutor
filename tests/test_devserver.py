import json

import pytest
from fastapi.testclient import TestClient

from ea_llm.adapters.memory import (
    MemoryHistoryStore,
    MemoryLearnerStore,
    MemoryPersonaStore,
)
from ea_llm.devserver import create_app
from ea_llm.teacher.runner import TeacherService
from ea_llm.testing import FakeProvider, reply


@pytest.fixture
def client():
    learners = MemoryLearnerStore()
    service = TeacherService(
        primary=FakeProvider([reply("<en>Nice to meet you, friend.</en>")]),
        history=MemoryHistoryStore(),
        personas=MemoryPersonaStore.from_seed(),
        learners=learners,
    )
    return TestClient(create_app(service, learners))


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in filter(None, text.split("\n\n")):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def test_docs_and_schema_are_published(client):
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    assert "/sessions/{session_id}/turns" in spec["paths"]
    schema = client.get("/schema/events").json()
    assert "oneOf" in schema or "anyOf" in schema


def test_catalog_endpoints(client):
    assert {p["id"] for p in client.get("/personas").json()} == {"lan", "minh", "hung", "chi"}
    assert "cafe-order" in {s["id"] for s in client.get("/scenarios").json()}


def test_full_flow_opener_then_turn_then_history(client):
    created = client.post("/sessions", json={"persona_id": "chi", "age_band": "under_18"})
    assert created.status_code == 201
    session = created.json()
    assert session["address"] == {"teacher": "chị", "learner": "em"}

    sid = session["session_id"]
    opener = parse_sse(client.post(f"/sessions/{sid}/turns", json={}).text)
    assert [k for k, _ in opener] == ["segment", "segment", "terminal", "turn_end"]

    turn = parse_sse(client.post(f"/sessions/{sid}/turns", json={"text": "Hello"}).text)
    assert [k for k, _ in turn][-1] == "turn_end"
    assert turn[0][1]["text"] == "Nice to meet you, friend."

    history = client.get(f"/sessions/{sid}/messages").json()
    assert [m["message"]["role"] for m in history] == ["user", "assistant", "user", "assistant"]
    assert client.post(f"/sessions/{sid}/turns/x/delivered", json={"seq": 0}).status_code == 204


def test_errors(client):
    assert client.post("/sessions", json={"scenario_id": "nope"}).status_code == 404
    assert client.post("/sessions", json={"persona_id": "ghost"}).status_code == 422
    assert client.post("/sessions/none/turns", json={"text": "hi"}).status_code == 404
    assert client.get("/sessions/none/messages").status_code == 404


def test_free_chat_session_needs_learner_text_first(client):
    created = client.post("/sessions", json={"mode": "free_chat", "scenario_id": "ignored"})
    assert created.status_code == 201
    session = created.json()
    assert session["mode"] == "free_chat"
    assert session["scenario"] is None
    assert session["prompt_version"].startswith("teacher.text.free_chat")

    sid = session["session_id"]
    opener = parse_sse(client.post(f"/sessions/{sid}/turns", json={}).text)
    assert opener == [("turn_failed", opener[0][1])]
    assert opener[0][1]["reason"] == "invalid_context"

    turn = parse_sse(client.post(f"/sessions/{sid}/turns", json={"text": "I like tea"}).text)
    assert turn[0][1]["text"] == "Nice to meet you, friend."
    assert turn[-1][0] == "turn_end"


def test_unknown_scenario_is_404(client):
    assert client.post("/sessions", json={"scenario_id": "nope"}).status_code == 404
