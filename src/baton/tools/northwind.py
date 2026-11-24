"""Implementations of the Northwind tools against the store API.

`NorthwindTools.execute(name, args, ctx)` is the only entry point the runtime uses. It:
1. validates `args` against the tool's input model (bad args -> `INVALID_INPUT`, with the
   validation errors so the model can fix its call),
2. scopes the call to the verified customer in `ctx` (defence in depth: the runtime's
   identity gate already blocks these tools before verification, but the tool layer refuses
   too, so nothing can call it unscoped by mistake),
3. calls the store and validates the response against the tool's output model.

Scoping rules, enforced here because they need the store's data:
- A `customer_id` argument must be the verified customer's (`CUSTOMER_MISMATCH` otherwise).
- An order or subscription that belongs to someone else is reported as not found, the same as
  one that doesn't exist, so the agent can't be used to probe other accounts.
- `lookup_customer` runs before verification, so it masks the contact details it returns.
  The id and first name are enough to continue to `verify_customer`.

`search_knowledge` doesn't touch the store: it searches the help center (`KnowledgeBase`). It
needs no identity, and without a knowledge base it reports `KNOWLEDGE_UNAVAILABLE`.
"""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from baton.knowledge.base import KnowledgeBase
from baton.knowledge.retrieve import KnowledgeIndexError
from baton.llm.voyage import EmbeddingError
from baton.runtime.redact import redact
from baton.tools import schemas as s
from baton.tools.client import CommerceClient, path_part
from baton.tools.errors import ToolError
from baton.tools.registry import ToolRegistry, default_registry


@dataclass(frozen=True)
class ToolContext:
    """Who the tool is acting for. `customer_id` is None until verification succeeds."""

    customer_id: str | None


@dataclass(frozen=True)
class ToolOutcome:
    """The result of one tool call: exactly one of `output` / `error` is set."""

    name: str
    output: BaseModel | None = None
    error: ToolError | None = None
    validation_errors: list[str] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


Handler = Callable[[BaseModel, ToolContext], BaseModel]


