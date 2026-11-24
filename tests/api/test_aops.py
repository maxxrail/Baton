from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from baton.aop.compiler import AOPCompileError
from baton.api.app import create_app
from baton.config import Settings

REPO = Path(__file__).resolve().parents[2]
RETURN_ITEM = (REPO / "aops" / "return_item.md").read_text(encoding="utf-8")


@pytest.fixture
def client() -> TestClient:
    settings = Settings(_env_file=None, baton_llm="fake")  # type: ignore[call-arg]
    return TestClient(create_app(settings))


def test_list_aops(client: TestClient) -> None:
    resp = client.get("/aops")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["bundle_hash"]) == 64
    assert body["diagnostics"] == []
    ids = [a["id"] for a in body["aops"]]
    assert ids == sorted(ids)
    assert len(ids) == 8
    item = next(a for a in body["aops"] if a["id"] == "return_item")
    assert item["ref"] == "return_item@2"
    assert item["step_count"] == 6
    assert item["requires_verification"] is True


def test_get_aop_returns_graph_and_mermaid(client: TestClient) -> None:
    resp = client.get("/aops/return_item")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ref"] == "return_item@2"
    assert body["mermaid"].startswith("flowchart TD\n")
    step2 = body["aop"]["steps"][1]
    assert step2["allowed_tools"] == ["get_order"]
    assert step2["transitions"][0] == {
        "condition": "the order is not delivered",
        "target": "RESOLVE",
        "implicit": False,
    }
    assert step2["transitions"][1]["target"] == 3
    assert "### Policies" in step2["prompt"]


def test_get_unknown_aop_is_404(client: TestClient) -> None:
    resp = client.get("/aops/nope")
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


def test_validate_good_draft(client: TestClient) -> None:
    resp = client.post("/aops/validate", json={"markdown": RETURN_ITEM})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["diagnostics"] == []
    assert body["aop"]["id"] == "return_item"
    assert body["mermaid"].startswith("flowchart TD")


def test_validate_draft_with_errors(client: TestClient) -> None:
    draft = RETURN_ITEM.replace("@get_order", "@get_ordr").replace(
        "requires_verification: true", "requires_verification: false"
    )
    body = client.post("/aops/validate", json={"markdown": draft}).json()
    assert body["ok"] is False
    assert body["aop"] is None
    assert body["mermaid"] is not None  # still drawable: it parsed
    codes = {d["code"] for d in body["diagnostics"]}
    assert codes == {"UNKNOWN_TOOL", "SIDE_EFFECT_WITHOUT_VERIFICATION"}
    assert all(d["level"] == "error" for d in body["diagnostics"])


def test_validate_unparseable_draft(client: TestClient) -> None:
    body = client.post("/aops/validate", json={"markdown": "just some text"}).json()
    assert body["ok"] is False
    assert body["mermaid"] is None
    assert [d["code"] for d in body["diagnostics"]] == ["PARSE_ERROR"]


def test_validate_rejects_oversized_or_missing_body(client: TestClient) -> None:
    assert client.post("/aops/validate", json={"markdown": "x" * 100_001}).status_code == 422
    assert client.post("/aops/validate", json={}).status_code == 422


def test_app_refuses_to_start_with_broken_aops(tmp_path: Path) -> None:
    (tmp_path / "broken.md").write_text(
        "---\nid: broken\nname: B\nversion: 1\ntriggers: [x]\n---\n1. Use @nope. RESOLVE.\n"
    )
    settings = Settings(_env_file=None, baton_llm="fake", aops_dir=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(AOPCompileError, match="UNKNOWN_TOOL"):
        create_app(settings)
