"""Tool implementations against the in-process mock store (fixtures: mock_commerce/README)."""

import httpx
import pytest

from baton.knowledge.base import KnowledgeBase
from baton.knowledge.retrieve import Retriever
from baton.llm import EmbeddingError
from baton.tools import schemas as s
from baton.tools.client import CommerceClient
from baton.tools.errors import ToolError, safe_message
from baton.tools.northwind import NorthwindTools, ToolContext

AVA = ToolContext(customer_id="cus_0001")
LIAM = ToolContext(customer_id="cus_0002")
NOBODY = ToolContext(customer_id=None)


@pytest.fixture
def tools(commerce: CommerceClient) -> NorthwindTools:
    return NorthwindTools(commerce)


def _code(outcome: object) -> str | None:
    error = getattr(outcome, "error", None)
    return error.code if error else None


def test_every_registered_tool_has_an_implementation(tools: NorthwindTools) -> None:
    assert (
        len(tools.registry) == 14
    )  # 13 store tools + search_knowledge; constructor raises if any is missing


def test_lookup_masks_contact_details(tools: NorthwindTools) -> None:
    out = tools.execute("lookup_customer", {"email": "AVA.THOMPSON@example.com"}, NOBODY)
    assert isinstance(out.output, s.CustomerLookupOut)
    (ava,) = out.output.customers
    assert ava.id == "cus_0001"
    assert ava.first_name == "Ava"
    assert ava.email == "a***@example.com"
    assert ava.phone == "[phone]"


def test_lookup_by_shared_phone_returns_both_accounts(tools: NorthwindTools) -> None:
    out = tools.execute("lookup_customer", {"phone": "(503) 555-0199"}, NOBODY)
    assert isinstance(out.output, s.CustomerLookupOut)
    assert [c.id for c in out.output.customers] == ["cus_0003", "cus_0004"]


def test_lookup_unknown_customer(tools: NorthwindTools) -> None:
    out = tools.execute("lookup_customer", {"email": "nobody@example.com"}, NOBODY)
    assert _code(out) == "CUSTOMER_NOT_FOUND"


@pytest.mark.parametrize(
    ("zip_code", "expected"), [("98101", True), ("10001", False)], ids=["right", "wrong"]
)
def test_verify(tools: NorthwindTools, zip_code: str, expected: bool) -> None:
    out = tools.execute(
        "verify_customer",
        {"customer_id": "cus_0001", "order_number": "NW-10002", "zip": zip_code},
        NOBODY,
    )
    assert isinstance(out.output, s.VerifyCustomerOut)
    assert out.output.verified is expected


def test_invalid_args_return_validation_errors(tools: NorthwindTools) -> None:
    out = tools.execute("verify_customer", {"customer_id": "cus_0001", "zip": "abc"}, NOBODY)
    assert _code(out) == "INVALID_INPUT"
    assert out.validation_errors
    assert any("order_number" in e for e in out.validation_errors)


def test_verification_required_tools_refuse_without_a_customer(tools: NorthwindTools) -> None:
    out = tools.execute("get_order", {"order_number": "NW-10002"}, NOBODY)
    assert _code(out) == "NOT_VERIFIED"


def test_get_order_scoped_to_owner(tools: NorthwindTools) -> None:
    mine = tools.execute("get_order", {"order_number": "nw-10002"}, AVA)
    assert isinstance(mine.output, s.Order)
    assert mine.output.refundable_cents == 6600
    # Liam's order looks exactly like a missing one to Ava.
    theirs = tools.execute("get_order", {"order_number": "NW-10004"}, AVA)
    missing = tools.execute("get_order", {"order_number": "NW-99999"}, AVA)
    assert _code(theirs) == _code(missing) == "ORDER_NOT_FOUND"


def test_ids_are_stripped(tools: NorthwindTools) -> None:
    out = tools.execute(
        "verify_customer",
        {"customer_id": " cus_0001 ", "order_number": " NW-10002", "zip": "98101 "},
        NOBODY,
    )
    assert isinstance(out.output, s.VerifyCustomerOut)
    assert out.output.verified


def test_customer_id_must_be_the_verified_customer(tools: NorthwindTools) -> None:
    out = tools.execute("get_customer_orders", {"customer_id": "cus_0002"}, AVA)
    assert _code(out) == "CUSTOMER_MISMATCH"
    ok = tools.execute("get_customer_orders", {"customer_id": "cus_0001"}, AVA)
    assert isinstance(ok.output, s.OrderListOut)
    assert {o.order_number for o in ok.output.orders} == {"NW-10001", "NW-10002", "NW-10003"}


