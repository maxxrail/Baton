import pytest
from pydantic import ValidationError

from baton.tools.registry import (
    KNOWLEDGE_TOOL,
    NORTHWIND_TOOLS,
    ToolDefinition,
    ToolRegistry,
    default_registry,
)
from baton.tools.schemas import LookupCustomerIn, OrderNumberIn, VerifyCustomerIn

SIDE_EFFECTS = {
    "cancel_order",
    "change_address",
    "create_return",
    "create_refund",
    "pause_subscription",
    "resume_subscription",
    "cancel_subscription",
}


def test_northwind_tool_names() -> None:
    assert default_registry().names() == sorted(
        [
            "lookup_customer",
            "verify_customer",
            "get_customer_orders",
            "get_order",
            *SIDE_EFFECTS,
            "get_subscriptions",
            "get_loyalty",
            "search_knowledge",
        ]
    )


def test_knowledge_search_is_read_only_and_needs_no_identity() -> None:
    assert KNOWLEDGE_TOOL.name == "search_knowledge"
    assert not KNOWLEDGE_TOOL.side_effect
    assert not KNOWLEDGE_TOOL.requires_verification
    assert "[kb:article_id]" in KNOWLEDGE_TOOL.description


def test_side_effect_flags() -> None:
    assert {t.name for t in NORTHWIND_TOOLS if t.side_effect} == SIDE_EFFECTS


def test_only_identity_tools_skip_verification() -> None:
    open_tools = {t.name for t in NORTHWIND_TOOLS if not t.requires_verification}
    assert open_tools == {"lookup_customer", "verify_customer"}


def test_every_side_effect_tool_requires_verification() -> None:
    assert all(t.requires_verification for t in NORTHWIND_TOOLS if t.side_effect)


def test_llm_spec_has_a_json_schema() -> None:
    spec = default_registry().get("create_refund").to_llm_spec()
    assert spec.name == "create_refund"
    assert spec.input_schema["type"] == "object"
    properties = spec.input_schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"order_number", "amount_cents", "reason"}


def test_registry_lookup_and_duplicates() -> None:
    registry = default_registry()
    assert "get_order" in registry
    assert "get_weather" not in registry
    assert len(registry) == len(NORTHWIND_TOOLS) + 1  # + search_knowledge
    with pytest.raises(KeyError, match="get_weather"):
        registry.get("get_weather")
    with pytest.raises(ValueError, match="already registered"):
        ToolRegistry([NORTHWIND_TOOLS[0], NORTHWIND_TOOLS[0]])


def test_custom_tool_can_be_registered() -> None:
    registry = ToolRegistry()
    registry.register(ToolDefinition("echo", "Echo", OrderNumberIn, OrderNumberIn, False, False))
    assert registry.names() == ["echo"]


@pytest.mark.parametrize(
    "kwargs", [{}, {"email": "a@b.co", "phone": "5035550199"}], ids=["neither", "both"]
)
def test_lookup_requires_exactly_one_of_email_or_phone(kwargs: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        LookupCustomerIn(**kwargs)


def test_verify_zip_must_be_five_digits() -> None:
    with pytest.raises(ValidationError):
        VerifyCustomerIn(customer_id="c", order_number="NW-1", zip="9721")
