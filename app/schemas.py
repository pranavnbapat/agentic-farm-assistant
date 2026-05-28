# app/schemas.py
#
# Trimmed from farm_assistant/app/schemas.py — only the shapes the agentic app uses.

from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field


class AskIn(BaseModel):
    question: str
    page: Optional[int] = Field(default=None, examples=[1])
    k: Optional[int] = Field(default=None, examples=[5])
    model: Optional[str] = None
    sort_by: Optional[str] = Field(default=None, examples=["score_desc"])
    dev: Optional[bool] = Field(default=False, examples=[False])
    max_tokens: Optional[int] = Field(default=None, examples=[768])
    temperature: Optional[float] = Field(default=None, examples=[0.4])
    top_k: Optional[int] = Field(default=None, examples=[5])


class SourceItem(BaseModel):
    id: Optional[str] = None
    url: Optional[str] = None
    title: Optional[str] = None
    score: Optional[float] = None
    subtitle: Optional[str] = None
    description: Optional[str] = None
    project: Optional[str] = None
    license: Optional[str] = None
    keywords: Optional[list[str]] = None
    topics: Optional[list[str]] = None
    themes: Optional[list[str]] = None
    languages: Optional[list[str]] = None
    creators: Optional[list[str]] = None
    date_of_completion: Optional[str] = None
    display_url: Optional[str] = None
    sid: str | None = None


class ChatMessageStreamIn(BaseModel):
    q: str
    page: int = Field(default=1, examples=[1])
    k: Optional[int] = Field(default=None, examples=[5])
    top_k: Optional[int] = Field(default=None, examples=[5])
    max_tokens: Optional[int] = Field(default=None, examples=[768])
    temperature: Optional[float] = Field(default=None, examples=[0.4])
    model: Optional[str] = None
    followup_hint: Optional[str] = None
    client_history: List[Dict[str, str]] = Field(default_factory=list)
    # When true, don't inject the user's profile/memory for this turn and don't learn
    # from it (the UI's "pause memory" toggle).
    pause_personalization: bool = False


# --- Django-backed auth + chat persistence (parity with farm_assistant) ---

class LoginBody(BaseModel):
    email: str
    password: str


class LogoutIn(BaseModel):
    email: str


class ChatSessionCreateIn(BaseModel):
    title: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChatSessionPatchIn(BaseModel):
    title: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class ChatTurnLogIn(BaseModel):
    session_uuid: Optional[str] = None
    user_message: str
    assistant_message: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class MessageFeedbackIn(BaseModel):
    feedback: str = Field(examples=["up", "down", "none"])
    meta: Dict[str, Any] = Field(default_factory=dict)
