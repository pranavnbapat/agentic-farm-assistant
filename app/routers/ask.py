# app/routers/ask.py
#
# SSE endpoint that drives the agent controller and relays its event stream.
# Event vocabulary matches farm_assistant (status / grounding / token / sources /
# timing / done / app_error) plus `trace` (agent steps) and `session` (the
# session_uuid a turn was persisted to, esp. when Django auto-created one).
#
# Auth + session:
#   - auth comes from the Authorization header, or (for browser EventSource, which
#     cannot set headers) an `auth` query param on the GET stream.
#   - when session_id + auth are present, the controller loads prior history from
#     Django and persists the completed turn back to chat_session / chat_message.

import json
import logging
from typing import Optional

from fastapi import APIRouter, Request
from sse_starlette.sse import EventSourceResponse

from app.agent.controller import run_turn
from app.schemas import ChatMessageStreamIn

logger = logging.getLogger("agentic-fa.router")
router = APIRouter()


def _bearer(request: Optional[Request], auth_qs: Optional[str]) -> str:
    """Resolve the bearer token from the Authorization header or an `auth` query param."""
    header = request.headers.get("Authorization", "") if request else ""
    if header:
        return header
    if auth_qs:
        return auth_qs if auth_qs.startswith("Bearer ") else f"Bearer {auth_qs}"
    return ""


def _parse_client_history(client_history: Optional[str]) -> list[dict]:
    if not client_history:
        return []
    try:
        parsed = json.loads(client_history)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _stream(
    request: Request,
    *,
    q: str,
    top_k,
    max_tokens,
    temperature,
    model,
    followup_hint,
    history: list[dict],
    session_id: Optional[str],
    auth_token: str,
    pause_personalization: bool = False,
) -> EventSourceResponse:
    sem = getattr(request.app.state, "gen_semaphore", None)

    async def gen():
        async for ev in run_turn(
            user_q=q,
            history_messages=history,
            auth_token=auth_token or None,
            session_id=session_id,
            pause_personalization=pause_personalization,
            top_k=top_k,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
            followup_hint=followup_hint or "",
            semaphore=sem,
        ):
            data = ev["data"]
            if not isinstance(data, str):
                data = json.dumps(data, ensure_ascii=False)
            yield {"event": ev["event"], "data": data}

    return EventSourceResponse(gen(), ping=10)


@router.get("/chatbot/api/chats/message/stream", tags=["Chat"], summary="Stream an agentic answer (new chat)")
@router.get("/chatbot/api/chats/{session_id}/message/stream", tags=["Chat"], summary="Stream an agentic answer (existing chat)")
@router.get("/ask/stream", include_in_schema=False)
async def ask_stream_get(
    q: str,
    session_id: Optional[str] = None,
    top_k: Optional[int] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    model: Optional[str] = None,
    followup_hint: Optional[str] = None,
    client_history: Optional[str] = None,
    auth: Optional[str] = None,
    pause_personalization: bool = False,
    request: Request = None,
):
    """
    SSE stream. With `session_id` + auth, prior history is loaded from Django and the
    completed turn is persisted. `auth` query param is accepted for EventSource clients
    that cannot set the Authorization header.
    """
    return _stream(
        request,
        q=q,
        top_k=top_k,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model,
        followup_hint=followup_hint,
        history=_parse_client_history(client_history),
        session_id=session_id,
        auth_token=_bearer(request, auth),
        pause_personalization=pause_personalization,
    )


@router.post("/chatbot/api/chats/message", tags=["Chat"], summary="Stream an agentic answer (new chat, POST)")
@router.post("/chatbot/api/chats/{session_id}/message", tags=["Chat"], summary="Stream an agentic answer (existing chat, POST)")
async def ask_stream_post(body: ChatMessageStreamIn, request: Request, session_id: Optional[str] = None):
    return _stream(
        request,
        q=body.q,
        top_k=body.top_k,
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        model=body.model,
        followup_hint=body.followup_hint,
        history=list(body.client_history or []),
        session_id=session_id,
        auth_token=_bearer(request, None),
        pause_personalization=body.pause_personalization,
    )
