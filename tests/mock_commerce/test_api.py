"""Mock commerce endpoints and the business rules they enforce."""

from fastapi.testclient import TestClient
from httpx import Response

from mock_commerce.seed import (
    FIXTURE_DELIVERED_10_DAYS,
    FIXTURE_DELIVERED_45_DAYS,
    FIXTURE_PARTIAL_REFUND,
    FIXTURE_PROCESSING,
    FIXTURE_SHARED_PHONE,
)


def _code(resp: Response) -> str:
    return str(resp.json()["error"]["code"])


# --------------------------------------------------------------------------- customers


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_lookup_by_email_is_case_insensitive(client: TestClient) -> None:
    resp = client.get("/customers/lookup", params={"email": "  AVA.Thompson@Example.com "})
    assert resp.status_code == 200
    customers = resp.json()["customers"]
    assert [c["id"] for c in customers] == ["cus_0001"]
    assert "zip" not in customers[0]  # zip is a verification secret; never returned


def test_lookup_by_shared_phone_returns_both_accounts(client: TestClient) -> None:
    resp = client.get("/customers/lookup", params={"phone": "(503) 555-0199"})
    assert resp.status_code == 200
    assert FIXTURE_SHARED_PHONE == "+15035550199"
    assert [c["id"] for c in resp.json()["customers"]] == ["cus_0003", "cus_0004"]


def test_lookup_not_found_and_bad_params(client: TestClient) -> None:
    resp = client.get("/customers/lookup", params={"email": "nobody@example.com"})
    assert (resp.status_code, _code(resp)) == (404, "CUSTOMER_NOT_FOUND")
    resp = client.get("/customers/lookup")
    assert (resp.status_code, _code(resp)) == (422, "INVALID_LOOKUP")
    resp = client.get("/customers/lookup", params={"email": "a@b.c", "phone": "5035550199"})
    assert (resp.status_code, _code(resp)) == (422, "INVALID_LOOKUP")


def test_verify_with_correct_order_and_zip(client: TestClient) -> None:
    resp = client.post(
        "/customers/cus_0001/verify", json={"order_number": "nw-10001", "zip": "98101"}
    )
    assert resp.json() == {"verified": True}


