"""
mcp_server.py — MCP server exposing git-ingested LKML email search to Claude Code.

Requires api.py running on port 8001.
Claude Code launches this automatically over stdio.

Register in ~/.claude.json:
    {
      "mcpServers": {
        "lkml-git": {
          "type": "stdio",
          "command": "/usr/bin/python3",
          "args": ["/home/jinghezhang/database/mcp_server.py"]
        }
      }
    }

Stack:
    Claude Code  →  THIS FILE (stdio)  →  api.py (HTTP port 8001)  →  PostgreSQL
"""

import httpx
from mcp.server.fastmcp import FastMCP

# Base URL of the FastAPI server. Must be running before this MCP server is used.
API = "http://127.0.0.1:8001"

mcp = FastMCP("LKML Git Emails")


# ── Tools ─────────────────────────────────────────────────────────────────────
# Each @mcp.tool() function is exposed to Claude Code as a callable tool.
# They are synchronous wrappers around HTTP calls to api.py — FastMCP handles
# running them in the correct context.

@mcp.tool()
def search_emails(
    query:       str = "",
    repo:        str = "",
    sender:      str = "",
    sender_addr: str = "",
    subject:     str = "",
    date_from:   str = "",
    date_to:     str = "",
    limit:       int = 20,
) -> str:
    """
    Search Linux kernel mailing list emails ingested from Git repositories.

    Args:
        query:       Full-text keyword search on subject. e.g. "use-after-free netdev"
        repo:        Filter by repo/list name (partial). e.g. "bpf", "stable", "lkml"
        sender:      Filter by sender display name (partial). e.g. "torvalds"
        sender_addr: Filter by sender email address (partial). e.g. "@kernel.org"
        subject:     Filter by subject line (partial). e.g. "[PATCH]"
        date_from:   Emails on or after this date. Format: YYYY-MM-DD
        date_to:     date_to:     Emails on or before this date. Format: YYYY-MM-DD. When investigating a bug, set this to the bug report date to avoid seeing post-fix discussions.
        limit:       Max results (default 20, max 200).
    """
    # Only pass non-empty values as query params so api.py doesn't treat
    # empty strings as filter values.
    params: dict = {"limit": limit}
    if query:       params["q"]           = query
    if repo:        params["repo"]        = repo
    if sender:      params["sender"]      = sender
    if sender_addr: params["sender_addr"] = sender_addr
    if subject:     params["subject"]     = subject
    if date_from:   params["date_from"]   = date_from
    if date_to:     params["date_to"]     = date_to

    resp = httpx.get(f"{API}/search", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    total = data["total_returned"]
    if total == 0:
        return "No emails found."

    # Format results as a readable text block for Claude Code to present.
    lines = [f"Found {total} email(s):\n"]
    for i, e in enumerate(data["results"], 1):
        lines += [
            f"[{i}] {e.get('subject') or '(no subject)'}",
            f"    Sender     : {e.get('sender', '')} <{e.get('sender_addr', '')}>",
            f"    Date (UTC) : {e.get('sent_at', '')}",
            f"    Email ID   : {e.get('email_id', '')}",
            "",
        ]
    return "\n".join(lines)


@mcp.tool()
def get_email(email_id: int) -> str:
    """
    Fetch one complete email by its integer ID (from search results).

    Args:
        email_id: Integer ID returned by search_emails.
    """
    resp = httpx.get(f"{API}/email/{email_id}", timeout=30)
    if resp.status_code == 404:
        return f"Email not found: {email_id}"
    resp.raise_for_status()
    e = resp.json()
    return "\n".join([
        f"Subject    : {e.get('subject', '')}",
        f"Sender     : {e.get('sender', '')} <{e.get('sender_addr', '')}>",
        f"Date (UTC) : {e.get('sent_at', '')}",
        f"Email ID   : {e.get('email_id', '')}",
        f"SHA-256    : {e.get('body_sha256', '')}",
        "",
        "Body:",
        e.get("body") or "(no body)",
    ])

@mcp.tool()
def get_thread(subject: str, limit: int = 200) -> str:
    """
    Fetch all emails in the same thread as the given subject, in chronological order.
    Use this after finding a relevant email in search_emails to get the full conversation.

    Args:
        subject: Any email subject from the thread (Re: prefixes are ignored).
        limit:   Max emails to return (default 200).
    """
    resp = httpx.get(f"{API}/thread", params={"subject": subject, "limit": limit}, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    total = data["total_returned"]
    if total == 0:
        return "No thread found."

    lines = [f"Thread: {total} email(s), oldest first:\n"]
    for i, e in enumerate(data["results"], 1):
        lines += [
            f"[{i}] {e.get('subject') or '(no subject)'}",
            f"    Sender     : {e.get('sender', '')} <{e.get('sender_addr', '')}>",
            f"    Date (UTC) : {e.get('sent_at', '')}",
            f"    Email ID   : {e.get('email_id', '')}",
            "",
        ]
    return "\n".join(lines)

@mcp.tool()
def list_repos() -> str:
    """
    List all mailing list repositories in the database with their email counts.
    Use this to find exact repo names for filtering in search_emails().
    """
    resp = httpx.get(f"{API}/repos", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    lines = [f"{len(data['repos'])} repositories:\n"]
    for item in data["repos"]:
        lines.append(f"  {item['email_count']:>9,}  {item['repo']}")
    return "\n".join(lines)


@mcp.tool()
def get_stats() -> str:
    """Return total number of emails and repositories in the database."""
    resp = httpx.get(f"{API}/stats", timeout=30)
    resp.raise_for_status()
    s = resp.json()
    return (
        f"Total emails : {s['total_emails']:,}\n"
        f"Repositories : {s['total_repos']}"
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
