"""Build a production `Agent` from settings. The API and `baton-chat` both use this."""

from sqlalchemy.orm import Session, sessionmaker

from baton.aop.models import AOPBundle
from baton.config import Settings
from baton.db.session import make_engine, make_session_factory
from baton.knowledge.base import KnowledgeBase
from baton.knowledge.rerank import LLMReranker
from baton.llm import LLMClient, LLMConfigError, get_embedder, get_llm
from baton.runtime.agent import Agent
from baton.runtime.guardrails import GuardrailConfig
from baton.tools.client import CommerceClient
from baton.tools.northwind import NorthwindTools


class AgentUnavailableError(RuntimeError):
    """No usable LLM is configured."""


NO_LLM_MESSAGE = (
    "The agent needs a real model: set ANTHROPIC_API_KEY in .env (or BATON_LLM=anthropic). "
    "With BATON_LLM=fake there is no script to play; tests inject a scripted LLM instead."
)


def session_factory_for(settings: Settings) -> sessionmaker[Session]:
    return make_session_factory(make_engine(settings.database_url))


def build_knowledge(
    settings: Settings, sessions: sessionmaker[Session], llm: LLMClient | None = None
) -> KnowledgeBase:
    """The help center over the ingested `kb_chunks` (loaded on first search)."""
    reranker = LLMReranker(llm, settings.prompts_dir) if settings.baton_kb_rerank and llm else None
    return KnowledgeBase(
        sessions,
        get_embedder(settings),
        reranker=reranker,
        base_url=settings.baton_kb_base_url,
    )


def build_agent(
    settings: Settings,
    bundle: AOPBundle,
    session_factory: sessionmaker[Session] | None = None,
) -> Agent:
    if settings.baton_llm == "fake":
        raise AgentUnavailableError(NO_LLM_MESSAGE)
    try:
        llm = get_llm(settings)
    except LLMConfigError as exc:
        raise AgentUnavailableError(f"{exc}. {NO_LLM_MESSAGE}") from None
    sessions = session_factory or session_factory_for(settings)
    try:
        knowledge = build_knowledge(settings, sessions, llm)
    except LLMConfigError as exc:  # e.g. BATON_EMBEDDER=voyage without a key
        raise AgentUnavailableError(str(exc)) from None
    tools = NorthwindTools(CommerceClient.from_url(settings.mock_commerce_url), knowledge=knowledge)
    return Agent(
        bundle,
        llm,
        tools,
        sessions,
        config=GuardrailConfig(refund_auto_limit_cents=settings.baton_refund_auto_limit_cents),
        prompts_dir=settings.prompts_dir,
    )
