"""Send a fake inbound SMS or email to a running Baton API, signed like the real thing.

    uv run python -m scripts.simulate_inbound --channel sms --from "+15555550123" \\
        --text "where is my order"
    uv run python -m scripts.simulate_inbound --channel email --from ava.thompson@example.com \\
        --subject "Where is my order?" --text "NW-10002 hasn't arrived"
    uv run python -m scripts.simulate_inbound --channel email --from ava.thompson@example.com \\
        --text "thanks!" --in-reply-to "<id-from-the-reply@northwind.example>"

SMS is signed with TWILIO_AUTH_TOKEN (or the dev default `scripts.dev` uses), over the URL
the API will see (BATON_PUBLIC_URL if set), so it passes the same signature check as Twilio.
Email carries EMAIL_WEBHOOK_SECRET. Then it waits for the agent's reply and prints it: the
reply itself goes out through the configured sender (printed by the API in dev).
"""

import argparse
import sys
import time
import uuid

import httpx

from baton.channels.sms import normalize_phone, twilio_signature
from baton.config import Settings
from baton.runtime.redact import redact
from scripts.dev import (
    BATON_PORT,
    DEV_CONSOLE_SECRET,
    DEV_EMAIL_SECRET,
    DEV_TWILIO_TOKEN,
)

REPLY_ROLES = {"agent", "human_agent"}


def _secret(value: object, default: str) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else default


def send_sms(client: httpx.Client, settings: Settings, sender: str, text: str) -> httpx.Response:
    path = "/channels/sms/inbound"
    params = {
        "From": sender,
        "To": settings.twilio_from_number or "+15550000000",
        "Body": text,
        "MessageSid": f"SM{uuid.uuid4().hex}",
    }
    base = settings.baton_public_url or str(client.base_url)
    token = _secret(settings.twilio_auth_token, DEV_TWILIO_TOKEN)
    signature = twilio_signature(token, base.rstrip("/") + path, params.items())
    return client.post(path, data=params, headers={"X-Twilio-Signature": signature})


def send_email(
    client: httpx.Client,
    settings: Settings,
    sender: str,
    subject: str,
    text: str,
    in_reply_to: str | None,
) -> httpx.Response:
    payload: dict[str, object] = {
        "from": sender,
        "subject": subject,
        "text": text,
        "message_id": f"<{uuid.uuid4().hex}@simulated.example>",
    }
    if in_reply_to:
        payload["in_reply_to"] = in_reply_to
    secret = _secret(settings.email_webhook_secret, DEV_EMAIL_SECRET)
    return client.post(
        "/channels/email/inbound", json=payload, headers={"X-Baton-Webhook-Secret": secret}
    )


def _ops_headers(settings: Settings) -> dict[str, str]:
    secret = _secret(settings.baton_console_secret, DEV_CONSOLE_SECRET)
    return {"Authorization": f"Bearer {secret}"}


def last_message_id(client: httpx.Client, settings: Settings, contact: str) -> int:
    """The newest message id in this contact's conversations (0 if none or no access)."""
    headers = _ops_headers(settings)
    resp = client.get("/ops/conversations", headers=headers)
    if resp.status_code != 200:
        return 0
    newest = 0
    for item in resp.json():
        if item["contact"] == contact:
            detail = client.get(f"/conversations/{item['id']}", headers=headers).json()
            newest = max([newest, *(m["id"] for m in detail["messages"])])
    return newest


def wait_for_reply(
    client: httpx.Client, settings: Settings, contact: str, after_id: int, timeout_s: float
) -> str | None:
    """Poll until a reply newer than `after_id` shows up in this contact's conversation."""
    headers = _ops_headers(settings)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get("/ops/conversations", headers=headers)
        if resp.status_code != 200:
            print(f"(can't read the inbox: {resp.status_code})", file=sys.stderr)
            return None
        for item in resp.json():
            if item["contact"] != contact:
                continue
            detail = client.get(f"/conversations/{item['id']}", headers=headers).json()
            new = [m for m in detail["messages"] if m["id"] > after_id]
            replies = [m for m in new if m["role"] in REPLY_ROLES]
            if replies:
                return str(replies[-1]["content"])
            if new and detail["status"] == "escalated" and new[-1]["role"] == "customer":
                return "(a teammate owns this conversation; the agent stays silent)"
        time.sleep(0.5)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--channel", choices=["sms", "email"], required=True)
    parser.add_argument("--from", dest="sender", required=True, help="phone number or email")
    parser.add_argument("--text", required=True)
    parser.add_argument("--subject", default="Question about my order")
    parser.add_argument("--in-reply-to", default=None)
    parser.add_argument("--api", default=f"http://localhost:{BATON_PORT}")
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait for a reply")
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings()
    with httpx.Client(base_url=args.api, timeout=10.0) as client:
        contact = (
            normalize_phone(args.sender) if args.channel == "sms" else args.sender.strip().lower()
        )
        try:
            before = last_message_id(client, settings, contact)
            if args.channel == "sms":
                resp = send_sms(client, settings, args.sender, args.text)
            else:
                resp = send_email(
                    client, settings, args.sender, args.subject, args.text, args.in_reply_to
                )
        except httpx.ConnectError:
            print(f"can't reach the API at {args.api}; is `scripts.dev` running?", file=sys.stderr)
            return 2
        print(f"POST {resp.request.url.path} -> {resp.status_code}")
        if resp.status_code >= 300:
            print(resp.text, file=sys.stderr)
            return 1
        if args.channel == "sms" and "<Message>" in resp.text:
            print(f"immediate reply (TwiML): {redact(resp.text)}")
            return 0
        if args.no_wait:
            return 0
        reply = wait_for_reply(client, settings, contact, before, args.timeout)
    if reply is None:
        print(f"no reply within {args.timeout:.0f}s", file=sys.stderr)
        return 1
    print(f"reply: {redact(reply)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
