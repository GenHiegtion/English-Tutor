import asyncio
import json
import time

import jwt
import pytest
from fastapi.testclient import TestClient

import ea_llm.api.app as app_module
from ea_llm.adapters.memory import (
    MemoryHistoryStore,
    MemoryLearnerStore,
    MemoryPersonaStore,
    MemoryScenarioStore,
)
from ea_llm.api.app import create_app, dev_app
from ea_llm.api.settings import ApiSettings
from ea_llm.models import LearnerProfile
from ea_llm.teacher.runner import TeacherService
from ea_llm.testing import FakeProvider, reply

TALKWISE_KEY = "k" * 40
PIPECAT_KEY = "p" * 40
SECRET = "s" * 40
LEARNER = "3f2b8c1e-0000-4000-8000-000000000001"
OTHER_LEARNER = "3f2b8c1e-0000-4000-8000-000000000002"
SERVICE = {"X-API-Key": TALKWISE_KEY}
CAFE = {"persona_id": "lan", "scenario_id": "cafe-order"}


def make_app(*, learners=None, require_known_learner=False, **settings):
    service = TeacherService(
        primary=FakeProvider([reply("<en>Nice to meet you, friend.</en>")] * 5),
        history=MemoryHistoryStore(),
        personas=MemoryPersonaStore.from_seed(),
        learners=learners or MemoryLearnerStore(),
    )
    config = ApiSettings(
        api_keys={"talkwise": TALKWISE_KEY, "pipecat": PIPECAT_KEY},
        token_secret=SECRET,
        **settings,
    )
    return create_app(
        service,
        MemoryScenarioStore.from_seed(),
        config,
        require_known_learner=require_known_learner,
    )


@pytest.fixture
def client():
    with TestClient(make_app()) as test_client:
        yield test_client


def token(user_id=LEARNER, *, lifetime=600, secret=SECRET, **claims):
    now = int(time.time())
    payload = {"sub": user_id, "aud": "ea-llm", "iat": now, "exp": now + lifetime, **claims}
    return {"Authorization": f"Bearer {jwt.encode(payload, secret, algorithm='HS256')}"}


def parse_sse(text):
    events = []
    for block in filter(None, text.split("\n\n")):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def open_session(client, headers=SERVICE, **body):
    payload = {**CAFE, **body}
    if headers is SERVICE:
        payload.setdefault("user_id", LEARNER)
    response = client.post("/sessions", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def test_health_and_published_docs(client):
    assert client.get("/health").json() == {"status": "ok", "database": "not_configured"}
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/sessions/{session_id}/turns" in paths
    assert "/sessions/{session_id}/end" in paths
    schema = client.get("/schema/events").json()
    assert "oneOf" in schema or "anyOf" in schema


def test_docs_can_be_switched_off():
    with TestClient(make_app(docs_enabled=False)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-API-Key": "wrong"},
        {"Authorization": "Bearer not-a-jwt"},
    ],
)
def test_requests_without_valid_credentials_get_401(client, headers):
    response = client.get("/personas", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        (token(lifetime=-60), "token expired"),
        (token(secret="x" * 40), "invalid token"),
        (token(lifetime=7200), "token lifetime is too long"),
    ],
)
def test_bad_learner_tokens_are_rejected(client, headers, message):
    response = client.get("/personas", headers=headers)
    assert response.status_code == 401
    assert response.json()["error"]["message"] == message


def test_wrong_audience_is_rejected(client):
    response = client.get("/personas", headers=token(aud="someone-else"))
    assert response.status_code == 401


def test_catalog_lists_wrapped_items(client):
    personas = client.get("/personas", headers=SERVICE).json()["items"]
    assert {p["id"] for p in personas} == {"lan", "minh", "hung", "chi"}
    assert set(personas[0]) == {"id", "name", "gender", "age_band"}
    scenarios = client.get("/scenarios", headers={"X-API-Key": PIPECAT_KEY}).json()["items"]
    assert "cafe-order" in {s["id"] for s in scenarios}


