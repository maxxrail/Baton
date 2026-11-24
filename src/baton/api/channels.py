"""Inbound webhooks for email and SMS.

Both answer fast and process in the background, because a provider times out long before an
agent turn with tool calls finishes (Twilio gives up after 15 seconds). The reply goes out
through the channel's sender when the turn is done (`ConversationService.deliver`).

- `POST /channels/email/inbound`: a generic JSON shape (`EmailInbound`), authenticated with the
  `X-Baton-Webhook-Secret` header (`EMAIL_WEBHOOK_SECRET`). Returns 202.
- `POST /channels/sms/inbound`: Twilio's form post, authenticated by its `X-Twilio-Signature`
  (`TWILIO_AUTH_TOKEN`); a bad or missing signature is a 403. STOP/START/HELP are answered
  in the TwiML response and never reach the agent.

With no secret configured, a webhook answers 503 rather than accepting unauthenticated posts.
"""

import hmac
from typing import Annotated
from urllib.parse import parse_qsl

import structlog
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from baton.api.deps import ServiceDep, require_agent
from baton.channels.email import EmailInbound
from baton.channels.sms import keyword, normalize_phone, twiml, valid_twilio_signature
from baton.config import Settings

router = APIRouter(prefix="/channels", tags=["channels"])
log = structlog.get_logger("baton.api.channels")

MAX_SMS_CHARS = 1600  # Twilio's own limit for a concatenated inbound message
MAX_FORM_BYTES = 64_000


@router.post("/email/inbound", status_code=202)
def email_inbound(
    body: EmailInbound,
    request: Request,
    background: BackgroundTasks,
    service: ServiceDep,
    x_baton_webhook_secret: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    settings: Settings = request.app.state.settings
    configured = settings.email_webhook_secret
    secret = configured.get_secret_value() if configured else ""
    if not secret:
        raise HTTPException(status_code=503, detail="set EMAIL_WEBHOOK_SECRET to accept email")
    if not x_baton_webhook_secret or not hmac.compare_digest(
        secret.encode(), x_baton_webhook_secret.encode()
    ):
        raise HTTPException(status_code=403, detail="bad webhook secret")
    require_agent(request)
    background.add_task(service.background_email, body)
    return {"accepted": True}


def _signed_url(request: Request, settings: Settings) -> str:
    """The URL Twilio called. Behind a proxy that's the public URL, not the one we see."""
    if not settings.baton_public_url:
        return str(request.url)
    query = f"?{request.url.query}" if request.url.query else ""
    return settings.baton_public_url.rstrip("/") + request.url.path + query


@router.post("/sms/inbound")
async def sms_inbound(
    request: Request, background: BackgroundTasks, service: ServiceDep
) -> Response:
    settings: Settings = request.app.state.settings
    token = settings.twilio_auth_token.get_secret_value() if settings.twilio_auth_token else ""
    if not token:
        raise HTTPException(status_code=503, detail="set TWILIO_AUTH_TOKEN to accept SMS")
    # Twilio posts application/x-www-form-urlencoded; parsing it here (rather than with
    # python-multipart) keeps exactly the parameters Twilio signed.
    raw = await request.body()
    if len(raw) > MAX_FORM_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    params = parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)
    signature = request.headers.get("x-twilio-signature")
    if not valid_twilio_signature(token, _signed_url(request, settings), params, signature):
        log.warning("sms_bad_signature")
        raise HTTPException(status_code=403, detail="invalid Twilio signature")

    fields = dict(params)
    sender, text = fields.get("From", ""), fields.get("Body", "")[:MAX_SMS_CHARS]
    if not sender.strip():
        raise HTTPException(status_code=422, detail="From is required")
    phone = normalize_phone(sender)
    if keyword(text) is None:
        # Before creating a conversation nobody can answer.
        await run_in_threadpool(require_agent, request)
    sid = fields.get("MessageSid") or None
    result = await run_in_threadpool(service.receive_sms, phone, text, sid)
    if result.conversation_id is not None:
        background.add_task(service.background_sms, result.conversation_id, text, sid)
    return Response(content=twiml(result.immediate), media_type="application/xml")
