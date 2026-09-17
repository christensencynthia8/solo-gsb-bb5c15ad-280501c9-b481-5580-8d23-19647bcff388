"""Core ledger logic: idempotent immutable writes + revisioned reads."""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Optional, Tuple

from fastapi import HTTPException

from .cursor import decode_cursor, encode_cursor
from .db import read_tx, write_tx
from .schemas import (
    EffectiveEvent,
    EventIn,
    EventOut,
    LeaderboardChangeOut,
    LeaderboardChangesOut,
    LeaderboardEntry,
    LeaderboardOut,
    LeaderboardPageOut,
    RankPosition,
)


def _canonical_payload(ev: EventIn) -> str:
    """Canonical JSON of the client-supplied payload (key order independent)."""
    return json.dumps(ev.model_dump(), sort_keys=True, separators=(",", ":"))


def _row_to_event(row: sqlite3.Row) -> EventOut:
    return EventOut(
        event_id=row["event_id"],
        participant_id=row["participant_id"],
        points=row["points"],
        correction_of=row["correction_of"],
        revision=row["revision"],
        created_at=row["created_at"],
    )


def create_event(ev: EventIn) -> Tuple[EventOut, bool]:
    """Insert an immutable event.

    Returns (event, created). If ``event_id`` already exists:
      * identical payload  -> replay: return the stored event, created=False
      * different payload  -> 409 Conflict
    """
    if ev.correction_of is not None and ev.correction_of == ev.event_id:
        raise HTTPException(
            status_code=422, detail="correction_of must not equal event_id"
        )

    payload = _canonical_payload(ev)

    with write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (ev.event_id,)
        ).fetchone()
        if row is not None:
            if row["payload"] == payload:
                return _row_to_event(row), False
            raise HTTPException(
                status_code=409,
                detail=f"event_id '{ev.event_id}' already exists with a different payload",
            )

        if ev.correction_of is not None:
            target = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (ev.correction_of,)
            ).fetchone()
            if target is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"correction_of references unknown event '{ev.correction_of}'",
                )

        revision = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM events"
        ).fetchone()[0]
        created_at = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        conn.execute(
            "INSERT INTO events (event_id, participant_id, points, correction_of, payload, revision, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.event_id,
                ev.participant_id,
                ev.points,
                ev.correction_of,
                payload,
                revision,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (ev.event_id,)
        ).fetchone()
        return _row_to_event(row), True


def get_event(event_id: str) -> EventOut:
    with read_tx() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"event '{event_id}' not found")
    return _row_to_event(row)


def list_events() -> list[EventOut]:
    with read_tx() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY revision ASC"
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def current_revision() -> int:
    with read_tx() as conn:
        return conn.execute("SELECT COALESCE(MAX(revision), 0) FROM events").fetchone()[0]


def _resolve_revision(conn: sqlite3.Connection, revision: Optional[int]) -> int:
    max_rev = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM events"
    ).fetchone()[0]
    if revision is None:
        return max_rev
    if revision < 1 or revision > max_rev:
        raise HTTPException(status_code=404, detail=f"revision {revision} not found")
    return revision