def test_verify_with_wrong_zip_is_false(client: TestClient) -> None:
    resp = client.post(
        "/customers/cus_0001/verify", json={"order_number": "NW-10001", "zip": "10001"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"verified": False}


def test_verify_with_someone_elses_order_is_false(client: TestClient) -> None:
    # NW-10004 belongs to cus_0002 and ships to 78701.
    resp = client.post(
        "/customers/cus_0001/verify", json={"order_number": "NW-10004", "zip": "78701"}
    )
    assert resp.json() == {"verified": False}


def test_verify_with_unknown_order_is_false_not_404(client: TestClient) -> None:
    resp = client.post(
        "/customers/cus_0001/verify", json={"order_number": "NW-99999", "zip": "98101"}
    )
    assert resp.json() == {"verified": False}


def test_verify_unknown_customer_404(client: TestClient) -> None:
    resp = client.post("/customers/cus_9999/verify", json={"order_number": "x", "zip": "y"})
    assert (resp.status_code, _code(resp)) == (404, "CUSTOMER_NOT_FOUND")


def test_list_orders_newest_first(client: TestClient) -> None:
    orders = client.get("/customers/cus_0001/orders").json()["orders"]
    assert [o["order_number"] for o in orders] == [
        FIXTURE_PROCESSING,
        FIXTURE_DELIVERED_10_DAYS,
        FIXTURE_DELIVERED_45_DAYS,
    ]


def test_loyalty(client: TestClient) -> None:
    body = client.get("/customers/cus_0001/loyalty").json()
    assert body["customer_id"] == "cus_0001"
    assert body["tier"] in {"bronze", "silver", "gold"}
    assert 0 <= body["points_balance"] <= body["lifetime_points"]
    resp = client.get("/customers/cus_9999/loyalty")
    assert _code(resp) == "CUSTOMER_NOT_FOUND"


# --------------------------------------------------------------------------- orders


def test_get_order_detail(client: TestClient) -> None:
    body = client.get(f"/orders/{FIXTURE_PARTIAL_REFUND}").json()
    assert body["total_cents"] == 21400
    assert body["refunded_cents"] == 4000
    assert body["refundable_cents"] == 17400
    assert len(body["items"]) == 2
    assert body["return_eligible_until"] is not None


def test_get_unknown_order_404(client: TestClient) -> None:
    resp = client.get("/orders/NW-99999")
    assert (resp.status_code, _code(resp)) == (404, "ORDER_NOT_FOUND")


def test_cancel_processing_order(client: TestClient) -> None:
    resp = client.post(f"/orders/{FIXTURE_PROCESSING}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    # cancelling refunds the full charge, matching the seeded cancelled orders
    assert body["refunds"][-1]["reason"] == "Order cancelled"
    assert body["refunded_cents"] == body["total_cents"] == 18900
    assert body["refundable_cents"] == 0
    # and it's not cancellable twice
    resp = client.post(f"/orders/{FIXTURE_PROCESSING}/cancel")
    assert (resp.status_code, _code(resp)) == (409, "ORDER_NOT_CANCELLABLE")


def test_cancel_non_processing_order_is_409(client: TestClient) -> None:
    resp = client.post(f"/orders/{FIXTURE_DELIVERED_10_DAYS}/cancel")
    assert resp.status_code == 409
    assert _code(resp) == "ORDER_NOT_CANCELLABLE"
    assert client.get(f"/orders/{FIXTURE_DELIVERED_10_DAYS}").json()["status"] == "delivered"


def test_change_address_only_while_processing(client: TestClient) -> None:
    new = {"line1": "1 New St", "city": "Tacoma", "state": "wa", "zip": "98402"}
    resp = client.post(f"/orders/{FIXTURE_PROCESSING}/address", json=new)
    assert resp.status_code == 200
    assert resp.json()["shipping_address"] == {**new, "state": "WA"}

    resp = client.post(f"/orders/{FIXTURE_DELIVERED_10_DAYS}/address", json=new)
    assert (resp.status_code, _code(resp)) == (409, "ADDRESS_CHANGE_NOT_ALLOWED")


def test_change_address_validates_body(client: TestClient) -> None:
    bad = {"line1": "1 New St", "city": "Tacoma", "state": "WA", "zip": "abc"}
    assert client.post(f"/orders/{FIXTURE_PROCESSING}/address", json=bad).status_code == 422


def test_return_inside_window_creates_rma(client: TestClient) -> None:
    resp = client.post(f"/orders/{FIXTURE_DELIVERED_10_DAYS}/return", json={"reason": "Too big"})
    assert resp.status_code == 201
    rma = resp.json()
    assert rma["status"] == "open"
    assert rma["rma_number"].startswith("RMA-")
    assert client.get(f"/orders/{FIXTURE_DELIVERED_10_DAYS}").json()["returns"][0] == rma

    again = client.post(f"/orders/{FIXTURE_DELIVERED_10_DAYS}/return", json={"reason": "x"})
    assert (again.status_code, _code(again)) == (409, "RETURN_ALREADY_EXISTS")


def test_return_outside_window_is_rejected(client: TestClient) -> None:
    resp = client.post(f"/orders/{FIXTURE_DELIVERED_45_DAYS}/return", json={"reason": "Changed"})
    assert resp.status_code == 409
    assert _code(resp) == "RETURN_WINDOW_EXPIRED"
    assert client.get(f"/orders/{FIXTURE_DELIVERED_45_DAYS}").json()["returns"] == []


def test_return_undelivered_order_is_rejected(client: TestClient) -> None:
    resp = client.post(f"/orders/{FIXTURE_PROCESSING}/return", json={"reason": "x"})
    assert (resp.status_code, _code(resp)) == (409, "ORDER_NOT_RETURNABLE")


def test_refund_up_to_balance(client: TestClient) -> None:
    resp = client.post(
        f"/orders/{FIXTURE_PARTIAL_REFUND}/refund",
        json={"amount_cents": 17400, "reason": "Knife defective"},
    )
    assert resp.status_code == 201
    assert resp.json()["refundable_cents"] == 0


def test_over_refund_is_rejected(client: TestClient) -> None:
    # 21400 total - 4000 already refunded = 17400 left; one cent more must fail.
    resp = client.post(
        f"/orders/{FIXTURE_PARTIAL_REFUND}/refund",
        json={"amount_cents": 17401, "reason": "please"},
    )
    assert resp.status_code == 409
    assert _code(resp) == "REFUND_EXCEEDS_BALANCE"
    assert client.get(f"/orders/{FIXTURE_PARTIAL_REFUND}").json()["refunded_cents"] == 4000


def test_cumulative_refunds_cannot_exceed_total(client: TestClient) -> None:
    order = FIXTURE_DELIVERED_10_DAYS  # total 6600, nothing refunded
    ok = client.post(f"/orders/{order}/refund", json={"amount_cents": 6000, "reason": "a"})
    assert ok.status_code == 201
    over = client.post(f"/orders/{order}/refund", json={"amount_cents": 601, "reason": "b"})
    assert _code(over) == "REFUND_EXCEEDS_BALANCE"


def test_refund_amount_must_be_positive(client: TestClient) -> None:
    for amount in (0, -100):
        resp = client.post(
            f"/orders/{FIXTURE_PARTIAL_REFUND}/refund",
            json={"amount_cents": amount, "reason": "x"},
        )
        assert resp.status_code == 422


# --------------------------------------------------------------------------- subscriptions


def _sub_with_status(client: TestClient, status: str) -> dict[str, object]:
    for n in range(1, 51):
        resp = client.get(f"/subscriptions/cus_{n:04d}")
        for sub in resp.json()["subscriptions"]:
            if sub["status"] == status:
                return sub  # type: ignore[no-any-return]
    raise AssertionError(f"seed has no {status} subscription")


def test_subscription_lifecycle(client: TestClient) -> None:
    sub_id = _sub_with_status(client, "active")["id"]

    paused = client.post(f"/subscriptions/{sub_id}/pause").json()
    assert (paused["status"], paused["next_ship_at"]) == ("paused", None)
    again = client.post(f"/subscriptions/{sub_id}/pause")
    assert (again.status_code, _code(again)) == (409, "SUBSCRIPTION_NOT_ACTIVE")

    resumed = client.post(f"/subscriptions/{sub_id}/resume").json()
    assert resumed["status"] == "active"
    assert resumed["next_ship_at"] is not None
    again = client.post(f"/subscriptions/{sub_id}/resume")
    assert _code(again) == "SUBSCRIPTION_NOT_PAUSED"

    cancelled = client.post(f"/subscriptions/{sub_id}/cancel").json()
    assert cancelled["status"] == "cancelled"
    again = client.post(f"/subscriptions/{sub_id}/cancel")
    assert _code(again) == "SUBSCRIPTION_ALREADY_CANCELLED"
    assert _code(client.post(f"/subscriptions/{sub_id}/resume")) == "SUBSCRIPTION_NOT_PAUSED"


def test_subscription_not_found(client: TestClient) -> None:
    assert _code(client.post("/subscriptions/sub_9999/pause")) == "SUBSCRIPTION_NOT_FOUND"
    assert _code(client.get("/subscriptions/cus_9999")) == "CUSTOMER_NOT_FOUND"


def test_openapi_docs_available(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/orders/{order_number}/refund" in paths
