"""Fixtures for agent tests: a temp Baton DB, the in-process mock store and a scripted LLM."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.compiler import compile_aops
from baton.aop.models import AOPBundle
from baton.db import Base, make_engine, make_session_factory
from baton.runtime.agent import Agent
from baton.tools.client import CommerceClient
from baton.tools.northwind import NorthwindTools
from tests.runtime.support import REPO, ScriptedLLM


@pytest.fixture(scope="session")
def bundle() -> AOPBundle:
    return compile_aops(REPO / "aops", prompts_dir=REPO / "prompts")


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'baton.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def llm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest.fixture
def tools(commerce: CommerceClient) -> NorthwindTools:
    return NorthwindTools(commerce)


@pytest.fixture
def agent(
    bundle: AOPBundle, llm: ScriptedLLM, tools: NorthwindTools, sessions: sessionmaker[Session]
) -> Agent:
    return Agent(bundle, llm, tools, sessions, prompts_dir=REPO / "prompts", raise_errors=True)
