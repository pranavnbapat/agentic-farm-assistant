# app/tools/synthesize.py
#
# SYNTHESIS tool. Maps the resolved turn mode onto the reused prompt_service
# builders, then streams tokens from vLLM. Citation hygiene runs afterward in the
# controller via utils.citations.

import logging
from typing import AsyncGenerator, Optional

from app.clients.vllm_client import stream_generate
from app.services import prompt_service

logger = logging.getLogger("agentic-fa.synthesize")


def build_answer_messages(
    mode: str,
    *,
    question: str,
    contexts: list[str],
    history_messages: Optional[list[dict]],
    profile_context: Optional[str],
    has_relevant_sources: bool,
    answer_language: Optional[str] = None,
) -> list[dict]:
    if mode == "off_topic":
        return prompt_service.build_off_topic_messages(question, history_messages, profile_context)
    if mode == "clarification_only":
        return prompt_service.build_clarification_messages(question, history_messages, profile_context)
    if mode == "history_only":
        return prompt_service.build_history_only_messages(question, history_messages, profile_context)
    if mode == "conversation_only":
        return prompt_service.build_conversation_only_messages(question, history_messages, profile_context)
    if mode == "assistant_capabilities":
        return prompt_service.build_capabilities_messages(question, history_messages, profile_context)
    if mode == "general_knowledge":
        return prompt_service.build_general_knowledge_messages(question, history_messages, profile_context, answer_language=answer_language)
    # default: normal grounded synthesis
    return prompt_service.build_messages(
        contexts=contexts or [],
        question=question,
        history_messages=history_messages,
        user_profile_context=profile_context,
        has_relevant_sources=has_relevant_sources,
        answer_language=answer_language,
    )


async def stream_answer(
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    model: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    async for obj in stream_generate(
        "", temperature, max_tokens, model=model, messages=messages
    ):
        chunk = obj.get("response")
        if chunk:
            yield chunk
