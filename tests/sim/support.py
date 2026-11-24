"""Builders for simulation tests: turns with traces, and scenarios from dicts."""

from baton.sim.results import TurnRecord
from baton.sim.suite import Scenario


def tool(
    name: str,
    *,
    ok: bool = True,
    blocked_by: str | None = None,
    source: str = "agent",
    error_code: str | None = None,
) -> dict[str, object]:
    """A trace tool call. Blocked or errored calls are never ok."""
    ok = ok and blocked_by is None and error_code is None
    return {
        "name": name,
        "args": {},
        "ok": ok,
        "blocked_by": blocked_by,
        "error_code": error_code,
        "source": source,
        "result": ({"verified": True} if name == "verify_customer" else {}) if ok else None,
    }


def turn(
    index: int,
    *calls: dict[str, object],
    agent: str = "ok",
    customer: str = "hi",
    error: str | None = None,
    status: str = "open",
) -> TurnRecord:
    return TurnRecord(
        index=index,
        customer=customer,
        agent=agent,
        status=status,
        trace={"tool_calls": list(calls), "error": error, "latency_ms": 100.0},
    )


def scenario(**overrides: object) -> Scenario:
    data: dict[str, object] = {
        "id": "s1",
        "aop": "return_item",
        "persona": {"name": "p", "goal": "g"},
        "expect": {"must": ["does a thing"]},
    }
    data.update(overrides)
    return Scenario.model_validate(data)
