# app/utils/history.py
#
# Minimal conversation helpers. The agentic app is stateless on the server side
# (no Django backend); conversation history arrives from the client per request.

from typing import Optional


def normalize_history(history_messages: Optional[list[dict]]) -> list[dict]:
    """Keep only well-formed user/assistant turns with non-empty content."""
    if not history_messages:
        return []
    out: list[dict] = []
    for m in history_messages:
        if not isinstance(m, dict):
            continue
        role = (m.get("role") or "").strip().lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role in ("user", "you", "human"):
            out.append({"role": "user", "content": content})
        elif role in ("assistant", "model", "ai", "bot"):
            out.append({"role": "assistant", "content": content})
    return out


def format_history(history_messages: Optional[list[dict]], max_chars: int = 4000) -> str:
    """Render history as plain text for the lightweight routing/resolve hops."""
    msgs = normalize_history(history_messages)
    if not msgs:
        return ""
    lines = [f"{m['role'].capitalize()}: {m['content']}" for m in msgs]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def last_assistant_question(history_messages: Optional[list[dict]]) -> str:
    """Last '?'-terminated assistant utterance, for follow-up resolution."""
    import re

    for m in reversed(normalize_history(history_messages)):
        if m["role"] != "assistant":
            continue
        content = m["content"]
        if "?" not in content:
            continue
        parts = re.split(r"(?<=[\?\!\.])\s+", content)
        for p in reversed(parts):
            p = p.strip()
            if p.endswith("?"):
                return p[:300]
        return content[:300]
    return ""
