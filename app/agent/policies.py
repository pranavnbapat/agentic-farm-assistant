# app/agent/policies.py
#
# Tunable knobs for the controller, sourced from config. Bundling them in one
# dataclass keeps the controller readable and makes the loop's behaviour easy to
# audit / override per request.

from dataclasses import dataclass

from app.config import get_settings


@dataclass
class AgentPolicies:
    max_corrections: int          # corrective retrieval iterations after the first search
    grade_good: float             # heuristic >= this -> accept retrieval
    grade_bad: float              # heuristic < this -> clearly weak (skip LLM grader)
    enable_llm_grader: bool       # ask the LLM for borderline grades
    llm_grade_pass: float         # LLM relevance >= this -> accept
    enable_constraint_filter: bool  # drop sources that miss the query's hard constraints
    enable_constraint_recovery: bool  # retry toward the constraint if verify empties sources
    enable_answer_verification: bool  # Self-RAG: check the draft answer against its sources
    enable_decomposition: bool    # split multi-part questions into sub-queries
    max_subqueries: int
    drop_weak_contexts: bool      # if still weak after retries, answer from general knowledge

    @classmethod
    def from_settings(cls) -> "AgentPolicies":
        s = get_settings()
        return cls(
            max_corrections=s.AGENT_MAX_CORRECTIONS,
            grade_good=s.AGENT_GRADE_GOOD,
            grade_bad=s.AGENT_GRADE_BAD,
            enable_llm_grader=s.AGENT_ENABLE_LLM_GRADER,
            llm_grade_pass=s.AGENT_LLM_GRADE_PASS,
            enable_constraint_filter=s.AGENT_ENABLE_CONSTRAINT_FILTER,
            enable_constraint_recovery=s.AGENT_ENABLE_CONSTRAINT_RECOVERY,
            enable_answer_verification=s.AGENT_ENABLE_ANSWER_VERIFICATION,
            enable_decomposition=s.AGENT_ENABLE_DECOMPOSITION,
            max_subqueries=s.AGENT_MAX_SUBQUERIES,
            drop_weak_contexts=s.AGENT_DROP_WEAK_CONTEXTS,
        )
