"""
api.py — FastAPI HTTP server for searching git-ingested LKML emails.

Start:
    uvicorn api:app --host 0.0.0.0 --port 8001

Endpoints:
    GET /health              — liveness check
    GET /stats               — total email and repo counts
    GET /repos               — all repos with email counts
    GET /search              — search emails by keyword / sender / date / repo
    GET /email/{email_id}    — fetch one email by its integer ID

Role in the stack:
    mcp_server.py  →  THIS FILE (HTTP on port 8001)  →  PostgreSQL (git schema)
"""

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Query

import os

_DEFAULT_DSN = "postgresql://mailinglist:yourpassword@127.0.0.1/mailinglist"

# ── Connection pool ───────────────────────────────────────────────────────────
# A pool (min 2, max 20 connections) is used instead of a single connection so
# concurrent requests don't block each other.

_pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once on startup: open the pool, set search_path to git schema.
    global _pool
    _pool = await asyncpg.create_pool(
        os.environ.get("LKML_DB_DSN", _DEFAULT_DSN),
        min_size=2,
        max_size=20,
        server_settings={"search_path": "git,public"},
    )
    yield
    # Runs once on shutdown: drain and close all connections.
    await _pool.close()


app = FastAPI(title="LKML Git Email Search", version="1.0", lifespan=lifespan)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> datetime:
    """Convert a YYYY-MM-DD string to a UTC-aware datetime for asyncpg.
    asyncpg requires datetime objects for timestamptz columns — plain strings
    cause a TypeError even when the SQL uses ::timestamptz casting."""
    return datetime.combine(date.fromisoformat(s), datetime.min.time(), tzinfo=timezone.utc)


async def _query(sql: str, *params) -> list[dict]:
    """Acquire a connection from the pool, run a query, return list of dicts."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        return [dict(r) for r in rows]


async def _queryval(sql: str, *params):
    """Acquire a connection from the pool, return a single scalar value."""
    async with _pool.acquire() as conn:
        return await conn.fetchval(sql, *params)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/stats")
async def stats():
    total_emails = await _queryval("SELECT COUNT(*) FROM emails")
    total_repos  = await _queryval("SELECT COUNT(DISTINCT repo) FROM commits")
    return {"total_emails": total_emails, "total_repos": total_repos}


@app.get("/repos")
async def list_repos():
    rows = await _query("""
        SELECT repo, COUNT(*) AS email_count
        FROM commits
        GROUP BY repo
        ORDER BY email_count DESC
    """)
    return {"repos": rows}


@app.get("/search")
async def search(
    q:           Optional[str] = Query(None, description="Full-text search on subject"),
    repo:        Optional[str] = Query(None, description="Filter by repo name (partial)"),
    sender:      Optional[str] = Query(None, description="Filter by sender name (partial)"),
    sender_addr: Optional[str] = Query(None, description="Filter by sender address (partial)"),
    subject:     Optional[str] = Query(None, description="Filter by subject (partial)"),
    date_from:   Optional[str] = Query(None, description="Emails on or after YYYY-MM-DD"),
    date_to:     Optional[str] = Query(None, description="Emails on or before YYYY-MM-DD"),
    limit:       int           = Query(20,   description="Max results (default 20, max 200)"),
):
    limit = min(limit, 200)

    # Build WHERE clause incrementally.
    # Each filter appends a condition string (e.g. "sender ILIKE $2") and its
    # value to params. p tracks the next placeholder number.
    conditions: list[str] = []
    params:     list      = []
    p = 1

    if q:
        # Full-text search on subject using the GIN index (fast).
        # q is added first so it's always $1 — the rank expression below
        # references it as $1 directly without re-adding it to params.
        conditions.append(
            f"to_tsvector('english', subject) @@ plainto_tsquery('english', ${p})"
        )
        params.append(q)
        p += 1

    if sender:
        conditions.append(f"sender ILIKE ${p}")
        params.append(f"%{sender}%")
        p += 1

    if sender_addr:
        conditions.append(f"sender_addr ILIKE ${p}")
        params.append(f"%{sender_addr}%")
        p += 1

    if subject:
        conditions.append(f"subject ILIKE ${p}")
        params.append(f"%{subject}%")
        p += 1

    if repo:
        # Subquery avoids a JOIN that would multiply rows when one email
        # is cross-posted to multiple repos.
        conditions.append(
            f"email_id IN (SELECT email_id FROM commits WHERE repo ILIKE ${p})"
        )
        params.append(f"%{repo}%")
        p += 1

    if date_from:
        conditions.append(f"sent_at >= ${p}")
        params.append(_parse_date(date_from))
        p += 1

    if date_to:
        conditions.append(f"sent_at <= ${p}")
        params.append(_parse_date(date_to))
        p += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    if q:
        # Rank by relevance when a keyword was given.
        # q is already $1 in params — reference it directly, do NOT append it
        # again or it shifts all subsequent $N placeholders and breaks filters.
        rank_expr = ", ts_rank(to_tsvector('english', subject), plainto_tsquery('english', $1)) AS rank"
        order = "ORDER BY sent_at ASC NULLS LAST"
    else:
        rank_expr = ""
        order     = "ORDER BY sent_at DESC NULLS LAST"

    # Append limit last so its placeholder number is always correct.
    params.append(limit)
    sql = f"""
        SELECT email_id, sender, sender_addr, sent_at, subject, body_sha256
               {rank_expr}
        FROM emails
        {where}
        {order}
        LIMIT ${p}
    """
    rows = await _query(sql, *params)
    return {"total_returned": len(rows), "results": rows}


@app.get("/thread")
async def get_thread(
    subject:  str           = Query(..., description="Any email subject from the thread"),
    date_to:  Optional[str] = Query(None, description="Only emails on or before YYYY-MM-DD"),
    limit:    int           = Query(200,  description="Max results (default 200)"),
):
    limit = min(limit, 200)
    # Cutoff is enforced in SQL — nothing after date_to ever leaves the DB.
    if date_to:
        rows = await _query("""
            SELECT email_id, sender, sender_addr, sent_at, subject, body_sha256
            FROM emails
            WHERE md5(regexp_replace(lower(subject), '^(re: )+', ''))
                = md5(regexp_replace(lower($1),      '^(re: )+', ''))
              AND sent_at <= $2
            ORDER BY sent_at ASC NULLS LAST
            LIMIT $3
        """, subject, _parse_date(date_to), limit)
    else:
        rows = await _query("""
            SELECT email_id, sender, sender_addr, sent_at, subject, body_sha256
            FROM emails
            WHERE md5(regexp_replace(lower(subject), '^(re: )+', ''))
                = md5(regexp_replace(lower($1),      '^(re: )+', ''))
            ORDER BY sent_at ASC NULLS LAST
            LIMIT $2
        """, subject, limit)
    return {"total_returned": len(rows), "results": rows}


@app.get("/email/{email_id}")
async def get_email(
    email_id: int,
    cutoff:   Optional[str] = Query(None, description="Reject emails after this date YYYY-MM-DD"),
):
    # If a cutoff is set, add it to the WHERE clause so the DB returns nothing
    # for emails after the cutoff — they appear as 404, not just hidden.
    if cutoff:
        rows = await _query(
            "SELECT * FROM emails WHERE email_id=$1 AND sent_at <= $2",
            email_id, _parse_date(cutoff),
        )
    else:
        rows = await _query("SELECT * FROM emails WHERE email_id=$1", email_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Email not found: {email_id}")
    return rows[0]
