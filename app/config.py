# app/config.py
#
# Configuration for agentic_farm_assistant.
# Adapted from farm_assistant/app/config.py (Django-backend bits dropped) plus a
# new `--- Agent loop ---` section that controls the CRAG controller.

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Per-environment Django backend base URLs (same mapping as farm_assistant).
_BACKEND_BY_ENV = {
    "local": "http://127.0.0.1:8000",
    "dev": "https://backend-admin.dev.farmbook.ugent.be",
    "prd": "https://backend-admin.prd.farmbook.ugent.be",
}


class Settings(BaseSettings):
    # --- Environment selector ---
    FA_ENV: str = Field("local")  # "local" | "dev" | "prd"

    # --- App ---
    LOG_LEVEL: str = Field("INFO")
    APP_TITLE: str = "Agentic Farm Assistant"
    APP_VERSION: str = "0.1.0"
    ENABLE_DOCS: bool = True
    ENABLE_REDOC: bool = False

    # --- Backend (Django: auth, chat sessions/messages persistence) ---
    # Explicit override; if empty it is derived from FA_ENV in normalise().
    CHAT_BACKEND_URL: str = ""
    AUTH_BACKEND_URL: str = ""
    ADMIN_API_TOKEN: str = ""
    # When true, the streaming endpoint persists each completed turn to Django
    # (chat_session / chat_message) via /chat/log-turn/. Set false if a frontend
    # logs turns itself (to avoid double-writes).
    AUTO_PERSIST_TURNS: bool = True

    # --- OpenSearch ---
    OPENSEARCH_API_URL: str
    OPENSEARCH_API_USR: str | None = None
    OPENSEARCH_API_PWD: str | None = None
    VERIFY_SSL: bool = True
    OS_API_PATH: str = "/neural_search_relevant"
    OS_RAG_API_PATH: str = "/llm_retrieve"

    # --- LLM / vLLM ---
    VLLM_URL: str = "http://localhost:8000"
    VLLM_MODEL: str = "qwen3-30b-a3b-awq"
    VLLM_API_KEY: str | None = None
    RUNPOD_VLLM_HOST: str = ""

    # --- Generation ---
    MAX_TOKENS: int = 768
    MAX_OUTPUT_TOKENS: int = 768
    MAX_INPUT_TOKENS: int = 3000
    MAX_USER_INPUT_TOKENS: int = 1200
    TEMPERATURE: float = 0.4
    NUM_CTX: int = 4096
    TOP_K: int = 5
    MAX_CONTEXT_CHARS: int = 24000
    RETRIEVAL_CANDIDATE_K: int = 10
    RETRIEVAL_MIN_SCORE: float = 1.0

    # --- Agent loop (CRAG controller) ---
    # Max corrective retrieval iterations after the first search. 0 disables the
    # loop and reproduces farm_assistant's single-pass behaviour.
    AGENT_MAX_CORRECTIONS: int = 2
    # Heuristic relevance (token overlap, 0..1). At or above this the retrieval is
    # accepted as "good"; below it the controller will try a corrective rewrite.
    AGENT_GRADE_GOOD: float = 0.15
    # When the heuristic lands in the borderline band [GRADE_BAD, GRADE_GOOD) and
    # the LLM grader is enabled, ask the model for a second opinion.
    AGENT_GRADE_BAD: float = 0.05
    AGENT_ENABLE_LLM_GRADER: bool = False
    AGENT_LLM_GRADE_PASS: float = 0.5
    # Constraint verification: after retrieval, an LLM checks each surviving source
    # against the SPECIFIC constraints in the question (country/region, time, crop,
    # species, named project) and drops sources that match the topic but miss the
    # constraint (e.g. an Italian practice for an "Irish" question). One LLM call per
    # grounded turn. Fail-open (keeps all sources if the check errors/times out).
    AGENT_ENABLE_CONSTRAINT_FILTER: bool = True
    # When constraint verification drops ALL sources (topic matched but the specific
    # constraint didn't), try one constraint-targeted re-retrieval before falling back to
    # a general answer. One extra retrieval (+ heuristic grade + one verify call), and only
    # when a constraint miss actually empties the sources.
    AGENT_ENABLE_CONSTRAINT_RECOVERY: bool = True
    # Self-RAG-lite: after drafting a grounded answer, an LLM checks whether its claims are
    # actually supported by the cited sources; if not, a short caveat is appended so the
    # answer never overstates what the EU-FarmBook sources support. One LLM call per grounded
    # turn (~1-2s). Set false to skip.
    AGENT_ENABLE_ANSWER_VERIFICATION: bool = True
    # Multi-part question decomposition (planner). Off by default to protect latency.
    AGENT_ENABLE_DECOMPOSITION: bool = False
    AGENT_MAX_SUBQUERIES: int = 3
    # If the final retrieval is still weak after all corrections, drop the weak
    # contexts and answer from general knowledge (matches farm_assistant), instead
    # of grounding the answer in noise.
    AGENT_DROP_WEAK_CONTEXTS: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def login_url(self) -> str:
        """Auth backend login endpoint (explicit override or env-derived)."""
        base = (self.AUTH_BACKEND_URL or self.CHAT_BACKEND_URL or "").rstrip("/")
        return f"{base}/fastapi/login/"

    def logout_url(self) -> str:
        base = (self.CHAT_BACKEND_URL or "").rstrip("/")
        return f"{base}/fastapi/logout/"

    def normalise(self) -> "Settings":
        if self.OPENSEARCH_API_USR == "":
            self.OPENSEARCH_API_USR = None
        if self.OPENSEARCH_API_PWD == "":
            self.OPENSEARCH_API_PWD = None

        if not self.CHAT_BACKEND_URL:
            env = (self.FA_ENV or "local").lower()
            self.CHAT_BACKEND_URL = _BACKEND_BY_ENV.get(env, _BACKEND_BY_ENV["local"])
        self.CHAT_BACKEND_URL = self.CHAT_BACKEND_URL.rstrip("/")
        if self.AUTH_BACKEND_URL:
            self.AUTH_BACKEND_URL = self.AUTH_BACKEND_URL.rstrip("/")

        if self.OPENSEARCH_API_URL:
            self.OPENSEARCH_API_URL = self.OPENSEARCH_API_URL.rstrip("/")

        if self.VLLM_URL == "http://localhost:8000" and self.RUNPOD_VLLM_HOST:
            self.VLLM_URL = self.RUNPOD_VLLM_HOST.rstrip("/")
        elif self.VLLM_URL:
            self.VLLM_URL = self.VLLM_URL.rstrip("/")

        return self


@lru_cache()
def get_settings() -> Settings:
    return Settings().normalise()