def test_service_flow_opener_turn_history_and_end(client):
    sid = open_session(
        client, persona_id="chi", learner={"age_band": "under_18", "display_name": "Na"}
    )
    opener = parse_sse(client.post(f"/sessions/{sid}/turns", json={}, headers=SERVICE).text)
    assert [kind for kind, _ in opener] == ["segment", "segment", "terminal", "turn_end"]

    turn = client.post(
        f"/sessions/{sid}/turns", json={"text": "Hello", "turn_id": "t-1"}, headers=SERVICE
    )
    assert turn.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(turn.text)
    assert events[0][1]["text"] == "Nice to meet you, friend."
    assert events[-1][0] == "turn_end"

    items = client.get(f"/sessions/{sid}/messages", headers=SERVICE).json()["items"]
    assert [m["role"] for m in items] == ["assistant", "user", "assistant"]  # no start note
    assert items[1] == {
        "turn_id": "t-1",
        "role": "user",
        "persona_id": None,
        "text": "Hello",
        "segments": [],
    }
    assert items[2]["segments"] == [{"lang": "en", "text": "Nice to meet you, friend."}]

    assert client.post(f"/sessions/{sid}/end", headers=SERVICE).json() == {
        "session_id": sid,
        "status": "done",
    }
    late = client.post(f"/sessions/{sid}/turns", json={"text": "Bye"}, headers=SERVICE)
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "session_closed"


def test_session_snapshot_reflects_profile_override(client):
    response = client.post(
        "/sessions",
        json={**CAFE, "persona_id": "chi", "user_id": LEARNER, "learner": {"cefr": "B2"}},
        headers=SERVICE,
    )
    body = response.json()
    assert body["learner"] == {"cefr": "B2", "age_band": "unknown", "display_name": None}
    assert body["channel"] == "text" and body["mode"] == "scenario"
    assert body["scenario"]["id"] == "cafe-order"


def test_service_must_name_the_user(client):
    response = client.post("/sessions", json=CAFE, headers=SERVICE)
    assert response.status_code == 422
    assert response.json()["error"]["message"] == "user_id is required with an API key"


def test_learner_token_opens_and_uses_own_session(client):
    headers = token()
    sid = open_session(client, headers=headers)
    turn = client.post(f"/sessions/{sid}/turns", json={}, headers=headers)
    assert parse_sse(turn.text)[-1][0] == "turn_end"
    assert client.get(f"/sessions/{sid}/messages", headers=headers).status_code == 200


def test_learner_cannot_reach_someone_elses_session(client):
    sid = open_session(client, headers=token(OTHER_LEARNER))
    for response in (
        client.get(f"/sessions/{sid}/messages", headers=token()),
        client.post(f"/sessions/{sid}/turns", json={"text": "hi"}, headers=token()),
        client.post(f"/sessions/{sid}/end", headers=token()),
        client.post(f"/sessions/{sid}/turns/t/delivered", json={"seq": 0}, headers=token()),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "session_not_found"


def test_session_scoped_token_reaches_only_that_session(client):
    mine = open_session(client)
    other = open_session(client)
    scoped = token(sid=mine)
    assert client.get(f"/sessions/{mine}/messages", headers=scoped).status_code == 200
    assert client.get(f"/sessions/{other}/messages", headers=scoped).status_code == 404
    response = client.post("/sessions", json=CAFE, headers=scoped)
    assert response.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {**CAFE, "learner": {"cefr": "C2"}},
        {**CAFE, "user_id": OTHER_LEARNER},
    ],
)
def test_learner_cannot_override_profile_or_user(client, body):
    response = client.post("/sessions", json=body, headers=token())
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"persona_id": "lan", "user_id": LEARNER}, 422, "invalid_request"),
        ({**CAFE, "mode": "free_chat", "user_id": LEARNER}, 422, "invalid_request"),
        ({**CAFE, "scenario_id": "nope", "user_id": LEARNER}, 404, "scenario_not_found"),
        ({**CAFE, "persona_id": "ghost", "user_id": LEARNER}, 404, "persona_not_found"),
        ({**CAFE, "persona_id": "bad id!", "user_id": LEARNER}, 422, "invalid_request"),
    ],
)
def test_create_session_errors(client, body, status, code):
    response = client.post("/sessions", json=body, headers=SERVICE)
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    if code == "invalid_request":
        assert error["details"] and "input" not in error["details"][0]


