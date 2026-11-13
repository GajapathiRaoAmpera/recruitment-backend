"""Mints the one-use Gemini Live ephemeral token the browser connects with.

The API key is read from settings, sent only in the `x-goog-api-key` header and never logged, put
in an error, or returned. The token is locked to the model and the full session config (system
instruction included) through `liveConnectConstraints`, so the browser can't swap the prompt.

Unconfirmed until a call with a real key (docs/09 "Known limits"): whether the lock covers the
system instruction. The browser never sends an instruction of its own either way.
"""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import AppError
from app.interviews.prompts import ANSWER_BOX_TOOL

log = logging.getLogger(__name__)

TOKEN_URL = "https://generativelanguage.googleapis.com/v1beta/auth_tokens"


def not_configured() -> AppError:
    return AppError(
        503, "AI_NOT_CONFIGURED", "The AI interviewer isn't set up on the server yet. Tell HR."
    )


def live_config(system_instruction: str, voice: str | None) -> dict[str, Any]:
    """The session config locked into the token (REST field names)."""
    config: dict[str, Any] = {
        "responseModalities": ["AUDIO"],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        # lifts the 15-minute audio cap; the oldest turns drop out of the model's memory
        "contextWindowCompression": {"slidingWindow": {}},
        # each connection lasts ~10 min; the browser reconnects with the latest handle
        "sessionResumption": {},
        # the interviewer opens a typing box on the candidate's screen for technical questions
        "tools": [{"functionDeclarations": [ANSWER_BOX_TOOL]}],
    }
    if voice and voice.strip():
        config["speechConfig"] = {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice.strip()}}
        }
    return config


def client_config(voice: str | None) -> dict[str, Any]:
    """The same config minus the system instruction: what the browser passes to live.connect.
    If the token's lock doesn't cover a field, the browser's value still matches the server's."""
    config = live_config("", voice)
    config.pop("systemInstruction")
    return config


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def mint_token(
    *,
    system_instruction: str,
    expire_at: datetime,
    new_session_until: datetime,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[str, str]:
    """(token, model). `new_session_until` bounds when a NEW session may open with the token;
    resumptions of that session keep working until `expire_at`."""
    s = get_settings()
    if not s.gemini_api_key:
        raise not_configured()
    model = s.gemini_live_model
    body = {
        "uses": 1,
        "expireTime": _iso(expire_at),
        "newSessionExpireTime": _iso(new_session_until),
        "liveConnectConstraints": {
            "model": model if model.startswith("models/") else f"models/{model}",
            "config": live_config(system_instruction, s.gemini_live_voice),
        },
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0), transport=transport
        ) as client:
            r = await client.post(
                TOKEN_URL,
                headers={"x-goog-api-key": s.gemini_api_key, "Content-Type": "application/json"},
                json=body,
            )
    except httpx.HTTPError as e:
        log.warning("Gemini token request failed (%s)", type(e).__name__)  # no URL, no key
        raise AppError(
            502, "AI_PROVIDER_ERROR", "Couldn't reach the AI interviewer. Try again."
        ) from None
    if r.status_code == 429:
        raise AppError(
            429,
            "AI_PROVIDER_RATE_LIMITED",
            "The AI interviewer is busy right now. Wait a minute and try again.",
        )
    if r.is_error:
        log.warning("Gemini token request returned HTTP %d", r.status_code)  # body may echo input
        raise AppError(
            502,
            "AI_PROVIDER_ERROR",
            "The AI interviewer couldn't start. Try again.",
            {"provider_status": r.status_code},
        )
    try:
        token = r.json()["name"]
    except (ValueError, KeyError, TypeError):
        token = None
    if not isinstance(token, str) or not token:
        log.warning("Gemini token reply had no token name")
        raise AppError(502, "AI_PROVIDER_ERROR", "The AI interviewer couldn't start. Try again.")
    return token, model.removeprefix("models/")
