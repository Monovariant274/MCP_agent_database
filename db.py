"""
db.py — Async database layer for the LKML git-based email ingestion system.

Tables live in the 'git' schema inside the existing 'mailinglist' database,
so they don't conflict with the old emails/emails_new/emails_dedup tables.

Usage:
    db = LkmlDB()
    await db.connect()
    email_id = await db.insert_email(...)
    await db.close()
"""

import os
from datetime import datetime
from typing import Optional

import asyncpg
from pydantic import BaseModel

# DSN is read from the environment if set, otherwise falls back to the default.
# Set LKML_DB_DSN to override without editing this file.
DSN = os.environ.get(
    "LKML_DB_DSN",
    "postgresql://mailinglist:yourpassword@127.0.0.1/mailinglist",
)


# ── Pydantic models ───────────────────────────────────────────────────────────
# These are used to validate and type the data coming out of the DB.

class LkmlEmail(BaseModel):
    email_id:    Optional[int]      = None
    sender:      str
    sender_addr: str
    sent_at:     Optional[datetime] = None
    subject:     str
    body:        str
    body_sha256: str


class LkmlCommit(BaseModel):
    repo:          str
    git_commit_id: str
    email_id:      int


# ── Database class ────────────────────────────────────────────────────────────

class LkmlDB:
    def __init__(self, dsn: str = DSN):
        self.dsn  = dsn
        self.conn: Optional[asyncpg.Connection] = None

    async def connect(self) -> None:
        self.conn = await asyncpg.connect(self.dsn)
        # All tables live in the 'git' schema; set search_path so queries
        # can use bare table names without the 'git.' prefix.
        await self.conn.execute("CREATE SCHEMA IF NOT EXISTS git")
        await self.conn.execute("SET search_path TO git, public")
        await self._create_tables()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()
            self.conn = None

    # ── Schema setup ──────────────────────────────────────────────────────────

    async def _create_tables(self) -> None:
        # emails — one row per unique email (deduplicated by content hash).
        # sent_at is nullable because some emails have malformed/missing dates.
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS emails (
                email_id    SERIAL      PRIMARY KEY,
                sender      TEXT        NOT NULL DEFAULT '',
                sender_addr TEXT        NOT NULL DEFAULT '',
                sent_at     TIMESTAMPTZ,
                subject     TEXT        NOT NULL DEFAULT '',
                body        TEXT        NOT NULL DEFAULT '',
                body_sha256 TEXT        NOT NULL DEFAULT ''
            )
        """)

        # commits — links each git commit in a repo to the email it added.
        # One commit usually adds one email, but the UNIQUE constraint on all
        # three columns handles the rare case of a commit touching multiple files.
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS commits (
                repo          TEXT    NOT NULL,
                git_commit_id TEXT    NOT NULL,
                email_id      INTEGER NOT NULL REFERENCES emails(email_id),
                UNIQUE (repo, git_commit_id, email_id)
            )
        """)

        # repo_checkpoints — stores the last processed commit SHA per repo.
        # The ingestor reads this on startup to skip already-processed commits,
        # making daily runs incremental and safe to resume after a crash.
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS repo_checkpoints (
                repo           TEXT PRIMARY KEY,
                last_commit_id TEXT NOT NULL
            )
        """)

        # ── Indexes ───────────────────────────────────────────────────────────

        # Unique deduplication index. Uses md5(subject) instead of subject
        # directly to avoid the B-tree row size limit (~2704 bytes) that very
        # long subjects would exceed.
        # NULLS NOT DISTINCT: two rows where sent_at IS NULL are treated as
        # equal, which is correct — they're the same email with a missing date.
        await self.conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS emails_unique
                ON emails (sender, sender_addr, sent_at, md5(subject), body_sha256)
                NULLS NOT DISTINCT
        """)

        # Indexes for common filter patterns used by api.py
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS emails_sender_idx    ON emails (sender)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS emails_sent_at_idx   ON emails (sent_at)"
        )
        # GIN full-text index: powers the ?q= keyword search in api.py
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS emails_subject_fts   ON emails "
            "USING GIN (to_tsvector('english', subject))"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS commits_repo_idx     ON commits (repo)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS commits_email_id_idx ON commits (email_id)"
        )

    # ── Core write operations ─────────────────────────────────────────────────

    async def check_email(
        self,
        sender:      str,
        sender_addr: str,
        sent_at:     Optional[datetime],
        subject:     str,
        body_sha256: str,
    ) -> Optional[int]:
        """
        Return email_id if this email already exists in the DB, else None.

        Uses md5(subject) = md5($4) so this query can use the emails_unique
        index instead of doing a full table scan.
        """
        row = await self.conn.fetchrow(
            """SELECT email_id FROM emails
               WHERE sender=$1 AND sender_addr=$2
                 AND sent_at IS NOT DISTINCT FROM $3
                 AND md5(subject) = md5($4)
                 AND body_sha256=$5""",
            sender, sender_addr, sent_at, subject, body_sha256,
        )
        return row["email_id"] if row else None

    async def insert_email(
        self,
        sender:      str,
        sender_addr: str,
        sent_at:     Optional[datetime],
        subject:     str,
        body_sha256: str,
        body:        str,
    ) -> int:
        """
        Insert a new email and return its email_id.
        On conflict (same sender/date/subject/body), updates body in case
        of a previous partial write and returns the existing email_id.
        """
        row = await self.conn.fetchrow(
            """INSERT INTO emails (sender, sender_addr, sent_at, subject, body, body_sha256)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (sender, sender_addr, sent_at, md5(subject), body_sha256)
               DO UPDATE SET body = EXCLUDED.body
               RETURNING email_id""",
            sender, sender_addr, sent_at, subject, body, body_sha256,
        )
        return row["email_id"]

    async def insert_commit(
        self,
        repo:          str,
        git_commit_id: str,
        email_id:      int,
    ) -> None:
        """Record the mapping from a git commit to the email it contains."""
        await self.conn.execute(
            """INSERT INTO commits (repo, git_commit_id, email_id)
               VALUES ($1, $2, $3)
               ON CONFLICT DO NOTHING""",
            repo, git_commit_id, email_id,
        )

    # ── Checkpoint helpers ────────────────────────────────────────────────────

    async def get_checkpoint(self, repo: str) -> Optional[str]:
        """Return the last processed commit SHA for this repo, or None if new."""
        row = await self.conn.fetchrow(
            "SELECT last_commit_id FROM repo_checkpoints WHERE repo=$1", repo
        )
        return row["last_commit_id"] if row else None

    async def set_checkpoint(self, repo: str, commit_id: str) -> None:
        """Upsert the checkpoint for a repo after successfully processing a commit."""
        await self.conn.execute(
            """INSERT INTO repo_checkpoints (repo, last_commit_id) VALUES ($1, $2)
               ON CONFLICT (repo) DO UPDATE SET last_commit_id = EXCLUDED.last_commit_id""",
            repo, commit_id,
        )

    # ── Search / read operations (used by api.py) ─────────────────────────────

    async def find_emails(
        self,
        sender:        Optional[str]      = None,
        sender_addr:   Optional[str]      = None,
        subject:       Optional[str]      = None,
        subject_query: Optional[str]      = None,
        repo:          Optional[str]      = None,
        before_dt:     Optional[datetime] = None,
        after_dt:      Optional[datetime] = None,
        limit:         int                = 20,
    ) -> list[LkmlEmail]:
        """
        Search emails with optional filters.
        subject_query: full-text keyword search on subject (uses GIN index).
        subject:       ILIKE substring match on the raw subject text (slower).
        repo:          filter by repo name (partial match).
        before_dt / after_dt: inclusive date range filter on sent_at.
        """
        conditions: list[str] = []
        params:     list      = []
        p = 1  # asyncpg uses $1, $2, ... positional placeholders

        if subject_query:
            # Full-text search using the GIN index — fast even on 6.87M rows
            conditions.append(
                f"to_tsvector('english', subject) @@ plainto_tsquery('english', ${p})"
            )
            params.append(subject_query)
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
            # appears in multiple repos (cross-posting).
            conditions.append(
                f"email_id IN (SELECT email_id FROM commits WHERE repo ILIKE ${p})"
            )
            params.append(f"%{repo}%")
            p += 1

        if before_dt:
            conditions.append(f"sent_at <= ${p}")
            params.append(before_dt)
            p += 1

        if after_dt:
            conditions.append(f"sent_at >= ${p}")
            params.append(after_dt)
            p += 1

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(min(limit, 200))

        sql = f"""
            SELECT email_id, sender, sender_addr, sent_at, subject, body, body_sha256
            FROM emails
            {where}
            ORDER BY sent_at DESC NULLS LAST
            LIMIT ${p}
        """
        rows = await self.conn.fetch(sql, *params)
        return [LkmlEmail(**dict(r)) for r in rows]

    async def get_email_by_id(self, email_id: int) -> Optional[LkmlEmail]:
        row = await self.conn.fetchrow(
            "SELECT * FROM emails WHERE email_id=$1", email_id
        )
        return LkmlEmail(**dict(row)) if row else None

    async def list_repos(self) -> list[dict]:
        rows = await self.conn.fetch("""
            SELECT repo, COUNT(*) AS email_count
            FROM commits
            GROUP BY repo
            ORDER BY email_count DESC
        """)
        return [dict(r) for r in rows]

    async def get_stats(self) -> dict:
        emails = await self.conn.fetchval("SELECT COUNT(*) FROM emails")
        repos  = await self.conn.fetchval("SELECT COUNT(DISTINCT repo) FROM commits")
        return {"total_emails": emails, "total_repos": repos}
