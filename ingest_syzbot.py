"""
ingest_syzbot.py — Populate git.syzbot_bugs from syzbot bug pages.

For each given syzbot bug ID:
  1. Fetch https://syzkaller.appspot.com/bug?id=<bug_id>
  2. Extract all discussion subject lines from the Discussions table on the page
  3. Query git.emails for every email whose normalized subject matches
     any of those titles (root emails + all Re: replies)
  4. Insert (bug_id, email_id) rows into git.syzbot_bugs

"Relevant" is defined purely by subject-line matching:
    normalize(subject) == normalize(discussion_title)
    where normalize = lowercase + strip all leading "Re: "

The table is created if it does not yet exist.
Insertions are idempotent (ON CONFLICT DO NOTHING), so re-running is safe.

Usage:
    python3 ingest_syzbot.py <bug_id> [<bug_id> ...]
    python3 ingest_syzbot.py --file /path/to/lkbench-2512.json
    python3 ingest_syzbot.py --file lkbench-2512.json <extra_bug_id>
"""

import sys
import re
import asyncio
import hashlib
import urllib.request
from html import unescape

import asyncpg
from aiolimiter import AsyncLimiter


def _get_config() -> dict:
    """Load runtime config from environment variables with sensible defaults."""
    return {
        "db_dsn": (
            __import__("os").environ.get(
                "LKML_DB_DSN",
                "postgresql://mailinglist:yourpassword@127.0.0.1:5432/mailinglist",
            )
        ),
        "limiter": AsyncLimiter(1, 10),  # max 1 request per 10 seconds
    }


SYZBOT_URL = "https://syzkaller.appspot.com/bug?id={bug_id}"


# ── Subject normalization ──────────────────────────────────────────────────────

def normalize_subject(subject: str) -> str:
    """
    Lowercase and strip all leading 'Re: ' prefixes.
    This maps root emails and all their replies to the same key,
    so searching for a discussion title matches the full thread.
    e.g. 'Re: Re: [PATCH] fix foo' → '[patch] fix foo'
    """
    s = subject.lower().strip()
    while s.startswith("re: "):
        s = s[4:]
    return s


# ── Syzkaller scraper ──────────────────────────────────────────────────────────

def scrape_bug(bug_id: str) -> tuple[str, list[str]]:
    """
    Fetch the syzkaller page for bug_id and extract:
      - bug_title: the page <title> (e.g. "KASAN: use-after-free in foo")
      - discussion_titles: list of email subject lines from the Discussions table

    The Discussions section on the syzkaller page is an HTML table of
    lore.kernel.org thread links. Each link text is a discussion title.
    """
    url = SYZBOT_URL.format(bug_id=bug_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Extract bug title from <title> tag
    title_match = re.search(r"<title>([^<]+)</title>", html)
    title = title_match.group(1).strip() if title_match else "N/A"

    # Extract discussion titles from the Discussions table only (not other links)
    discussion_titles = []
    section = re.search(r"Discussions.*?</table>", html, re.DOTALL)
    if section:
        for _href, text in re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>',
                                       section.group()):
            discussion_titles.append(unescape(text.strip()))

    return title, discussion_titles


# ── Database setup ─────────────────────────────────────────────────────────────

async def ensure_table(conn: asyncpg.Connection) -> None:
    """
    Create git.syzbot_bugs if it doesn't exist, and ensure the subject index
    on git.emails is present (needed for fast subject lookups).
    """
    # syzbot_bugs: many-to-many join between bug IDs and email IDs.
    # One bug has many emails; one email can appear in multiple bugs.
    # UNIQUE (bug_id, email_id) prevents duplicates on re-runs.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS git.syzbot_bugs (
            bug_id   TEXT    NOT NULL,
            email_id INTEGER NOT NULL REFERENCES git.emails(email_id),
            UNIQUE (bug_id, email_id)
        )
    """)

    # Functional index on md5(normalized subject) so subject lookups don't
    # full-scan 6.87M rows. md5() is used instead of the raw expression to
    # avoid btree row-size limits on very long subjects (>2704 bytes).
    # CREATE INDEX IF NOT EXISTS is a no-op if the index already exists.
    print("Ensuring subject index (may take a few minutes on first run)...")
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS emails_subject_normalized_idx
        ON git.emails (md5(regexp_replace(lower(subject), '^(re: )+', '')))
    """)
    print("Subject index ready.")


# ── Email lookup ───────────────────────────────────────────────────────────────

