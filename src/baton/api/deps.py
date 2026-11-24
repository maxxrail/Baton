"""Shared FastAPI dependencies for the routers added in Part 04."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from baton.channels.events import EventHub
from baton.channels.service import ConversationService
from baton.runtime.agent import Agent
from baton.runtime.factory import AgentUnavailableError


def get_service(request: Request) -> ConversationService:
    try:
        service: ConversationService = request.app.state.get_service()
    except ValueError as exc:  # e.g. EMAIL_PROVIDER=resend without RESEND_API_KEY
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return service


def require_agent(request: Request) -> Agent:
    """Fail with 503 (and how to fix it) before accepting work that needs the agent."""
    try:
        agent: Agent = request.app.state.get_agent()
    except AgentUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return agent


def get_sessions(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] = request.app.state.session_factory()
    return factory


def get_hub(request: Request) -> EventHub:
    hub: EventHub = request.app.state.hub
    return hub


ServiceDep = Annotated[ConversationService, Depends(get_service)]
AgentDep = Annotated[Agent, Depends(require_agent)]
SessionsDep = Annotated[sessionmaker[Session], Depends(get_sessions)]
