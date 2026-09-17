from __future__ import annotations

from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, Query, Response, status

from . import ledger
from .db import init_db
from .schemas import (
    EventIn,
    EventOut,
    LeaderboardChangesOut,
    LeaderboardOut,
    LeaderboardPageOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="League Points Ledger",
    version="1.0.0",
    description=(
        "Immutable points-event ledger with idempotent writes, "
        "correction chains and revisioned leaderboards."
    ),
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/events",
    response_model=EventOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Idempotent replay: event_id already stored with identical payload"},
        409: {"description": "event_id already exists with a different payload"},
        422: {"description": "Invalid correction reference"},
    },
)
def post_event(ev: EventIn, response: Response) -> EventOut:
    event, created = ledger.create_event(ev)
    if not created:
        response.status_code = status.HTTP_200_OK
    return event


@app.get("/events", response_model=List[EventOut])
def get_events() -> List[EventOut]:
    return ledger.list_events()


@app.get("/events/{event_id}", response_model=EventOut)
def get_event(event_id: str) -> EventOut:
    return ledger.get_event(event_id)


@app.get("/leaderboard", response_model=LeaderboardOut)
def get_leaderboard(
    revision: Optional[int] = Query(
        default=None, description="Revision to query; defaults to the latest."
    )
) -> LeaderboardOut:
    return ledger.leaderboard(revision)


@app.get(
    "/leaderboard/changes",
    response_model=LeaderboardChangesOut,
    responses={
        404: {"description": "Revision beyond the current maximum"},
        422: {"description": "Missing/non-integer params, or invalid revision ordering"},
    },
)
def get_leaderboard_changes(
    from_revision: int = Query(
        description="Earlier revision; 0 means the empty leaderboard.",
        ge=0,
    ),
    to_revision: int = Query(
        description="Later revision; must be greater than from_revision.",
        ge=0,
    ),
) -> LeaderboardChangesOut:
    return ledger.leaderboard_changes(from_revision, to_revision)


@app.get(
    "/leaderboard/page",
    response_model=LeaderboardPageOut,
    responses={
        404: {"description": "Revision not found"},
        422: {"description": "Invalid limit, or malformed/tampered/conflicting cursor"},
    },
)
def get_leaderboard_page(
    revision: Optional[int] = Query(
        default=None,
        description="Revision to query; defaults to the latest. When a cursor is "
        "supplied, an explicit revision must match the cursor's revision.",
    ),
    limit: int = Query(default=50, ge=1, le=500, description="Maximum entries per page."),
    cursor: Optional[str] = Query(
        default=None,
        description="Signed cursor from a previous page; pins the snapshot revision.",
    ),
) -> LeaderboardPageOut:
    return ledger.leaderboard_page(revision, limit, cursor)
