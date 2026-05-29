"""
ingest.py — Daily ingestion of Linux kernel mailing list emails from Git repos.

On each run:
  1. Fetch the full list of repos from the linux-mailinglist-archives GitHub org.
  2. For each repo: clone it if not present locally, or git pull if it is.
  3. Traverse new git commits (since the stored checkpoint) and insert emails.

Run:
    python3 ingest.py [/path/to/local/repos/folder]
    python3 ingest.py /home/jinghezhang/my_repos --skip-pull   # resume after crash

Set GITHUB_TOKEN env var to avoid GitHub API rate limits on the repo listing step.
"""

import asyncio
import email as email_lib
import email.utils
import hashlib
import os
import re
import sys
import time
from datetime import timezone
from pathlib import Path

import httpx

from db import DSN, LkmlDB

# Default folder where all 219 git repos are cloned locally.
# Override via CLI argument: python3 ingest.py /path/to/repos
GIT_REPOS_FOLDER = "/home/jinghezhang/my_repos"

GITHUB_ORG       = "linux-mailinglist-archives"
PULL_CONCURRENCY = 20   # max concurrent clone/pull operations

_ws = re.compile(r"\s+")   # used to collapse whitespace in email headers


# ── GitHub repo listing ───────────────────────────────────────────────────────

async def _fetch_github_repos() -> list[dict]:
    """
    Return all repos in the linux-mailinglist-archives GitHub org.
    Each entry is a dict with 'name' and 'clone_url'.
    GitHub returns at most 100 per page, so this paginates automatically.
    """
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        # Without a token, GitHub rate-limits to 60 requests/hour.
        # With a token, the limit is 5000/hour — needed for 219 repos.
        headers["Authorization"] = f"Bearer {token}"

    repos = []
    page  = 1
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                f"https://api.github.com/orgs/{GITHUB_ORG}/repos",
                params={"per_page": 100, "page": page},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            repos.extend({"name": r["name"], "clone_url": r["clone_url"]} for r in batch)
            if len(batch) < 100:
                # Last page — stop paginating
                break
            page += 1

    return repos


# ── Email parser ──────────────────────────────────────────────────────────────

def _parse_email_bytes(raw: bytes) -> dict | None:
    """
    Parse raw email bytes (RFC 2822 format) into a dict of fields.
    Returns None if parsing fails — malformed emails are silently skipped.
    """
    try:
        msg = email_lib.message_from_bytes(raw)

        def hdr(name: str) -> str:
            """Extract a header value, collapsing internal whitespace."""
            val = msg.get(name, "") or ""
            return _ws.sub(" ", str(val)).strip()

        # Split 'From: Display Name <addr@example.com>' into two parts.
        # parseaddr handles quoted names, bare addresses, and malformed headers.
        from_raw    = hdr("From")
        sender, sender_addr = email_lib.utils.parseaddr(from_raw)
        sender      = sender.strip()
        sender_addr = sender_addr.strip()
        if not sender_addr:
            # Malformed header (no angle brackets) — store the raw value
            sender_addr = from_raw

        # Parse the Date header into a UTC-aware datetime.
        # sent_at stays None if the date is missing or unparseable.
        sent_at  = None
        date_str = hdr("Date")
        if date_str:
            try:
                dt      = email.utils.parsedate_to_datetime(date_str)
                sent_at = dt.astimezone(timezone.utc)
            except Exception:
                pass

        subject = hdr("Subject")

        # Extract plain-text body, handling both simple and multipart messages.
        if msg.is_multipart():
            # Walk all parts and collect text/plain parts that aren't attachments.
            parts = []
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition", ""))
                if ct == "text/plain" and "attachment" not in cd:
                    try:
                        parts.append(
                            part.get_payload(decode=True).decode("utf-8", errors="replace")
                        )
                    except Exception:
                        pass
            body = "\n".join(parts)
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload is None:
                    # get_payload(decode=True) returns None for non-encoded text;
                    # fall back to the raw string payload.
                    payload = (msg.get_payload() or "").encode()
                body = payload.decode("utf-8", errors="replace")
            except Exception:
                body = str(msg.get_payload() or "")

        # PostgreSQL rejects null bytes in TEXT columns.
        body        = body.replace("\x00", "")
        body_sha256 = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()

        return {
            "sender":      sender,
            "sender_addr": sender_addr,
            "sent_at":     sent_at,
            "subject":     subject,
            "body":        body,
            "body_sha256": body_sha256,
        }
    except Exception:
        return None


# ── Git helpers ───────────────────────────────────────────────────────────────

async def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return its stdout as a string."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="replace").strip()


