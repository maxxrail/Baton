"""Signed links to a conversation's transcript, for replies too long for SMS.

An SMS reply that doesn't fit in two segments ends with a link to the full answer. That link
must not be the bare conversation id: anyone the text is forwarded to could read the whole
(verified) conversation forever. Instead it carries `t=<issued, hex>.<hmac>`, keyed with
`BATON_LINK_SECRET` and bound to the conversation id, and it expires after `LINK_TTL_S`.
Without a secret configured, SMS replies are cut without a link and non-web transcripts can't
be opened at all.
"""

import hashlib
import hmac
import time

LINK_TTL_S = 7 * 24 * 3600


def _sign(secret: str, conversation_id: str, issued: int) -> str:
    msg = f"transcript|{conversation_id}|{issued}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def transcript_token(secret: str, conversation_id: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    # Hex, so the token never looks like a phone number to log redaction.
    return f"{issued:x}.{_sign(secret, conversation_id, issued)}"


def valid_transcript_token(
    secret: str, conversation_id: str, token: str | None, now: float | None = None
) -> bool:
    if not secret or not token or "." not in token:
        return False
    issued_raw, sig = token.split(".", 1)
    try:
        issued = int(issued_raw, 16)
    except ValueError:
        return False
    expected = _sign(secret, conversation_id, issued)
    if not hmac.compare_digest(expected.encode(), sig.encode("utf-8", "replace")):
        return False
    now = now if now is not None else time.time()
    return 0 <= now - issued <= LINK_TTL_S
