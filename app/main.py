# app/main.py
#
# FastAPI entry point for the Agentic Farm Assistant. Mirrors farm_assistant's app
# setup (lifespan generation semaphore, CORS, /health) but the chat surface is driven
# by the agent controller instead of a linear pipeline.

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.routers.ask import router as ask_router
from app.routers.chat_proxy import router as chat_proxy_router

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

S = get_settings()

logging.basicConfig(
    level=S.LOG_LEVEL.upper(),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("agentic-fa")


@asynccontextmanager
async def lifespan(app: FastAPI):
    limit = int(os.getenv("MAX_ACTIVE_GENERATIONS", "3"))
    cast(Any, app).state.gen_semaphore = asyncio.Semaphore(limit)
    logger.info(f"Agentic Farm Assistant up. Model={S.VLLM_MODEL}, max_active_generations={limit}")
    yield


app = FastAPI(
    title=S.APP_TITLE,
    version=S.APP_VERSION,
    docs_url="/docs" if S.ENABLE_DOCS else None,
    redoc_url="/redoc" if S.ENABLE_REDOC else None,
    openapi_url="/openapi.json" if S.ENABLE_DOCS else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(ask_router)
app.include_router(chat_proxy_router)


@app.get("/health", tags=["Ops"])
async def health():
    return {
        "status": "ok",
        "app": S.APP_TITLE,
        "version": S.APP_VERSION,
        "model": S.VLLM_MODEL,
        "backend": S.CHAT_BACKEND_URL,
        "auto_persist_turns": S.AUTO_PERSIST_TURNS,
        "agent": {
            "max_corrections": S.AGENT_MAX_CORRECTIONS,
            "grade_good": S.AGENT_GRADE_GOOD,
            "llm_grader": S.AGENT_ENABLE_LLM_GRADER,
            "decomposition": S.AGENT_ENABLE_DECOMPOSITION,
        },
    }


# --- Web UI (ported from farm_assistant) ---

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    """Public login page."""
    return (TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")


@app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
def chat_page(request: Request):
    """Chat UI — chat.js redirects to / if there is no token."""
    return templates.TemplateResponse(
        request=request, name="ask_stream.html", context={"FA_ENV": S.FA_ENV}
    )


@app.get("/c/{session_id}", response_class=HTMLResponse, include_in_schema=False)
def chat_page_with_session(session_id: str, request: Request):
    """Chat UI with a path-based chat id (ChatGPT-style URL)."""
    return templates.TemplateResponse(
        request=request, name="ask_stream.html", context={"FA_ENV": S.FA_ENV}
    )