async def find_email_ids(conn: asyncpg.Connection,
                         normalized_titles: list[str]) -> list[int]:
    """
    Return all email_ids from git.emails whose normalized subject matches
    any title in normalized_titles.

    The SQL expression mirrors normalize_subject():
        md5(regexp_replace(lower(subject), '^(re: )+', ''))
    which the index covers, making this query fast even on 6.87M rows.

    We pass MD5 hashes of the titles (not the raw strings) so the query
    can use the index directly.
    """
    if not normalized_titles:
        return []

    title_hashes = [hashlib.md5(t.encode()).hexdigest() for t in normalized_titles]
    rows = await conn.fetch("""
        SELECT email_id
        FROM git.emails
        WHERE md5(regexp_replace(lower(subject), '^(re: )+', '')) = ANY($1::text[])
    """, title_hashes)

    return [r["email_id"] for r in rows]


# ── Per-bug ingest ─────────────────────────────────────────────────────────────

async def ingest_bug(pool: asyncpg.Pool, bug_id: str,
                     limiter: AsyncLimiter) -> dict:
    """
    Full pipeline for one bug ID:
      1. Scrape the syzkaller page
      2. Normalize the discussion titles
      3. Find matching email_ids in the DB
      4. Insert (bug_id, email_id) rows

    Returns a summary dict with counts (used for the final report).
    """
    url = SYZBOT_URL.format(bug_id=bug_id)
    print(f"\n── Bug: {bug_id}")
    print(f"   URL: {url}")

    # Step 1: fetch the syzkaller page (rate-limited proactively)
    try:
        async with limiter:
            title, discussion_titles = scrape_bug(bug_id)
    except Exception as e:
        print(f"   ERROR scraping: {e}")
        return {"bug_id": bug_id, "error": str(e)}

    print(f"   Title: {title}")
    print(f"   Discussions found: {len(discussion_titles)}")
    for t in discussion_titles:
        print(f"     · {t}")

    # Step 2: normalize and deduplicate discussion titles
    normalized = list({normalize_subject(t) for t in discussion_titles})
    print(f"   Unique normalized subjects: {len(normalized)}")

    async with pool.acquire() as conn:
        # Step 3: find all matching emails in git.emails
        email_ids = await find_email_ids(conn, normalized)
        print(f"   Matching email_ids: {len(email_ids)}")

        # Step 4: insert (bug_id, email_id) rows; skip existing ones silently
        inserted = 0
        for email_id in email_ids:
            result = await conn.execute("""
                INSERT INTO git.syzbot_bugs (bug_id, email_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
            """, bug_id, email_id)
            # execute() returns a string like "INSERT 0 1" (inserted) or "INSERT 0 0" (skipped)
            if result.endswith("1"):
                inserted += 1

    print(f"   Inserted: {inserted}  (skipped existing: {len(email_ids) - inserted})")
    return {
        "bug_id":         bug_id,
        "title":          title,
        "discussions":    len(discussion_titles),
        "emails_matched": len(email_ids),
        "inserted":       inserted,
    }


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(bug_ids: list[str]) -> None:
    cfg  = _get_config()
    pool = await asyncpg.create_pool(cfg["db_dsn"], min_size=2, max_size=10,
                                     server_settings={"search_path": "git,public"})
    try:
        # Ensure table + index exist before processing any bugs
        async with pool.acquire() as conn:
            await ensure_table(conn)
        print(f"Table git.syzbot_bugs ready.")

        limiter = cfg["limiter"]
        summaries = await asyncio.gather(
            *[ingest_bug(pool, bug_id, limiter) for bug_id in bug_ids]
        )

        # Final summary report
        print("\n══ Summary ══")
        total_emails   = sum(s.get("emails_matched", 0) for s in summaries)
        total_inserted = sum(s.get("inserted", 0) for s in summaries)
        errors         = [s for s in summaries if "error" in s]
        print(f"  Bugs processed : {len(bug_ids)}")
        print(f"  Errors         : {len(errors)}")
        print(f"  Emails matched : {total_emails}")
        print(f"  Rows inserted  : {total_inserted}")
    finally:
        await pool.close()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Ingest syzbot bug IDs into git.syzbot_bugs"
    )
    parser.add_argument(
        "bug_ids",
        nargs="*",
        metavar="BUG_ID",
        help="One or more syzbot bug IDs (full SHA1)",
    )
    parser.add_argument(
        "--file", "-f",
        metavar="PATH",
        help="JSON file containing a list of bug IDs (e.g. lkbench-2512.json)",
    )
    args = parser.parse_args()

    # Collect bug IDs from both CLI args and --file, then deduplicate
    bug_ids = list(args.bug_ids)
    if args.file:
        with open(args.file) as f:
            bug_ids += json.load(f)

    if not bug_ids:
        parser.print_help()
        sys.exit(1)

    # Deduplicate while preserving order
    seen       = set()
    unique_ids = []
    for b in bug_ids:
        if b not in seen:
            seen.add(b)
            unique_ids.append(b)

    print(f"Bug IDs to ingest: {len(unique_ids)}")
    asyncio.run(main(unique_ids))
