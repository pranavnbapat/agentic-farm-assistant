# app/clients/opensearch_client.py
#
# Copied verbatim from farm_assistant/app/clients/opensearch_client.py.

import httpx
from typing import Optional, Dict
from app.config import get_settings

S = get_settings()


def os_auth() -> Optional[httpx.Auth]:
    if S.OPENSEARCH_API_USR and S.OPENSEARCH_API_PWD:
        return httpx.BasicAuth(S.OPENSEARCH_API_USR, S.OPENSEARCH_API_PWD)
    return None


def os_headers() -> Dict[str, str]:
    return {"accept": "application/json", "Content-Type": "application/json"}


def os_async_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, verify=S.VERIFY_SSL)
