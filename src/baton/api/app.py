"""Baton's HTTP API: health checks, AOPs and conversations; later parts add more routers here.

Run with `uv run uvicorn baton.api.app:app --port 8000`, or via `scripts/dev.py`.
"""

import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response
from sqlalchemy.orm import Session, sessionmaker

from baton import __version__
from baton.aop.compiler import compile_aops
from baton.aop.models import AOPBundle
from baton.api import aops, conversations
from baton.api.logging import configure_logging
from baton.config import Settings, get_settings
from baton.runtime.agent import Agent
from baton.runtime.factory import build_agent, session_factory_for

REQUEST_ID_HEADER = "X-Request-ID"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

log = structlog.get_logger("baton.api")


def _request_id(incoming: str | None) -> str:
    """Reuse a caller's request ID if it's well-formed, otherwise mint one."""
    if incoming and _SAFE_REQUEST_ID.match(incoming):
        return incoming
    return uuid.uuid4().hex


def create_app(
    settings: Settings | None = None,
    aop_bundle: AOPBundle | None = None,
    *,
    agent: Agent | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> FastAPI:
    """Build the app. AOPs compile here, so invalid AOPs stop startup with the diagnostics.

    The agent and the database engine are created on first use, so importing or starting the
    app needs neither an API key nor a database. Tests inject both.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="Baton", version=__version__)
    app.state.settings = settings
    bundle = aop_bundle or compile_aops(settings.aops_dir, prompts_dir=settings.prompts_dir)
    app.state.aop_bundle = bundle
    app.include_router(aops.router)
    app.include_router(conversations.router)

    lock = threading.Lock()
    lazy: dict[str, object] = {}

    def get_session_factory() -> sessionmaker[Session]:
        with lock:
            if "sessions" not in lazy:
                lazy["sessions"] = session_factory or (
                    agent.sessions if agent else session_factory_for(settings)
                )
            factory = lazy["sessions"]
        assert isinstance(factory, sessionmaker)
        return factory

    def get_agent() -> Agent:
        if agent is not None:
            return agent
        sessions = get_session_factory()
        with lock:
            if "agent" not in lazy:
                lazy["agent"] = build_agent(settings, bundle, sessions)
            built = lazy["agent"]
        assert isinstance(built, Agent)
        return built

    app.state.session_factory = get_session_factory
    app.state.get_agent = get_agent

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = _request_id(request.headers.get(REQUEST_ID_HEADER))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request_failed", method=request.method, path=request.url.path)
            raise
        response.headers[REQUEST_ID_HEADER] = request_id
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "llm": str(settings.baton_llm)}

    return app


def __getattr__(name: str) -> FastAPI:
    # Lazily built so importing this module (e.g. in tests) has no side effects.
    if name == "app":
        return create_app()
    raise AttributeError(name)
