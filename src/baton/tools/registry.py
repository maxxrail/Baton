"""The tool registry: every tool an AOP may reference, with its typed contract.

Part 01 only needs the contract (names, input/output models, flags) so the AOP validator can
reject `@unknown_tool` and catch side-effecting procedures that skip identity verification.
Part 02 adds the implementations that call the mock commerce API.

Flags:
- `side_effect`: the tool changes something in the store (cancel, refund, ...).
- `requires_verification`: the runtime blocks the tool until the customer has passed
  `verify_customer` in this conversation. Everything that reads or changes account data has it;
  only the two identity tools themselves don't.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from pydantic import BaseModel

from baton.llm.types import ToolSpec
from baton.tools import schemas as s


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    side_effect: bool
    requires_verification: bool

    def to_llm_spec(self) -> ToolSpec:
        """The tool as the model sees it: name, description and a JSON Schema for the input."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )


class ToolRegistry:
    """A name -> `ToolDefinition` map that refuses duplicate names."""

    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool {name!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[ToolDefinition]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        return sorted(self._tools)


def _tool(
    name: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    *,
    side_effect: bool,
    requires_verification: bool = True,
) -> ToolDefinition:
    return ToolDefinition(
        name, description, input_model, output_model, side_effect, requires_verification
    )


NORTHWIND_TOOLS: tuple[ToolDefinition, ...] = (
    # Identity: the only tools usable before verification.
    _tool(
        "lookup_customer",
        "Find a customer account by email or phone. Returns matching accounts.",
        s.LookupCustomerIn,
        s.CustomerLookupOut,
        side_effect=False,
        requires_verification=False,
    ),
    _tool(
        "verify_customer",
        "Verify the customer's identity with an order number on their account and that "
        "order's shipping ZIP code. Returns verified true/false.",
        s.VerifyCustomerIn,
        s.VerifyCustomerOut,
        side_effect=False,
        requires_verification=False,
    ),
    # Orders.
    _tool(
        "get_customer_orders",
        "List the verified customer's orders, newest first.",
        s.CustomerIdIn,
        s.OrderListOut,
        side_effect=False,
    ),
    _tool(
        "get_order",
        "Get one order: status, tracking, items, refunds, returns and return deadline.",
        s.OrderNumberIn,
        s.Order,
        side_effect=False,
    ),
    _tool(
        "cancel_order",
        "Cancel an order that is still processing and refund what was charged.",
        s.OrderNumberIn,
        s.Order,
        side_effect=True,
    ),
    _tool(
        "change_address",
        "Change the shipping address of an order that is still processing.",
        s.ChangeAddressIn,
        s.Order,
        side_effect=True,
    ),
    _tool(
        "create_return",
        "Open a return (RMA) for a delivered order inside the 30-day return window.",
        s.CreateReturnIn,
        s.ReturnAuthorization,
        side_effect=True,
    ),
    _tool(
        "create_refund",
        "Refund part or all of an order's remaining refundable balance.",
        s.CreateRefundIn,
        s.Order,
        side_effect=True,
    ),
    # Subscriptions.
    _tool(
        "get_subscriptions",
        "List the verified customer's subscriptions with status and next ship date.",
        s.CustomerIdIn,
        s.SubscriptionListOut,
        side_effect=False,
    ),
    _tool(
        "pause_subscription",
        "Pause an active subscription.",
        s.SubscriptionIdIn,
        s.Subscription,
        side_effect=True,
    ),
    _tool(
        "resume_subscription",
        "Resume a paused subscription; the next shipment is scheduled one cadence from today.",
        s.SubscriptionIdIn,
        s.Subscription,
        side_effect=True,
    ),
    _tool(
        "cancel_subscription",
        "Cancel a subscription. Shipments already sent are not refunded.",
        s.SubscriptionIdIn,
        s.Subscription,
        side_effect=True,
    ),
    # Loyalty.
    _tool(
        "get_loyalty",
        "Get the verified customer's loyalty points balance, lifetime points and tier.",
        s.CustomerIdIn,
        s.Loyalty,
        side_effect=False,
    ),
)


def default_registry() -> ToolRegistry:
    """The Northwind tool set."""
    return ToolRegistry(NORTHWIND_TOOLS)
