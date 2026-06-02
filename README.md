# LKML Git Email Database

PostgreSQL database of **6.87M unique Linux kernel mailing list emails** scraped from 219 Git repositories at [linux-mailinglist-archives](https://github.com/linux-mailinglist-archives).

## Architecture

```
Claude Code (MCP client)
    │  stdio
    ▼
mcp_server.py          — MCP server; auto-launched by Claude Code
    │  HTTP :8001
    ▼
api.py                 — FastAPI search API (must be started manually)
    │  asyncpg
    ▼
PostgreSQL: mailinglist DB, git schema
    ├── git.emails           — 6.87M unique emails
    ├── git.commits          — repo × commit × email mappings
    ├── git.repo_checkpoints — last processed commit per repo
    └── git.syzbot_bugs      — syzbot bug_id × email_id links
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Or with [uv](https://github.com/astral-sh/uv) (recommended):

```bash
uv pip install -r requirements.txt
```

### 2. Configure environment

Copy the template and fill in your values:

```bash
cp .env.example .env
# edit .env
```

Then source it before running any script:

```bash
source .env
```

### 3. Start the API server

```bash
uvicorn api:app --host 0.0.0.0 --port 8001
```

The MCP server (`mcp_server.py`) is launched automatically by Claude Code via `~/.claude.json` — no manual start needed.

## Ingestion

### Email ingestion (from GitHub)

```bash
# Full run: pull all 219 repos from GitHub, then ingest new commits
python3 ingest.py /home/jinghezhang/my_repos

# Resume after a crash — skips git pull, picks up from checkpoints
python3 ingest.py /home/jinghezhang/my_repos --skip-pull
```

Ingestion is incremental and resume-safe. A checkpoint is saved after every commit, so re-running after a crash will not duplicate data.

Set `GITHUB_TOKEN` to avoid hitting the 60 req/hour unauthenticated GitHub API limit when fetching the repo list.

### Syzbot bug ingestion

```bash
# One or more bug IDs
python3 ingest_syzbot.py <bug_id> [<bug_id> ...]

# From a JSON file (list of bug ID strings)
python3 ingest_syzbot.py --file lkbench-2512.json

# Both at once
python3 ingest_syzbot.py --file lkbench-2512.json <extra_bug_id>
```

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `GET` | `/stats` | Total email and repo counts |
| `GET` | `/repos` | All repos with email counts |
| `GET` | `/search` | Search emails (see parameters below) |
| `GET` | `/thread` | Fetch all emails in a thread by subject |
| `GET` | `/email/{email_id}` | Fetch one email by integer ID |

### `/search` parameters

Results are sorted by **sent time ascending** (oldest first) when `q` is given, giving a chronological view of the discussion.

| Parameter | Type | Description |
|---|---|---|
| `q` | string | Full-text keyword search on subject |
| `sender` | string | Partial match on sender display name |
| `sender_addr` | string | Partial match on sender email address |
| `subject` | string | Partial match on subject line |
| `repo` | string | Partial match on repo/list name |
| `date_from` | YYYY-MM-DD | Emails on or after this date |
| `date_to` | YYYY-MM-DD | Emails on or before this date. When investigating a bug, set this to the bug report date to avoid seeing post-fix discussions. |
| `limit` | int | Max results (default 20, max 200) |

### `/thread` parameters

| Parameter | Type | Description |
|---|---|---|
| `subject` | string | Any email subject from the thread — `Re:` prefixes are stripped automatically |
| `limit` | int | Max results (default 200) |

## MCP tools (available to Claude Code)

| Tool | Description |
|---|---|
| `search_emails(...)` | Search emails; mirrors `/search` parameters. Results sorted oldest-first when `query` is given. |
| `get_thread(subject)` | Fetch all emails in the same thread, sorted oldest-first. Use after `search_emails` to get the full conversation. |
| `get_email(email_id)` | Fetch full email body by integer ID |
| `list_repos()` | List all 219 repos with email counts |
| `get_stats()` | Return total email and repo counts |

## Environment variables

| Variable | Used in | Description |
|---|---|---|
| `LKML_DB_DSN` | `db.py`, `api.py`, `ingest_syzbot.py` | Full PostgreSQL DSN. Overrides the compiled-in default. |
| `GITHUB_TOKEN` | `ingest.py` | GitHub personal access token. Raises the API rate limit from 60 to 5000 req/hour — required when fetching 219 repos. |

## Database schema (quick reference)

```sql
-- One row per unique email
git.emails (
    email_id    SERIAL PRIMARY KEY,
    sender      TEXT,
    sender_addr TEXT,
    sent_at     TIMESTAMPTZ,
    subject     TEXT,
    body        TEXT,
    body_sha256 TEXT
)

-- repo × commit × email (cross-posted emails have multiple rows)
git.commits (
    repo          TEXT,
    git_commit_id TEXT,
    email_id      INTEGER → git.emails
)

-- Resume state for incremental ingestion
git.repo_checkpoints (
    repo           TEXT PRIMARY KEY,
    last_commit_id TEXT
)

-- syzbot bug ↔ email links
git.syzbot_bugs (
    bug_id   TEXT,
    email_id INTEGER → git.emails
)
```
