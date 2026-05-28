# app/clients/vllm_client.py
#
# Adapted from farm_assistant/app/clients/vllm_client.py.
# Kept: generate_once (one-shot helpers — routing, grading, query rewrite, titles)
#       and stream_generate (the SSE answer stream).
# Dropped: vision generation (not used by the agentic core yet).

import httpx
import json
import logging

from typing import Dict, Any, AsyncGenerator, Optional, List

from app.config import get_settings


logger = logging.getLogger("agentic-fa.vllm")
S = get_settings()
logger.info(f"vLLM client initialized with URL: {S.VLLM_URL}, Model: {S.VLLM_MODEL}")
_transport = httpx.AsyncHTTPTransport(http2=False, retries=0)


def _build_headers(api_key: str | None = None) -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Connection": "keep-alive"}
    resolved_api_key = api_key if api_key is not None else S.VLLM_API_KEY
    if resolved_api_key:
        headers["Authorization"] = f"Bearer {resolved_api_key}"
    return headers


def _wrap_prompt_as_messages(prompt: str) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": prompt},
    ]


def build_gen_payload(
    prompt: str,
    temperature: float,
    max_tokens: int,
    model: str | None = None,
    messages: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model or S.VLLM_MODEL,
        "messages": messages if messages is not None else _wrap_prompt_as_messages(prompt),
        "temperature": temperature,
        "top_p": 0.9,
    }
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens
    return payload


def build_stream_payload(
    prompt: str,
    temperature: float,
    max_tokens: int,
    model: str | None = None,
    messages: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    payload = build_gen_payload(prompt, temperature, max_tokens, model, messages=messages)
    payload["stream"] = True
    return payload


async def generate_once(
    prompt: str,
    temperature: float,
    max_tokens: int,
    model: str | None = None,
    messages: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Non-streaming one-shot generation (used by the agent's decision hops)."""
    timeout = httpx.Timeout(connect=30.0, read=300.0, write=1800.0, pool=None)
    url = f"{S.VLLM_URL}/v1/chat/completions"

    async with httpx.AsyncClient(
        timeout=timeout,
        verify=S.VERIFY_SSL,
        transport=_transport,
        headers=_build_headers(),
        trust_env=False,
    ) as client:
        r = await client.post(
            url,
            json=build_gen_payload(prompt, temperature, max_tokens, model, messages=messages),
        )
        r.raise_for_status()
        data = r.json()
        if "choices" in data and len(data["choices"]) > 0:
            message = data["choices"][0].get("message", {})
            return message.get("content", "").strip()
        return ""


async def stream_generate(
    prompt: str,
    temperature: float,
    max_tokens: int,
    model: str | None = None,
    messages: Optional[List[Dict[str, str]]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Streaming generation. If `messages` is provided it is used verbatim and
    `prompt` is ignored. Yields {'response': <token>} dicts and a final
    {'done': True} marker.
    """
    timeout = httpx.Timeout(connect=30.0, read=3600.0, write=300.0, pool=None)
    url = f"{S.VLLM_URL}/v1/chat/completions"

    logger.info(f"Starting vLLM stream to {url}, model: {model or S.VLLM_MODEL}")

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            verify=S.VERIFY_SSL,
            transport=_transport,
            headers=_build_headers(),
            trust_env=False,
        ) as client:
            payload = build_stream_payload(prompt, temperature, max_tokens, model, messages=messages)

            async with client.stream("POST", url, json=payload) as r:
                logger.info(f"vLLM response status: {r.status_code}")
                r.raise_for_status()

                token_count = 0
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        logger.info(f"Stream completed, tokens received: {token_count}")
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse SSE data: {e}, data: {data_str[:100]}")
                        continue
                    if "choices" in data and len(data["choices"]) > 0:
                        delta = data["choices"][0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            token_count += 1
                            yield {"response": content}

                yield {"done": True, "done_reason": "stop", "response": ""}
    except httpx.ConnectError as e:
        logger.error(f"Cannot connect to vLLM at {url}: {e}")
        raise
    except httpx.HTTPStatusError as e:
        logger.error(f"vLLM HTTP error {e.response.status_code}: {e.response.text[:500]}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in vLLM streaming: {e}")
        raise
