import json

import pytest
from fastapi.testclient import TestClient

from baton.api.app import REQUEST_ID_HEADER, create_app
from baton.config import Settings


@pytest.fixture
def client() -> TestClient:
    settings = Settings(_env_file=None, baton_llm="fake")  # type: ignore[call-arg]
    return TestClient(create_app(settings))


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["llm"] == "fake"


def test_request_id_generated(client: TestClient) -> None:
    rid = client.get("/healthz").headers[REQUEST_ID_HEADER]
    assert len(rid) == 32
    assert rid != client.get("/healthz").headers[REQUEST_ID_HEADER]


def test_request_id_propagated_when_well_formed(client: TestClient) -> None:
    resp = client.get("/healthz", headers={REQUEST_ID_HEADER: "abc-123"})
    assert resp.headers[REQUEST_ID_HEADER] == "abc-123"


def test_malformed_request_id_is_replaced(client: TestClient) -> None:
    resp = client.get("/healthz", headers={REQUEST_ID_HEADER: "bad id\nwith newline"})
    assert resp.headers[REQUEST_ID_HEADER] != "bad id\nwith newline"


def test_logs_are_json_with_request_id(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    client.get("/healthz", headers={REQUEST_ID_HEADER: "req-42"})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    event = next(e for e in lines if e.get("event") == "request")
    assert event["request_id"] == "req-42"
    assert event["path"] == "/healthz"
    assert event["status"] == 200
    assert event["level"] == "info"


def test_logged_pii_is_redacted(client: TestClient, capsys: pytest.CaptureFixture[str]) -> None:
    import structlog

    structlog.get_logger("t").info("lookup", query="ava.thompson@example.com +1 503 555 0199")
    out = capsys.readouterr().out
    assert "ava.thompson@example.com" not in out
    assert "555 0199" not in out
    assert "a***@example.com" in out
