# app/agent/state.py
#
# The agent's working memory for a single turn. Carries the evolving query,
# the evidence gathered, and a step-by-step trace (surfaced over SSE as `trace`
# events and useful for evaluation / debugging).

from dataclasses import dataclass, field
from typing import Any, Optional

from app.schemas import SourceItem


@dataclass
class TraceStep:
    step: str                       # "route" | "plan" | "search" | "grade" | "rewrite" | "synthesize"
    detail: str = ""
    query: Optional[str] = None
    grade_score: Optional[float] = None
    verdict: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"step": self.step}
        if self.detail:
            d["detail"] = self.detail
        if self.query is not None:
            d["query"] = self.query
        if self.grade_score is not None:
            d["grade_score"] = round(self.grade_score, 3)
        if self.verdict is not None:
            d["verdict"] = self.verdict
        return d


@dataclass
class AgentState:
    user_q: str                                  # raw user message
    effective_q: str = ""                        # standalone interpretation (post follow-up resolution)
    prompt_q: str = ""                            # instruction handed to the synthesis prompt
    retrieval_q: str = ""                         # spelling/grammar-normalised search query
    history_messages: list[dict] = field(default_factory=list)
    history_text: str = ""
    profile_context: Optional[str] = None

    mode: str = "normal"
    iterations: int = 0                          # corrective retrievals performed

    contexts: list[str] = field(default_factory=list)
    sources: list[SourceItem] = field(default_factory=list)
    grounding_state: str = "general_fallback"

    trace: list[TraceStep] = field(default_factory=list)

    def add_trace(self, step: TraceStep) -> TraceStep:
        self.trace.append(step)
        return step
