# app/routers/chat_proxy.py
#
# Auth + chat-session persistence surface, ported from farm_assistant/app/main.py.
# Everything proxies to the Django backend (euf app), so agentic_farm_assistant uses
# the exact same login/logout and chat_session/chat_message tables as farm_assistant.
#
# Endpoints (same paths as farm_assistant's public surface):
#   POST   /chatbot/api/auth/login
#   POST   /chatbot/api/auth/logout
#   GET    /chatbot/api/chats
#   POST   /chatbot/api/chats
#   GET    /chatbot/api/chats/{session_id}
#   PATCH  /chatbot/api/chats/{session_id}
#   DELETE /chatbot/api/chats/{session_id}
#   POST   /chatbot/api/chats/log-turn
#   POST   /chatbot/api/chats/{session_id}/log-turn
#   POST   /chatbot/api/chats/{session_id}/message/{message_id}/feedback

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.schemas import (
    ChatSessionCreateIn,
    ChatSessionPatchIn,
    ChatTurnLogIn,
    LoginBody,
    LogoutIn,
    MessageFeedbackIn,
)
from app.services.backend_client import (
    auth_header,
    chat_backend_headers,
    proxy_json_request,
    relay_upstream_response,
    require_backend,
)

logger = logging.getLogger("agentic-fa.chat_proxy")
S = get_settings()
router = APIRouter()


# --------------------------- Auth ---------------------------

@router.post("/chatbot/api/auth/login", tags=["Authentication"], summary="Authorize a user")
@router.post("/api/login", include_in_schema=False)
async def api_login(body: LoginBody):
    login_url = S.login_url()
    headers = {"Content-Type": "application/json"}
    if S.ADMIN_API_TOKEN:
        headers["Authorization"] = f"Bearer {S.ADMIN_API_TOKEN}"

    logger.info(f"Login attempt for {body.email} -> {login_url}")
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=S.VERIFY_SSL) as client:
            upstream = await client.post(login_url, json=body.model_dump(), headers=headers)
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=503, detail=f"Auth backend unavailable at {login_url}: {exc}")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Unable to reach auth backend: {exc}")
    return relay_upstream_response(upstream)


@router.post("/chatbot/api/auth/logout", tags=["Authentication"], summary="Logout the current user")
async def api_logout(body: LogoutIn):
    require_backend()
    return await proxy_json_request("POST", S.logout_url(), json_body=body.model_dump())


# --------------------------- Chat sessions ---------------------------

@router.get("/chatbot/api/chats", tags=["Chats"], summary="List chat sessions")
async def api_list_sessions(request: Request):
    base = require_backend()
    return await proxy_json_request("GET", f"{base}/chat/sessions/", headers=chat_backend_headers(request))


@router.post("/chatbot/api/chats", tags=["Chats"], summary="Create chat session")
async def api_create_session(body: ChatSessionCreateIn, request: Request):
    base = require_backend()
    return await proxy_json_request(
        "POST", f"{base}/chat/sessions/", headers=chat_backend_headers(request), json_body=body.model_dump()
    )


@router.get("/chatbot/api/chats/{session_id}", tags=["Chats"], summary="Get a chat session (with messages)")
async def api_get_session(session_id: str, request: Request):
    base = require_backend()
    return await proxy_json_request("GET", f"{base}/chat/sessions/{session_id}/", headers=chat_backend_headers(request))


@router.patch("/chatbot/api/chats/{session_id}", tags=["Chats"], summary="Update chat metadata/title")
async def api_patch_session(session_id: str, body: ChatSessionPatchIn, request: Request):
    base = require_backend()
    return await proxy_json_request(
        "PATCH",
        f"{base}/chat/sessions/{session_id}/",
        headers=chat_backend_headers(request),
        json_body=body.model_dump(exclude_none=True),
    )


@router.delete("/chatbot/api/chats/{session_id}", tags=["Chats"], summary="Delete a chat session")
async def api_delete_session(session_id: str, request: Request):
    base = require_backend()
    return await proxy_json_request("DELETE", f"{base}/chat/sessions/{session_id}/", headers=chat_backend_headers(request))


# --------------------------- Turn logging / feedback ---------------------------

