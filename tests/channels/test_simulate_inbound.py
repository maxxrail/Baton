"""`scripts.simulate_inbound` signs requests the way the real webhooks expect."""

from baton.config import Settings
from scripts import simulate_inbound
from tests.channels.conftest import AVA_EMAIL, CONSOLE_SECRET, EMAIL_SECRET, TWILIO_TOKEN, Harness
from tests.runtime.support import say


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        twilio_auth_token=TWILIO_TOKEN,
        email_webhook_secret=EMAIL_SECRET,
        baton_console_secret=CONSOLE_SECRET,
    )


def test_simulated_sms_passes_the_signature_check_and_gets_a_reply(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hi from Northwind!"))
    settings = _settings()
    before = simulate_inbound.last_message_id(harness.client, settings, "+15555550123")  # type: ignore[arg-type]
    resp = simulate_inbound.send_sms(harness.client, settings, "+15555550123", "hello")  # type: ignore[arg-type]
    assert resp.status_code == 200
    reply = simulate_inbound.wait_for_reply(
        harness.client,  # type: ignore[arg-type]
        settings,
        "+15555550123",
        before,
        timeout_s=2,
    )
    assert reply == "Hi from Northwind!"


def test_simulated_email_carries_the_webhook_secret(harness: Harness) -> None:
    harness.llm.route_to = "none"
    harness.llm.push(say("Hello Ava."))
    resp = simulate_inbound.send_email(
        harness.client,  # type: ignore[arg-type]
        _settings(),
        AVA_EMAIL,
        "Hi",
        "hello",
        None,
    )
    assert resp.status_code == 202
    assert harness.email.sent[0].to == AVA_EMAIL
