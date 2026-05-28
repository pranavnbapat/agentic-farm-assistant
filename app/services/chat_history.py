# app/services/chat_history.py
#
# Django-backed conversation persistence. Ported/adapted from
# farm_assistant/app/services/chat_history.py:
#   - load_chat_state  : GET /chat/sessions/{id}/  -> prior messages
#   - merge_messages   : merge persisted history with recent client-side turns
#   - log_turn_to_backend : POST /chat/log-turn/   -> writes chat_session + chat_message
#
# The Django side (euf.views.fastapi.UserChatV.log_chat_turn) stores
# user_message + assistant_message + a flexible `meta` dict (lands in
# ChatMessage.extra), and auto-creates a ChatSession when session_uuid is absent.
# So agentic answers persist into the same chat_session / chat_message tables with
# no Django changes.

import base64
import json
import logging
from typing import Optional, Any

import httpx

from app.config import get_settings

S = get_settings()
logger = logging.getLogger("agentic-fa.chat_history")

CHAT_BACKEND_URL = (S.CHAT_BACKEND_URL or "").rstrip("/")


def extract_user_uuid_from_token(auth_token: Optional[str]) -> Optional[str]:
    """Best-effort decode of the JWT payload for logging/scoping (no verification)."""
    if not auth_token or not auth_token.startswith("Bearer "):
        return None
    token = auth_token[7:]
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        data = json.loads(base64.urlsafe_b64decode(payload))
        uid = data.get("uuid") or data.get("user_id") or data.get("sub")
        return str(uid) if uid else None
    except Exception:
        return None


async def load_chat_state(session_id: Optional[str], auth_token: Optional[str] = None) -> dict:
    """Load prior messages for a session from Django."""
    if not session_id or not CHAT_BACKEND_URL:
        return {"messages": [], "llm_context": None}

    url = f"{CHAT_BACKEND_URL}/chat/sessions/{session_id}/"
    timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
    headers = {"Authorization": auth_token} if auth_token else {}

    async with httpx.AsyncClient(timeout=timeout, verify=S.VERIFY_SSL) as client:
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json() or {}
            msgs = data.get("messages", [])
            logger.info(f"Loaded {len(msgs)} messages from session {session_id[:8]}...")
            return {"messages": msgs, "llm_context": None}
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                logger.warning(f"Auth failed loading history (HTTP {e.response.status_code}); continuing without history.")
            else:
                logger.warning(f"Failed to load chat state: HTTP {e.response.status_code}")
            return {"messages": [], "llm_context": None}
        except httpx.HTTPError as e:
            logger.warning(f"Failed to load chat state: {e}; continuing without history.")
            return {"messages": [], "llm_context": None}


def merge_messages(
    backend_messages: list[dict] | None,
    client_messages: list[dict] | None,
) -> list[dict]:
    """Merge persisted session messages with recent client-side turns (ported)."""
    merged = [m for m in (backend_messages or []) if isinstance(m, dict)]
    recent = [m for m in (client_messages or []) if isinstance(m, dict)]
    if not recent:
        return merged

    existing_pairs = [
        ((m.get("role") or "user").strip().lower(), (m.get("content") or "").strip())
        for m in merged
        if (m.get("content") or "").strip()
    ]

    scan_start = 0
    for msg in recent:
        role = (msg.get("role") or "user").strip().lower()
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        pair = (role, content)
        found_at = -1
        for idx in range(scan_start, len(existing_pairs)):
            if existing_pairs[idx] == pair:
                found_at = idx
                break
        if found_at >= 0:
            scan_start = found_at + 1
            continue
        merged.append({"role": role, "content": content})
        existing_pairs.append(pair)
        scan_start = len(existing_pairs)

    return merged


async def log_turn_to_backend(
    *,
    session_id: Optional[str],
    user_message: str,
    assistant_message: str,
    meta: dict[str, Any],
    auth_token: Optional[str],
) -> Optional[str]:
    """
    Persist a completed turn to Django (chat_session + chat_message). Returns the
    session_uuid the turn was written to (newly created when session_id is None),
    or None on failure. Mirrors farm_assistant's /chat/log-turn payload shape.
    """
    if not CHAT_BACKEND_URL or not auth_token or not assistant_message.strip():
        return session_id

    url = f"{CHAT_BACKEND_URL}/chat/log-turn/"
    payload: dict[str, Any] = {
        "user_message": user_message,
        "assistant_message": assistant_message,
        "meta": meta or {},
    }
    if session_id:
        payload["session_uuid"] = session_id

    timeout = httpx.Timeout(connect=5.0, read=8.0, write=8.0, pool=5.0)
    headers = {"Authorization": auth_token, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=timeout, verify=S.VERIFY_SSL) as client:
        try:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            try:
                body = r.json()
            except ValueError:
                body = {}
            # Django returns the session_uuid (esp. when it auto-created one).
            new_sid = (
                body.get("session_uuid")
                or (body.get("session") or {}).get("session_uuid")
                or session_id
            )
            logger.info(f"Persisted turn to session {str(new_sid)[:8]}...")
            return str(new_sid) if new_sid else session_id
        except httpx.HTTPStatusError as e:
            logger.warning(f"log-turn failed: HTTP {e.response.status_code} {(e.response.text or '')[:200]}")
            return session_id
        except httpx.HTTPError as e:
            logger.warning(f"log-turn failed: {e}")
            return session_id