async def _git_show_bytes(commit: str, filepath: str, cwd: Path) -> bytes:
    """
    Return the raw bytes of a file as it existed at a specific commit.
    This is used instead of reading from disk because each commit in these
    repos replaces the single 'm' file — disk only has the latest version.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "show", f"{commit}:{filepath}",
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return stdout


# ── Ingestor ──────────────────────────────────────────────────────────────────

class LkmlIngestor:
    def __init__(self, git_repositories_folder: str = GIT_REPOS_FOLDER, dsn: str = DSN):
        self.db                      = LkmlDB(dsn)
        self.git_repositories_folder = git_repositories_folder

    async def pull(self) -> list[str]:
        """
        Fetch the repo list from GitHub, then clone or pull each one locally.
        Returns the list of repo names that are now available on disk.
        Runs up to PULL_CONCURRENCY clone/pull operations in parallel using
        a semaphore to avoid overwhelming the network or GitHub rate limits.
        """
        print("Fetching repo list from GitHub...", flush=True)
        repos  = await _fetch_github_repos()
        folder = Path(self.git_repositories_folder)
        folder.mkdir(parents=True, exist_ok=True)
        print(f"Found {len(repos)} repos. Syncing (concurrency={PULL_CONCURRENCY})...",
              flush=True)

        sem   = asyncio.Semaphore(PULL_CONCURRENCY)
        total = len(repos)
        done  = 0
        lock  = asyncio.Lock()  # protects the done counter across concurrent tasks

        async def _sync_one(name: str, clone_url: str) -> None:
            nonlocal done
            async with sem:
                local = folder / name
                if local.exists():
                    # Repo already cloned — just pull new commits.
                    # --ff-only prevents merge commits if history diverged.
                    await _run_git(["git", "pull", "--ff-only", "--quiet"], local)
                    action = "pulled"
                else:
                    proc = await asyncio.create_subprocess_exec(
                        "git", "clone", "--quiet", clone_url, str(local),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if proc.returncode != 0:
                        err = stderr.decode(errors="replace").strip()
                        print(f"  [WARN] clone failed for {name}: {err}", flush=True)
                        action = "failed"
                    else:
                        action = "cloned"

                async with lock:
                    done += 1
                    print(f"  [{done}/{total}] {action}: {name}", flush=True)

        await asyncio.gather(*[_sync_one(r["name"], r["clone_url"]) for r in repos])
        print("All repos synced.", flush=True)
        return [r["name"] for r in repos]

    async def ingest_repo(self, repo_path: Path) -> int:
        """
        Process one git repository: find commits newer than the checkpoint,
        parse the email files they added, and insert into the DB.

        Checkpoint logic: on first run, traverses all commits (HEAD).
        On subsequent runs, only traverses commits after the last checkpoint.
        The checkpoint is saved after each commit, so a crash mid-run resumes
        cleanly from where it left off.

        Returns the number of new emails inserted.
        """
        repo_name  = repo_path.name
        checkpoint = await self.db.get_checkpoint(repo_name)

        # --reverse: oldest-first so the checkpoint advances incrementally.
        # If checkpoint exists, only fetch commits after it.
        log_args = ["git", "log", "--reverse", "--format=%H"]
        if checkpoint:
            log_args.append(f"{checkpoint}..HEAD")
        else:
            log_args.append("HEAD")

        commit_output = await _run_git(log_args, repo_path)
        commits = [c for c in commit_output.splitlines() if c]
        if not commits:
            return 0

        total_commits = len(commits)
        print(f"  {repo_name}: {total_commits} new commit(s)", flush=True)
        inserted     = 0
        REPORT_EVERY = 1_000

        for i, commit_hash in enumerate(commits, 1):
            # List files added or modified in this commit.
            # --diff-filter=AM: only Added or Modified files (skip deletions).
            # We read file content from git at this commit, not from disk,
            # because these repos store each email as a file named 'm' that
            # gets replaced on every commit.
            files_output = await _run_git(
                ["git", "diff-tree", "--no-commit-id", "-r",
                 "--name-only", "--diff-filter=AM", commit_hash],
                repo_path,
            )
            for filepath in files_output.splitlines():
                filepath = filepath.strip()
                if not filepath:
                    continue

                raw    = await _git_show_bytes(commit_hash, filepath, repo_path)
                parsed = _parse_email_bytes(raw)
                if parsed is None:
                    continue  # skip unparseable emails silently

                # check_email before insert to accurately count new insertions.
                # insert_email would also handle duplicates via ON CONFLICT,
                # but we need check_email to know if this is truly new.
                email_id = await self.db.check_email(
                    parsed["sender"], parsed["sender_addr"],
                    parsed["sent_at"], parsed["subject"], parsed["body_sha256"],
                )
                if email_id is None:
                    email_id = await self.db.insert_email(**parsed)
                    inserted += 1

                # Always record the commit→email mapping, even for duplicates,
                # so we know which repos carry each email (cross-posting).
                await self.db.insert_commit(repo_name, commit_hash, email_id)

            # Advance checkpoint after each fully processed commit.
            await self.db.set_checkpoint(repo_name, commit_hash)

            if i % REPORT_EVERY == 0:
                pct = i / total_commits * 100
                print(f"    {repo_name}: {i:,}/{total_commits:,} commits ({pct:.1f}%), "
                      f"{inserted:,} inserted", flush=True)

        print(f"  {repo_name}: done — {inserted} new email(s) inserted", flush=True)
        return inserted


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", nargs="?", default=GIT_REPOS_FOLDER,
                        help="Local folder containing git repos")
    parser.add_argument("--skip-pull", action="store_true",
                        help="Skip git pull/clone and go straight to ingestion "
                             "(useful when resuming after a crash)")
    args = parser.parse_args()

    ingestor = LkmlIngestor(args.folder)

    async def _run():
        await ingestor.db.connect()
        try:
            if not args.skip_pull:
                await ingestor.pull()
            folder = Path(args.folder)
            repos_on_disk = sorted(p for p in folder.iterdir() if p.is_dir())
            print(f"\nIngesting {len(repos_on_disk)} repositories...", flush=True)
            t0             = time.monotonic()
            total_inserted = 0
            for repo_path in repos_on_disk:
                total_inserted += await ingestor.ingest_repo(repo_path)
            elapsed = time.monotonic() - t0
            print(f"\nDone. {total_inserted} new email(s) inserted in {elapsed:.1f}s.",
                  flush=True)
        finally:
            await ingestor.db.close()

    asyncio.run(_run())