class NorthwindTools:
    def __init__(
        self,
        client: CommerceClient,
        registry: ToolRegistry | None = None,
        knowledge: KnowledgeBase | None = None,
    ) -> None:
        self.client = client
        self.registry = registry or default_registry()
        self.knowledge = knowledge
        self._handlers: dict[str, Handler] = {
            "lookup_customer": self._lookup_customer,
            "verify_customer": self._verify_customer,
            "get_customer_orders": self._get_customer_orders,
            "get_order": self._get_order,
            "cancel_order": self._cancel_order,
            "change_address": self._change_address,
            "create_return": self._create_return,
            "create_refund": self._create_refund,
            "get_subscriptions": self._get_subscriptions,
            "pause_subscription": self._subscription_action("pause"),
            "resume_subscription": self._subscription_action("resume"),
            "cancel_subscription": self._subscription_action("cancel"),
            "get_loyalty": self._get_loyalty,
            "search_knowledge": self._search_knowledge,
        }
        missing = set(self.registry.names()) - set(self._handlers)
        if missing:
            raise ValueError(f"no implementation for tools: {sorted(missing)}")

    def execute(self, name: str, args: dict[str, object], ctx: ToolContext) -> ToolOutcome:
        tool = self.registry.get(name)
        try:
            parsed = tool.input_model.model_validate(args)
        except ValidationError as exc:
            errors = [
                f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in exc.errors()
            ]
            return ToolOutcome(name, error=ToolError("INVALID_INPUT"), validation_errors=errors)
        if tool.requires_verification and ctx.customer_id is None:
            return ToolOutcome(name, error=ToolError("NOT_VERIFIED"))
        try:
            output = self._handlers[name](parsed, ctx)
        except ToolError as exc:
            return ToolOutcome(name, error=exc)
        except ValidationError:  # the store answered with a shape we don't understand
            return ToolOutcome(name, error=ToolError("UNKNOWN_ERROR"))
        return ToolOutcome(name, output=tool.output_model.model_validate(output.model_dump()))

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _me(ctx: ToolContext) -> str:
        if ctx.customer_id is None:
            raise ToolError("NOT_VERIFIED")
        return ctx.customer_id

    def _check_customer(self, customer_id: str, ctx: ToolContext) -> str:
        me = self._me(ctx)
        if customer_id.strip() != me:
            raise ToolError("CUSTOMER_MISMATCH")
        return me

    def _owned_order(self, order_number: str, ctx: ToolContext) -> s.Order:
        me = self._me(ctx)
        order = s.Order.model_validate(self.client.get(f"/orders/{path_part(order_number)}"))
        if order.customer_id != me:
            raise ToolError("ORDER_NOT_FOUND")  # same answer as a missing order: no probing
        return order

    def _owned_subscription(self, subscription_id: str, ctx: ToolContext) -> s.Subscription:
        mine = self._subscriptions(self._me(ctx))
        for sub in mine.subscriptions:
            if sub.id == subscription_id.strip():
                return sub
        raise ToolError("SUBSCRIPTION_NOT_FOUND")

    def _subscriptions(self, customer_id: str) -> s.SubscriptionListOut:
        data = self.client.get(f"/subscriptions/{path_part(customer_id)}")
        return s.SubscriptionListOut.model_validate(data)

    # ------------------------------------------------------------------ identity

    def _lookup_customer(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.LookupCustomerIn)
        params = {"email": args.email} if args.email is not None else {"phone": args.phone or ""}
        found = s.CustomerLookupOut.model_validate(self.client.get("/customers/lookup", params))
        # Unverified caller: mask contact details. Last name is kept to tell accounts apart.
        masked = [
            c.model_copy(update={"email": redact(c.email), "phone": redact(c.phone)})
            for c in found.customers
        ]
        return s.CustomerLookupOut(customers=masked)

    def _verify_customer(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.VerifyCustomerIn)
        data = self.client.post(
            f"/customers/{path_part(args.customer_id)}/verify",
            {"order_number": args.order_number, "zip": args.zip},
        )
        return s.VerifyCustomerOut.model_validate(data)

    # ------------------------------------------------------------------ orders

    def _get_customer_orders(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.CustomerIdIn)
        me = self._check_customer(args.customer_id, ctx)
        return s.OrderListOut.model_validate(self.client.get(f"/customers/{path_part(me)}/orders"))

    def _get_order(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.OrderNumberIn)
        return self._owned_order(args.order_number, ctx)

    def _cancel_order(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.OrderNumberIn)
        order = self._owned_order(args.order_number, ctx)
        return s.Order.model_validate(
            self.client.post(f"/orders/{path_part(order.order_number)}/cancel")
        )

    def _change_address(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.ChangeAddressIn)
        order = self._owned_order(args.order_number, ctx)
        data = self.client.post(
            f"/orders/{path_part(order.order_number)}/address", args.address.model_dump()
        )
        return s.Order.model_validate(data)

    def _create_return(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.CreateReturnIn)
        order = self._owned_order(args.order_number, ctx)
        data = self.client.post(
            f"/orders/{path_part(order.order_number)}/return", {"reason": args.reason}
        )
        return s.ReturnAuthorization.model_validate(data)

    def _create_refund(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.CreateRefundIn)
        order = self._owned_order(args.order_number, ctx)
        data = self.client.post(
            f"/orders/{path_part(order.order_number)}/refund",
            {"amount_cents": args.amount_cents, "reason": args.reason},
        )
        return s.Order.model_validate(data)

    # ------------------------------------------------------------------ subscriptions, loyalty

    def _get_subscriptions(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.CustomerIdIn)
        return self._subscriptions(self._check_customer(args.customer_id, ctx))

    def _subscription_action(self, action: str) -> Handler:
        def handler(args: BaseModel, ctx: ToolContext) -> BaseModel:
            assert isinstance(args, s.SubscriptionIdIn)
            sub = self._owned_subscription(args.subscription_id, ctx)
            data = self.client.post(f"/subscriptions/{path_part(sub.id)}/{action}")
            return s.Subscription.model_validate(data)

        return handler

    def _get_loyalty(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.CustomerIdIn)
        me = self._check_customer(args.customer_id, ctx)
        return s.Loyalty.model_validate(self.client.get(f"/customers/{path_part(me)}/loyalty"))

    # ------------------------------------------------------------------ help center

    def _search_knowledge(self, args: BaseModel, ctx: ToolContext) -> BaseModel:
        assert isinstance(args, s.SearchKnowledgeIn)
        if self.knowledge is None:
            raise ToolError("KNOWLEDGE_UNAVAILABLE")
        try:
            found = self.knowledge.search(args.query)
        except (KnowledgeIndexError, EmbeddingError):  # index stale or embedder down
            raise ToolError("KNOWLEDGE_UNAVAILABLE") from None
        results = [
            s.KnowledgeResult(
                chunk_id=h.chunk.id,
                article_id=h.chunk.article_id,
                title=h.chunk.title,
                section=h.chunk.section,
                updated_at=h.chunk.updated_at,
                text=h.chunk.text,
                cite_as=h.chunk.citation,
            )
            for h in found.hits
        ]
        if not results:
            note = "Nothing in the help center matches. Say you're not sure and offer a teammate."
        elif found.covered:
            note = "Answer only from these passages and cite each one you use as [kb:article_id]."
        else:
            note = (
                "These passages probably don't answer the question. Unless one clearly does, "
                "say you're not sure and offer a teammate; don't stretch a near match."
            )
        return s.SearchKnowledgeOut(
            covered=found.covered and bool(results), results=results, note=note
        )