def _effective_rows(conn: sqlite3.Connection, revision: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.event_id, e.participant_id, e.points, e.revision, e.correction_of
        FROM events e
        WHERE e.revision <= ?
          AND NOT EXISTS (
                SELECT 1 FROM events c
                WHERE c.correction_of = e.event_id
                  AND c.revision <= ?
          )
        ORDER BY e.participant_id ASC, e.revision ASC
        """,
        (revision, revision),
    ).fetchall()


def _aggregate(
    rows: list[sqlite3.Row],
) -> Tuple[list[Tuple[str, int]], dict[str, list[EffectiveEvent]]]:
    """Per-participant totals ordered by (-total_points, participant_id)."""
    totals: dict[str, int] = {}
    details: dict[str, list[EffectiveEvent]] = {}
    for r in rows:
        pid = r["participant_id"]
        totals[pid] = totals.get(pid, 0) + r["points"]
        details.setdefault(pid, []).append(
            EffectiveEvent(
                event_id=r["event_id"],
                points=r["points"],
                revision=r["revision"],
                correction_of=r["correction_of"],
            )
        )
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered, details


def leaderboard(revision: Optional[int] = None) -> LeaderboardOut:
    """Leaderboard as of ``revision`` (default: latest).

    An event is *effective* at revision R iff it exists at R (revision <= R)
    and no event with revision <= R corrects it. Hence, for every correction
    chain, only the chain tip that existed at R is counted.
    """
    with read_tx() as conn:
        revision = _resolve_revision(conn, revision)
        rows = _effective_rows(conn, revision)

    ordered, details = _aggregate(rows)
    entries = [
        LeaderboardEntry(
            rank=i + 1,
            participant_id=pid,
            total_points=total,
            effective_events=details[pid],
        )
        for i, (pid, total) in enumerate(ordered)
    ]
    return LeaderboardOut(revision=revision, entries=entries)


def _snapshot(conn: sqlite3.Connection, revision: int) -> dict[str, RankPosition]:
    """``{participant_id: (rank, total_points)}`` as of ``revision``.

    ``revision == 0`` denotes the empty leaderboard. The effective-event
    rule is the same one used by ``/leaderboard``: at revision R an event
    counts iff it exists at R and no correction existing at R supersedes
    it, so a correction that moves an event from participant A to B makes
    A disappear and B appear in the same snapshot.
    """
    if revision == 0:
        return {}
    ordered, _details = _aggregate(_effective_rows(conn, revision))
    return {
        pid: RankPosition(rank=i + 1, total_points=total)
        for i, (pid, total) in enumerate(ordered)
    }


def leaderboard_changes(
    from_revision: int, to_revision: int
) -> LeaderboardChangesOut:
    """Participants whose total points or rank differ between two revisions.

    Validation order: ordering errors (negative, ``from >= to``) are 422;
    revisions beyond the current maximum are 404. ``from_revision == 0``
    compares against the empty leaderboard.
    """
    if from_revision < 0 or from_revision >= to_revision:
        raise HTTPException(
            status_code=422,
            detail=(
                "require 0 <= from_revision < to_revision, got "
                f"from_revision={from_revision}, to_revision={to_revision}"
            ),
        )

    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]
        if to_revision > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {to_revision} not found"
            )
        before_map = _snapshot(conn, from_revision)
        after_map = _snapshot(conn, to_revision)

    changes: list[LeaderboardChangeOut] = []
    for pid in sorted(set(before_map) | set(after_map)):
        before = before_map.get(pid)
        after = after_map.get(pid)
        before_total = before.total_points if before is not None else 0
        after_total = after.total_points if after is not None else 0
        if before != after:  # rank or total_points changed (covers entry/exit)
            changes.append(
                LeaderboardChangeOut(
                    participant_id=pid,
                    before=before,
                    after=after,
                    points_delta=after_total - before_total,
                )
            )
    return LeaderboardChangesOut(
        from_revision=from_revision,
        to_revision=to_revision,
        changes=changes,
    )


def leaderboard_page(
    revision: Optional[int], limit: int, cursor: Optional[str]
) -> LeaderboardPageOut:
    """One page of the leaderboard, resumable via a signed cursor.

    The first request pins ``revision`` (default: latest). The returned
    ``next_cursor`` binds that revision and the last sort key
    ``(total_points, participant_id)``; follow-up requests continue from
    the cursor, so concurrent writers cannot shift or tear the snapshot.
    """
    after: Optional[Tuple[int, str]] = None
    if cursor is not None:
        payload = decode_cursor(cursor)
        if revision is not None and revision != payload.revision:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"explicit revision {revision} conflicts with "
                    f"cursor revision {payload.revision}"
                ),
            )
        revision = payload.revision
        after = (payload.total_points, payload.participant_id)

    with read_tx() as conn:
        revision = _resolve_revision(conn, revision)
        rows = _effective_rows(conn, revision)

    ordered, details = _aggregate(rows)

    start = 0
    if after is not None:
        keys = [(-total, pid) for pid, total in ordered]
        start = bisect_right(keys, (-after[0], after[1]))

    page = ordered[start : start + limit]
    entries = [
        LeaderboardEntry(
            rank=start + i + 1,
            participant_id=pid,
            total_points=total,
            effective_events=details[pid],
        )
        for i, (pid, total) in enumerate(page)
    ]
    has_more = start + limit < len(ordered)
    next_cursor = (
        encode_cursor(revision, page[-1][1], page[-1][0]) if has_more else None
    )
    return LeaderboardPageOut(revision=revision, entries=entries, next_cursor=next_cursor)
