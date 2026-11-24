"""A full Baton app for channel tests: scripted LLM, in-process store, in-memory outboxes."""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session, sessionmaker

from baton.aop.compiler import compile_aops
from baton.api.app import create_app
from baton.channels.email import ConsoleEmailSender
from baton.channels.events import EventHub
from baton.channels.sms import ConsoleSmsSender, twilio_signature
from baton.config import Settings
from baton.db import Base, make_engine, make_session_factory
from baton.runtime.agent import Agent
from baton.tools.client import CommerceClient
from baton.tools.northwind import NorthwindTools
from tests.runtime.support import ScriptedLLM

REPO = Path(__file__).resolve().parents[2]
TWILIO_TOKEN = "twilio-test-token"
EMAIL_SECRET = "email-test-secret"
CONSOLE_SECRET = "console-test-secret"
LINK_SECRET = "link-test-secret"
SMS_URL = "http://testserver/channels/sms/inbound"
OPS = {"Authorization": f"Bearer {CONSOLE_SECRET}"}

AVA_EMAIL = "ava.thompson@example.com"
AVA_PHONE = "+12065550101"
AVA = {"customer_id": "cus_0001", "order_number": "NW-10002", "zip": "98101"}


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "baton_llm": "fake",
        "twilio_auth_token": TWILIO_TOKEN,
        "email_webhook_secret": EMAIL_SECRET,
        "baton_console_secret": CONSOLE_SECRET,
        "baton_web_url": "https://chat.northwind.example",
        "baton_link_secret": LINK_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@dataclass
class Harness:
    client: TestClient
    llm: ScriptedLLM
    sessions: sessionmaker[Session]
    email: ConsoleEmailSender
    sms: ConsoleSmsSender
    hub: EventHub
    sms_count: int = 0

    def send_sms(
        self,
        body: str,
        sender: str = AVA_PHONE,
        *,
        signature: str | None = None,
        sid: str | None = None,
    ) -> Response:
        self.sms_count += 1
        message_sid = sid or f"SM{self.sms_count}"
        params = {"From": sender, "To": "+15550000000", "Body": body, "MessageSid": message_sid}
        sig = signature or twilio_signature(TWILIO_TOKEN, SMS_URL, params.items())
        return self.client.post(
            "/channels/sms/inbound",
            content=urlencode(params),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-twilio-signature": sig,
            },
        )

    def send_email(self, **fields: object) -> Response:
        payload: dict[str, object] = {
            "from": f"Ava Thompson <{AVA_EMAIL}>",
            "subject": "Where is my order?",
            "text": "Hello",
            "message_id": "<m1@mail.example>",
        }
        payload.update(fields)
        return self.client.post(
            "/channels/email/inbound",
            json=payload,
            headers={"x-baton-webhook-secret": EMAIL_SECRET},
        )


@pytest.fixture
def harness(tmp_path: Path, commerce: CommerceClient) -> Iterator[Harness]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'baton.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    bundle = compile_aops(REPO / "aops", prompts_dir=REPO / "prompts")
    llm = ScriptedLLM()
    agent = Agent(bundle, llm, NorthwindTools(commerce), sessions, prompts_dir=REPO / "prompts")
    email, sms, hub = ConsoleEmailSender(echo=False), ConsoleSmsSender(echo=False), EventHub()
    app = create_app(settings(), bundle, agent=agent, email_sender=email, sms_sender=sms, hub=hub)
    with TestClient(app) as client:
        yield Harness(client, llm, sessions, email, sms, hub)
    engine.dispose()
