import pytest

from baton.runtime.redact import redact, redact_value


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("email me at ava.thompson@example.com", "email me at a***@example.com"),
        ("call +15035550199 now", "call [phone] now"),
        ("call (503) 555-0199", "call [phone]"),
        ("call 503.555.0199", "call [phone]"),
        ("card 4242 4242 4242 4242 thanks", "card [card ****4242] thanks"),
        ("card 4242-4242-4242-4242", "card [card ****4242]"),
    ],
)
def test_masks_pii(raw: str, expected: str) -> None:
    assert redact(raw) == expected


@pytest.mark.parametrize(
    "safe",
    [
        "order NW-10001 shipped",
        "refund of 17400 cents",
        "tracking 1ZNW000000000001",
        "at 2026-11-23T16:47:00Z",
        "zip 98101",
    ],
)
def test_leaves_support_identifiers_alone(safe: str) -> None:
    assert redact(safe) == safe


def test_redact_value_recurses() -> None:
    value = {"a": ["x@y.com", {"b": "5035550199"}], "n": 3, "t": ("z@w.org",)}
    assert redact_value(value) == {
        "a": ["x***@y.com", {"b": "[phone]"}],
        "n": 3,
        "t": ["z***@w.org"],
    }


def test_redact_value_covers_keys_models_and_objects() -> None:
    from pydantic import BaseModel

    class Customer(BaseModel):
        email: str

    class Opaque:
        def __str__(self) -> str:
            return "Opaque(phone=503-555-0199)"

    value = {
        "ava@example.com": "key holds pii",
        "model": Customer(email="ava@example.com"),
        "error": ValueError("no account for ava@example.com"),
        "obj": Opaque(),
        "flag": True,
        "count": 7,
        "none": None,
    }
    out = redact_value(value)
    assert out == {
        "a***@example.com": "key holds pii",
        "model": {"email": "a***@example.com"},
        "error": "no account for a***@example.com",
        "obj": "Opaque(phone=[phone])",
        "flag": True,
        "count": 7,
        "none": None,
    }


def test_known_limits_are_as_documented() -> None:
    assert redact("+44 20 7946 0958") == "+44 20 7946 0958"  # not masked (documented limit)
    assert redact("ts 1732380000000") == "ts [card ****0000]"  # over-masked (safe side)