@router.post("/chatbot/api/chats/log-turn", tags=["Chats"], summary="Store a completed chat turn")
async def api_log_turn(body: ChatTurnLogIn, request: Request):
    base = require_backend()
    return await proxy_json_request(
        "POST", f"{base}/chat/log-turn/", headers=chat_backend_headers(request), json_body=body.model_dump()
    )


@router.post("/chatbot/api/chats/{session_id}/log-turn", tags=["Chats"], summary="Store a completed turn for a chat")
async def api_log_turn_for_session(session_id: str, body: ChatTurnLogIn, request: Request):
    payload = body.model_dump()
    payload["session_uuid"] = session_id
    return await api_log_turn(ChatTurnLogIn(**payload), request)


@router.post(
    "/chatbot/api/chats/{session_id}/message/{message_id}/feedback",
    tags=["Chats"],
    summary="Add feedback to a message",
)
async def api_message_feedback(session_id: str, message_id: str, body: MessageFeedbackIn, request: Request):
    base = require_backend()
    url = f"{base}/chat/sessions/{session_id}/message/{message_id}/feedback/"
    return await proxy_json_request("POST", url, headers=chat_backend_headers(request), json_body=body.model_dump())


# --------------------------- User memory (chat.js memory modal) ---------------------------

@router.get("/chatbot/api/users/me/memory", tags=["User Profile"], summary="List memory notes")
async def api_get_memory(request: Request, limit: int = 20):
    base = require_backend()
    return await proxy_json_request(
        "GET", f"{base}/chat/user/memory/", headers=auth_header(request), params={"limit": limit}
    )


@router.post("/chatbot/api/users/me/memory", tags=["User Profile"], summary="Add a memory note")
async def api_add_memory(request: Request):
    base = require_backend()
    return await proxy_json_request(
        "POST", f"{base}/chat/user/memory/", headers=auth_header(request), json_body=await request.json()
    )


@router.delete("/chatbot/api/users/me/memory/{note_id}", tags=["User Profile"], summary="Delete a memory note")
async def api_delete_memory(note_id: int, request: Request):
    base = require_backend()
    return await proxy_json_request("DELETE", f"{base}/chat/user/memory/{int(note_id)}/", headers=auth_header(request))


# --------------------------- Legacy /proxy/* aliases used by chat.js ---------------------------

@router.post("/proxy/logout/", include_in_schema=False)
async def proxy_logout(request: Request):
    require_backend()
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await proxy_json_request("POST", S.logout_url(), json_body=body)


@router.get("/proxy/chat/sessions/", include_in_schema=False)
async def proxy_list_sessions(request: Request):
    base = require_backend()
    return await proxy_json_request("GET", f"{base}/chat/sessions/", headers=chat_backend_headers(request))


@router.post("/proxy/chat/sessions/", include_in_schema=False)
async def proxy_create_session(request: Request):
    base = require_backend()
    return await proxy_json_request(
        "POST", f"{base}/chat/sessions/", headers=chat_backend_headers(request), json_body=await request.json()
    )


@router.get("/proxy/chat/sessions/{session_id}/", include_in_schema=False)
async def proxy_get_session(session_id: str, request: Request):
    base = require_backend()
    return await proxy_json_request("GET", f"{base}/chat/sessions/{session_id}/", headers=chat_backend_headers(request))


@router.patch("/proxy/chat/sessions/{session_id}/", include_in_schema=False)
async def proxy_patch_session(session_id: str, request: Request):
    base = require_backend()
    return await proxy_json_request(
        "PATCH", f"{base}/chat/sessions/{session_id}/", headers=chat_backend_headers(request), json_body=await request.json()
    )


@router.delete("/proxy/chat/sessions/{session_id}/", include_in_schema=False)
async def proxy_delete_session(session_id: str, request: Request):
    base = require_backend()
    return await proxy_json_request("DELETE", f"{base}/chat/sessions/{session_id}/", headers=chat_backend_headers(request))


@router.post("/proxy/chat/log-turn/", include_in_schema=False)
async def proxy_log_turn(request: Request):
    base = require_backend()
    return await proxy_json_request(
        "POST", f"{base}/chat/log-turn/", headers=chat_backend_headers(request), json_body=await request.json()
    )
