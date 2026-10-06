"""FastAPI app — the single backend service for Phase 1/2.

Run from the repo root so the `services` package resolves:

    uvicorn services.api.main:app --reload

Routes are mounted under `/api/v1` to match architecture doc section 9, even
though Phase 1 only implements a slice of them. The prefix is cheap now and
saves a client-side rewrite in Phase 2.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.agents.llm import llm_mode
from services.api.routes import demo, disruptions
from services.core.config import settings
from services.core.db import create_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("orchestrator")

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_all()
    log.info(
        "Phase 1 up — db=%s llm=%s hub=%s",
        "sqlite (local)" if settings.is_sqlite else "postgres",
        llm_mode(),
        settings.hub_icao,
    )
    if llm_mode() == "stub":
        log.info(
            "No ANTHROPIC_API_KEY: Recovery and Comms use deterministic "
            "rule-based stubs. The loop still runs end to end."
        )
    yield


app = FastAPI(
    title="Disruption Management & Recovery Orchestrator",
    description=(
        "Phase 1: one scenario (Fog at DEL), five agent functions called in "
        "sequence, one human approval gate, polling UI. No auth, no Redis, "
        "no MCP network boundary yet."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(demo.router, prefix=API_PREFIX)
app.include_router(disruptions.router, prefix=API_PREFIX)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {
        "service": "disruption-orchestrator",
        "phase": "1",
        "docs": "/docs",
        "health": f"{API_PREFIX}/health",
    }