def test_path_traversal_in_order_number_is_quoted(tools: NorthwindTools) -> None:
    # Quoted, the id can't reach another route (e.g. Liam's order list); it is just not found.
    out = tools.execute("get_order", {"order_number": "../customers/cus_0002/orders"}, AVA)
    assert _code(out) == "NOT_FOUND"


def test_cannot_cancel_someone_elses_order(tools: NorthwindTools) -> None:
    out = tools.execute("cancel_order", {"order_number": "NW-10003"}, LIAM)
    assert _code(out) == "ORDER_NOT_FOUND"
    still = tools.execute("get_order", {"order_number": "NW-10003"}, AVA)
    assert isinstance(still.output, s.Order)
    assert still.output.status == "processing"


def test_cancel_processing_order(tools: NorthwindTools) -> None:
    out = tools.execute("cancel_order", {"order_number": "NW-10003"}, AVA)
    assert isinstance(out.output, s.Order)
    assert out.output.status == "cancelled"
    assert out.output.refundable_cents == 0
    again = tools.execute("cancel_order", {"order_number": "NW-10003"}, AVA)
    assert _code(again) == "ORDER_NOT_CANCELLABLE"


def test_change_address(tools: NorthwindTools) -> None:
    new = {"line1": "1 Pine St", "city": "Portland", "state": "or", "zip": "97205"}
    out = tools.execute("change_address", {"order_number": "NW-10003", "address": new}, AVA)
    assert isinstance(out.output, s.Order)
    assert out.output.shipping_address.state == "OR"
    shipped = tools.execute("change_address", {"order_number": "NW-10002", "address": new}, AVA)
    assert _code(shipped) == "ADDRESS_CHANGE_NOT_ALLOWED"


def test_returns_respect_window(tools: NorthwindTools) -> None:
    ok = tools.execute("create_return", {"order_number": "NW-10002", "reason": "too big"}, AVA)
    assert isinstance(ok.output, s.ReturnAuthorization)
    assert ok.output.rma_number == "RMA-10002-1"
    late = tools.execute("create_return", {"order_number": "NW-10001", "reason": "meh"}, AVA)
    assert _code(late) == "RETURN_WINDOW_EXPIRED"
    dup = tools.execute("create_return", {"order_number": "NW-10002", "reason": "again"}, AVA)
    assert _code(dup) == "RETURN_ALREADY_EXISTS"


def test_refund_capped_at_balance(tools: NorthwindTools) -> None:
    over = tools.execute(
        "create_refund", {"order_number": "NW-10004", "amount_cents": 17500, "reason": "x"}, LIAM
    )
    assert _code(over) == "REFUND_EXCEEDS_BALANCE"
    ok = tools.execute(
        "create_refund", {"order_number": "NW-10004", "amount_cents": 1000, "reason": "x"}, LIAM
    )
    assert isinstance(ok.output, s.Order)
    assert ok.output.refundable_cents == 16400


def test_subscription_lifecycle_and_scoping(tools: NorthwindTools) -> None:
    ethan = ToolContext(customer_id="cus_0019")
    subs = tools.execute("get_subscriptions", {"customer_id": "cus_0019"}, ethan)
    assert isinstance(subs.output, s.SubscriptionListOut)
    assert [x.id for x in subs.output.subscriptions] == ["sub_0001"]

    assert _code(tools.execute("pause_subscription", {"subscription_id": "sub_0001"}, AVA)) == (
        "SUBSCRIPTION_NOT_FOUND"
    )
    paused = tools.execute("pause_subscription", {"subscription_id": "sub_0001"}, ethan)
    assert isinstance(paused.output, s.Subscription)
    assert paused.output.status == "paused"
    again = tools.execute("pause_subscription", {"subscription_id": "sub_0001"}, ethan)
    assert _code(again) == "SUBSCRIPTION_NOT_ACTIVE"
    resumed = tools.execute("resume_subscription", {"subscription_id": "sub_0001"}, ethan)
    assert isinstance(resumed.output, s.Subscription)
    assert resumed.output.next_ship_at is not None
    cancelled = tools.execute("cancel_subscription", {"subscription_id": "sub_0001"}, ethan)
    assert isinstance(cancelled.output, s.Subscription)
    assert cancelled.output.status == "cancelled"


def test_loyalty(tools: NorthwindTools) -> None:
    out = tools.execute("get_loyalty", {"customer_id": "cus_0001"}, AVA)
    assert isinstance(out.output, s.Loyalty)
    assert out.output.tier in {"bronze", "silver", "gold"}


def test_returned_orders_parse(tools: NorthwindTools) -> None:
    # cus_0019 has a "returned" order; the status literal must accept it.
    out = tools.execute(
        "get_customer_orders", {"customer_id": "cus_0019"}, ToolContext(customer_id="cus_0019")
    )
    assert isinstance(out.output, s.OrderListOut)
    assert "returned" in {o.status for o in out.output.orders}