def test_turn_errors(client):
    assert (
        client.post("/sessions/none/turns", json={"text": "hi"}, headers=SERVICE).status_code == 404
    )
    sid = open_session(client)
    empty = client.post(f"/sessions/{sid}/turns", json={"text": ""}, headers=SERVICE)
    assert empty.status_code == 422
    too_long = client.post(f"/sessions/{sid}/turns", json={"text": "a" * 4001}, headers=SERVICE)
    assert too_long.status_code == 422


def test_free_chat_needs_learner_text_first(client):
    sid = open_session(client, mode="free_chat", scenario_id=None)
    events = parse_sse(client.post(f"/sessions/{sid}/turns", json={}, headers=SERVICE).text)
    assert events == [("turn_failed", events[0][1])]
    assert events[0][1]["reason"] == "invalid_context"


def test_delivered_for_a_finished_turn_is_accepted_and_ignored(client):
    sid = open_session(client)
    response = client.post(
        f"/sessions/{sid}/turns/unknown/delivered", json={"seq": 3}, headers=SERVICE
    )
    assert response.status_code == 204
    assert client.app.state.ea.service._delivered == {}


def test_turns_are_rate_limited_per_learner():
    with TestClient(make_app(turns_per_minute=1)) as client:
        sid = open_session(client)
        assert client.post(f"/sessions/{sid}/turns", json={}, headers=SERVICE).status_code == 200
        limited = client.post(f"/sessions/{sid}/turns", json={"text": "hi"}, headers=SERVICE)
        assert limited.status_code == 429
        assert limited.json()["error"]["code"] == "rate_limited"
        assert int(limited.headers["Retry-After"]) >= 1


def test_sessions_are_rate_limited_per_learner():
    with TestClient(make_app(sessions_per_hour=1)) as client:
        open_session(client)
        response = client.post("/sessions", json={**CAFE, "user_id": LEARNER}, headers=SERVICE)
        assert response.status_code == 429


def test_unknown_user_is_rejected_when_learners_must_exist():
    learners = MemoryLearnerStore([LearnerProfile(user_id=LEARNER, cefr="B1")])
    with TestClient(make_app(learners=learners, require_known_learner=True)) as client:
        assert open_session(client)
        response = client.post(
            "/sessions", json={**CAFE, "user_id": OTHER_LEARNER}, headers=SERVICE
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "user_not_found"


def test_cors_preflight_for_configured_origin():
    with TestClient(make_app(cors_origins=["https://talkwise.example"])) as client:
        response = client.options(
            "/sessions",
            headers={
                "Origin": "https://talkwise.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://talkwise.example"


def test_unknown_route_uses_the_error_shape(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not Found"}}


def test_dev_app_needs_no_credentials():
    with TestClient(dev_app(offline=True)) as client:
        sid = open_session(client, headers={}, user_id="dev-user")
        events = parse_sse(client.post(f"/sessions/{sid}/turns", json={}, headers={}).text)
        assert events[-1][0] == "turn_end"


async def test_slow_database_ping_keeps_running_after_health_gives_up(monkeypatch):
    calls = []

    async def slow_ping(engine):
        calls.append(engine)
        if len(calls) == 1:
            await asyncio.sleep(0.2)  # the first connection to a cold database; later ones reuse it

    monkeypatch.setattr(app_module, "ping", slow_ping)
    probe = app_module.DatabaseProbe(engine="engine", timeout_s=0.05)

    with pytest.raises(TimeoutError):
        await probe.check()
    with pytest.raises(TimeoutError):
        await probe.check()  # joins the same ping instead of starting over
    await asyncio.sleep(0.25)
    await probe.check()  # the abandoned ping finished and warmed the pool

    assert len(calls) == 2
    await probe.close()


async def test_database_errors_reach_the_health_check(monkeypatch):
    async def broken_ping(engine):
        raise OSError("connection refused")

    monkeypatch.setattr(app_module, "ping", broken_ping)
    probe = app_module.DatabaseProbe(engine="engine", timeout_s=1.0)
    with pytest.raises(OSError):
        await probe.check()
