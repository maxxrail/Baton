"""`ToolError`: the one way a tool reports failure.

Every failure has a machine-readable `code` (the store's own error codes plus a few of Baton's)
and a `customer_safe_message` the model may paraphrase to the customer. Safe means: no stack
traces, no internal URLs, and nothing that tells the customer whether someone else's order or
account exists. An order that belongs to another customer is reported as `ORDER_NOT_FOUND`,
exactly like one that doesn't exist.
"""

# Messages for the store's error codes and Baton's own. Anything unknown falls back to a
# generic message so a new backend code can never leak raw text to the customer.
SAFE_MESSAGES: dict[str, str] = {
    # Baton's own codes.
    "INVALID_INPUT": "The request was missing something or had an invalid value.",
    "NOT_VERIFIED": "The customer's identity must be verified first.",
    "CUSTOMER_MISMATCH": "That account isn't the verified customer's account.",
    "SERVICE_UNAVAILABLE": "The store system isn't responding right now. Please try again shortly.",
    "NOT_FOUND": "Nothing was found with those details.",
    "UNKNOWN_ERROR": "Something went wrong looking that up.",
    "KNOWLEDGE_UNAVAILABLE": "The help center can't be searched right now.",
    # Mock commerce codes (see src/mock_commerce/README.md).
    "INVALID_LOOKUP": "Provide either an email address or a phone number.",
    "CUSTOMER_NOT_FOUND": "No account was found with those details.",
    "ORDER_NOT_FOUND": "No order with that number was found on this account.",
    "SUBSCRIPTION_NOT_FOUND": "No subscription with that id was found on this account.",
    "ORDER_NOT_CANCELLABLE": "Only orders that haven't shipped yet can be cancelled.",
    "ADDRESS_CHANGE_NOT_ALLOWED": "The address can only change before the order ships.",
    "ORDER_NOT_RETURNABLE": "Only delivered orders can be returned.",
    "RETURN_WINDOW_EXPIRED": "This order is outside the 30-day return window.",
    "RETURN_ALREADY_EXISTS": "There is already an open return for this order.",
    "REFUND_EXCEEDS_BALANCE": "That's more than the amount left to refund on this order.",
    "SUBSCRIPTION_NOT_ACTIVE": "Only an active subscription can be paused.",
    "SUBSCRIPTION_NOT_PAUSED": "Only a paused subscription can be resumed.",
    "SUBSCRIPTION_ALREADY_CANCELLED": "This subscription is already cancelled.",
}


def safe_message(code: str) -> str:
    return SAFE_MESSAGES.get(code, SAFE_MESSAGES["UNKNOWN_ERROR"])


class ToolError(Exception):
    """A tool failed in a way the agent should explain, not crash on."""

    def __init__(self, code: str, customer_safe_message: str | None = None) -> None:
        self.code = code
        self.customer_safe_message = customer_safe_message or safe_message(code)
        super().__init__(f"{code}: {self.customer_safe_message}")