# ---------------------------------------------------------------- HTTP client behaviour


def _client(handler: httpx.MockTransport) -> CommerceClient:
    return CommerceClient(httpx.Client(transport=handler, base_url="http://store"))


def test_get_5xx_is_retried_once_then_succeeds() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"order_number": "NW-1"})

    assert _client(httpx.MockTransport(handle)).get("/orders/NW-1") == {"order_number": "NW-1"}
    assert calls == 2


def test_post_5xx_is_not_retried() -> None:
    """A 5xx can arrive after the refund committed; retrying would pay twice."""
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502)

    with pytest.raises(ToolError) as exc:
        _client(httpx.MockTransport(handle)).post("/orders/NW-1/refund", {})
    assert exc.value.code == "SERVICE_UNAVAILABLE"
    assert calls == 1


def test_5xx_twice_is_service_unavailable() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with pytest.raises(ToolError) as exc:
        _client(httpx.MockTransport(handle)).get("/orders/NW-1")
    assert exc.value.code == "SERVICE_UNAVAILABLE"
    assert calls == 2


def test_timeout_retried_for_get_but_not_post() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(httpx.MockTransport(handle))
    with pytest.raises(ToolError):
        client.get("/orders/NW-1")
    with pytest.raises(ToolError) as exc:
        client.post("/orders/NW-1/refund", {})
    assert exc.value.code == "SERVICE_UNAVAILABLE"
    assert calls == ["GET", "GET", "POST"]


def test_store_error_text_is_replaced_by_safe_message() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = {"error": {"code": "ORDER_NOT_CANCELLABLE", "message": "internal: row 42 locked"}}
        return httpx.Response(409, json=body)

    with pytest.raises(ToolError) as exc:
        _client(httpx.MockTransport(handle)).post("/orders/NW-1/cancel")
    assert exc.value.code == "ORDER_NOT_CANCELLABLE"
    assert "row 42" not in exc.value.customer_safe_message
    assert exc.value.customer_safe_message == safe_message("ORDER_NOT_CANCELLABLE")


def test_unknown_error_codes_get_a_generic_message() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, text="teapot")

    with pytest.raises(ToolError) as exc:
        _client(httpx.MockTransport(handle)).get("/x")
    assert exc.value.code == "UNKNOWN_ERROR"


# ---------------------------------------------------------------- help center (Part 03)


def test_search_knowledge_needs_no_identity_and_returns_citable_passages(
    commerce: CommerceClient, knowledge: KnowledgeBase
) -> None:
    tools = NorthwindTools(commerce, knowledge=knowledge)
    out = tools.execute(
        "search_knowledge", {"query": "how long does shipping to Canada take?"}, NOBODY
    )
    assert isinstance(out.output, s.SearchKnowledgeOut)
    assert out.output.covered
    first = out.output.results[0]
    assert first.article_id == "shipping-international"
    assert first.cite_as == "[kb:shipping-international]"
    assert "6-10 business days" in first.text
    assert len(out.output.results) == 5


def test_search_knowledge_flags_questions_it_doesnt_cover(
    commerce: CommerceClient, knowledge: KnowledgeBase
) -> None:
    tools = NorthwindTools(commerce, knowledge=knowledge)
    out = tools.execute(
        "search_knowledge", {"query": "are you hiring? I'd like to apply for a job"}, NOBODY
    )
    assert isinstance(out.output, s.SearchKnowledgeOut)
    assert not out.output.covered
    assert "not sure" in out.output.note


def test_search_knowledge_validates_input_and_reports_a_missing_kb(tools: NorthwindTools) -> None:
    assert tools.execute("search_knowledge", {"query": ""}, NOBODY).error.code == "INVALID_INPUT"  # type: ignore[union-attr]
    out = tools.execute("search_knowledge", {"query": "returns"}, NOBODY)
    assert out.error is not None
    assert out.error.code == "KNOWLEDGE_UNAVAILABLE"


def test_an_embedding_outage_reports_knowledge_unavailable(
    commerce: CommerceClient, retriever: Retriever
) -> None:
    class DownEmbedder:
        dim, name = retriever.embedder.dim, retriever.embedder.name

        def embed(self, texts: list[str]) -> object:
            raise EmbeddingError("Voyage embeddings request failed: ConnectError")

    broken = Retriever(retriever.chunks, retriever.vectors, DownEmbedder())  # type: ignore[arg-type]
    kb = KnowledgeBase(None, broken.embedder, retriever=broken)
    out = NorthwindTools(commerce, knowledge=kb).execute(
        "search_knowledge", {"query": "returns"}, NOBODY
    )
    assert out.error is not None
    assert out.error.code == "KNOWLEDGE_UNAVAILABLE"
