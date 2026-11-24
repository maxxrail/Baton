"""Conversation endpoints, with an injected agent (scripted LLM + in-process store)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.compiler import compile_aops
from baton.api.app import create_app
from baton.config import Settings
from baton.db import Base, make_engine, make_session_factory
from baton.knowledge.base import KnowledgeBase
from baton.runtime.agent import Agent
from baton.runtime.state import ConversationBusyError
from baton.tools.client import CommerceClient
from baton.tools.northwind import NorthwindTools
from tests.knowledge.judge import judge, supported_if
from tests.runtime.support import ScriptedLLM, call, say

REPO = Path(__file__).resolve().parents[2]
SETTINGS = Settings(_env_file=None, baton_llm="fake")  # type: ignore[call-arg]


@pytest.fixture
def sessions(tmp_path: Path) -> sessionmaker[Session]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'baton.db').as_posix()}")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


@pytest.fixture
def llm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest.fixture
def api(llm: ScriptedLLM, commerce: CommerceClient, sessions: sessionmaker[Session]) -> TestClient:
    bundle = compile_aops(REPO / "aops", prompts_dir=REPO / "prompts")
    agent = Agent(bundle, llm, NorthwindTools(commerce), sessions, prompts_dir=REPO / "prompts")
    return TestClient(create_app(SETTINGS, bundle, agent=agent))


def test_conversation_round_trip(api: TestClient, llm: ScriptedLLM) -> None:
    created = api.post("/conversations", json={"channel": "web"})
    assert created.status_code == 201
    cid = created.json()["id"]

    llm.route_to = "where_is_my_order"
    llm.push(
        call(("lookup_customer", {"email": "ava.thompson@example.com"})),
        say("Thanks! What's the ZIP code on that order?"),
    )
    reply = api.post(
        f"/conversations/{cid}/messages",
        json={"text": "where's NW-10002? ava.thompson@example.com"},
    )
    assert reply.status_code == 200
    body = reply.json()
    assert body["status"] == "open"
    assert body["text"].startswith("Thanks!")
    assert body["trace_id"]

    detail = api.get(f"/conversations/{cid}").json()
    assert [m["role"] for m in detail["messages"]] == ["customer", "agent"]
    assert detail["state"]["aop_id"] == "where_is_my_order"
    assert detail["state"]["verified"] is False

    traces = api.get(f"/conversations/{cid}/traces").json()
    assert [t["id"] for t in traces] == [body["trace_id"]]
    assert traces[0]["payload"]["aop_ref"] == "where_is_my_order@1"


def test_customer_hint_is_kept_in_state(api: TestClient) -> None:
    cid = api.post("/conversations", json={"channel": "email", "customer_hint": "a@b.co"}).json()[
        "id"
    ]
    state = api.get(f"/conversations/{cid}").json()["state"]
    assert state["channel"] == "email"
    assert state["slots"] == {"customer_hint": "a@b.co"}


def test_unknown_conversation_is_404(api: TestClient) -> None:
    assert api.get("/conversations/nope").status_code == 404
    assert api.get("/conversations/nope/traces").status_code == 404
    assert api.post("/conversations/nope/messages", json={"text": "hi"}).status_code == 404


@pytest.mark.parametrize("text", ["", "   ", "x" * 4001])
def test_bad_messages_are_422(api: TestClient, text: str) -> None:
    cid = api.post("/conversations", json={}).json()["id"]
    assert api.post(f"/conversations/{cid}/messages", json={"text": text}).status_code == 422


def test_bad_channel_is_422(api: TestClient) -> None:
    assert api.post("/conversations", json={"channel": "fax"}).status_code == 422


def test_without_an_llm_messages_get_a_clear_503(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        baton_llm="fake",
        database_url=f"sqlite:///{(tmp_path / 'x.db').as_posix()}",
    )
    client = TestClient(create_app(settings))
    resp = client.post("/conversations", json={})
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_message_during_a_running_turn_is_409(
    api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cid = api.post("/conversations", json={}).json()["id"]

    def busy(conversation_id: str, text: str) -> None:
        raise ConversationBusyError(conversation_id)

    agent = api.app.state.get_agent()  # type: ignore[attr-defined]
    monkeypatch.setattr(agent, "handle_turn", busy)
    resp = api.post(f"/conversations/{cid}/messages", json={"text": "hello?"})
    assert resp.status_code == 409
    assert "still being written" in resp.json()["detail"]


def test_reply_carries_resolvable_citations(
    llm: ScriptedLLM,
    commerce: CommerceClient,
    sessions: sessionmaker[Session],
    knowledge: KnowledgeBase,
) -> None:
    bundle = compile_aops(REPO / "aops", prompts_dir=REPO / "prompts")
    tools = NorthwindTools(commerce, knowledge=knowledge)
    agent = Agent(bundle, llm, tools, sessions, prompts_dir=REPO / "prompts")
    client = TestClient(create_app(SETTINGS, bundle, agent=agent))
    cid = client.post("/conversations", json={"channel": "web"}).json()["id"]
    llm.route_to = "general_question"
    llm.grounding = judge(supported_if(**{"6-10 business days": "kb:shipping-international"}))
    llm.push(
        call(("search_knowledge", {"query": "Canada shipping time"})),
        say("Shipping to Canada takes 6-10 business days [kb:shipping-international]."),
    )
    body = client.post(f"/conversations/{cid}/messages", json={"text": "Canada shipping?"}).json()
    assert body["text"].endswith("[kb:shipping-international].")  # stored channel-neutral
    assert body["citations"] == [
        {
            "article_id": "shipping-international",
            "title": "International orders and shipping to Canada",
            "url": "https://help.northwind.example/articles/shipping-international",
        }
    ]
