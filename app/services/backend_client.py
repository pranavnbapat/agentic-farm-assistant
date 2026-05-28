# app/services/backend_client.py
#
# Thin HTTP proxy helpers for talking to the Django backend, adapted from
# farm_assistant/app/main.py. Used by the chat_proxy router so the browser/API
# client never talks to Django directly (avoids CORS and centralises auth).

import logging
from typing import Any, Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import get_settings

S = get_settings()
logger = logging.getLogger("agentic-fa.backend")


def chat_backend_headers(request: Request) -> dict[str, str]:
    return {
        "Authorization": request.headers.get("Authorization", ""),
        "X-Refresh-Token": request.headers.get("X-Refresh-Token", ""),
    }


def auth_header(request: Request) -> dict[str, str]:
    return {"Authorization": request.headers.get("Authorization", "")}


def relay_upstream_response(upstream: httpx.Response) -> JSONResponse:
    try:
        body = upstream.json()
    except ValueError:
        body = {
            "status": "error",
            "message": "Upstream returned non-JSON response",
            "upstream_status": upstream.status_code,
            "upstream_body": (upstream.text or "")[:1000],
        }
    if upstream.is_error:
        logger.warning(f"Upstream proxy error: HTTP {upstream.status_code}, body={str(body)[:300]}")
    return JSONResponse(content=body, status_code=upstream.status_code)


async def proxy_json_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    json_body: Any = None,
    params: Optional[dict[str, Any]] = None,
) -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=10.0, verify=S.VERIFY_SSL) as client:
            upstream = await client.request(method, url, headers=headers, json=json_body, params=params)
            return relay_upstream_response(upstream)
    except httpx.HTTPError as e:
        logger.error(f"Failed to proxy {method} {url}: {e}")
        raise HTTPException(status_code=502, detail=str(e))


def require_backend() -> str:
    if not S.CHAT_BACKEND_URL:
        raise HTTPException(status_code=503, detail="Chat backend not configured")
    return S.CHAT_BACKEND_URL
