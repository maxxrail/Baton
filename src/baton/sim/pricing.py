"""Estimated cost of model calls, for the cost-per-conversation metric.

Prices are Anthropic first-party list prices in USD per million tokens (input, output), as of
2026-11. They're estimates: prompt-cache discounts and fallback-model repricing aren't modelled.
An unknown model costs 0 and is reported as such, rather than guessed.
"""

from baton.llm.types import Usage as LLMUsage
from baton.sim.results import Usage

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.00, 40.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def price(model: str) -> tuple[float, float] | None:
    """Prices for `model`; tolerates a dated or suffixed id (`claude-sonnet-5-5-20261101`)."""
    if model in PRICES_PER_MTOK:
        return PRICES_PER_MTOK[model]
    for known in sorted(PRICES_PER_MTOK, key=len, reverse=True):
        if model.startswith(known + "-"):
            return PRICES_PER_MTOK[known]
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p = price(model)
    if p is None:
        return 0.0
    return (input_tokens * p[0] + output_tokens * p[1]) / 1_000_000


def usage_of(model: str, usage: LLMUsage) -> Usage:
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost_usd(model, usage.input_tokens, usage.output_tokens),
    )


def trace_usage(trace: dict[str, object]) -> Usage:
    """Tokens and cost of every model call recorded in an agent turn trace."""
    total = Usage()
    calls = trace.get("llm_calls")
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        i, o = int(call.get("input_tokens") or 0), int(call.get("output_tokens") or 0)
        total = total.add(
            Usage(input_tokens=i, output_tokens=o, cost_usd=cost_usd(str(call.get("model")), i, o))
        )
    return total
