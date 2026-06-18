"""
lkml-cli.py — Command-line interface to the LKML email database.

This is the non-MCP equivalent of mcp_server.py. Instead of Claude calling
named MCP tools, a mini agent runs this script as a bash command and reads
the printed output.

Requires api.py running on port 8001.

Usage:
    python3 lkml-cli.py search "deadlock" --date-to 2024-06-01
    python3 lkml-cli.py get 12345
    python3 lkml-cli.py thread "kernel BUG at mm/slub.c"
    python3 lkml-cli.py repos
    python3 lkml-cli.py stats

Cutoff / token:
    The script reads LKML_SESSION_TOKEN and LKML_CUTOFF from the environment
    automatically — no flags needed. These are set by invoke_agent.py before
    launching the agent.
"""

import argparse
import os
import sys
import httpx

# ── Config ────────────────────────────────────────────────────────────────────

# Base URL of the running FastAPI server.
API = "http://127.0.0.1:8001"

# Session token issued by invoke_agent.py. Carries the cutoff date server-side.
# The agent never sees or knows the cutoff date — it's just a UUID here.
SESSION_TOKEN: str = os.environ.get("LKML_SESSION_TOKEN", "")

# Fallback env-var cutoff (plain date, no token system). Used if no session token.
CUTOFF_DATE: str = os.environ.get("LKML_CUTOFF", "")


# ── Helpers ───────────────────────────────────────────────────────────────────

def add_auth(params: dict) -> dict:
    """
    Inject whichever cutoff mechanism is active into a params dict.

    Token system takes priority. If neither is set, params are unchanged
    and the API returns all emails with no date restriction.
    """
    if SESSION_TOKEN:
        params["token"] = SESSION_TOKEN
    elif CUTOFF_DATE:
        # Env-var fallback: pass cutoff as date_to if not already stricter.
        existing = params.get("date_to", "")
        if not existing or CUTOFF_DATE < existing:
            params["date_to"] = CUTOFF_DATE
    return params


def get(path: str, params: dict) -> dict:
    """Make a GET request to api.py and return parsed JSON. Exit on error."""
    try:
        resp = httpx.get(f"{API}{path}", params=params, timeout=30)
    except httpx.ConnectError:
        sys.exit(f"ERROR: Cannot connect to API at {API}. Is api.py running?")
    if resp.status_code != 200:
        sys.exit(f"ERROR: API returned {resp.status_code}: {resp.text}")
    return resp.json()


# ── Subcommand handlers ───────────────────────────────────────────────────────

def cmd_search(args: argparse.Namespace) -> None:
    """
    Search emails by keyword and/or filters.

    Maps to: GET /search
    MCP equivalent: search_emails(...)
    """
    params: dict = {"limit": args.limit}

    # Only pass filters that were actually provided — empty strings would
    # be treated as real filter values by the API.
    if args.query:       params["q"]           = args.query
    if args.repo:        params["repo"]        = args.repo
    if args.sender:      params["sender"]      = args.sender
    if args.sender_addr: params["sender_addr"] = args.sender_addr
    if args.subject:     params["subject"]     = args.subject
    if args.date_from:   params["date_from"]   = args.date_from
    if args.date_to:     params["date_to"]     = args.date_to

    add_auth(params)

    data = get("/search", params)
    total = data["total_returned"]

    if total == 0:
        print("No emails found.")
        return

    print(f"Found {total} email(s):\n")
    for i, e in enumerate(data["results"], 1):
        print(f"[{i}] {e.get('subject') or '(no subject)'}")
        print(f"    Sender     : {e.get('sender', '')} <{e.get('sender_addr', '')}>")
        print(f"    Date (UTC) : {e.get('sent_at', '')}")
        print(f"    Email ID   : {e.get('email_id', '')}")
        print()


def cmd_get(args: argparse.Namespace) -> None:
    """
    Fetch one full email by its integer ID.

    Maps to: GET /email/{id}
    MCP equivalent: get_email(email_id)
    """
    params: dict = {}
    if CUTOFF_DATE:
        params["cutoff"] = CUTOFF_DATE
    if SESSION_TOKEN:
        params["token"] = SESSION_TOKEN

    try:
        resp = httpx.get(f"{API}/email/{args.email_id}", params=params, timeout=30)
    except httpx.ConnectError:
        sys.exit(f"ERROR: Cannot connect to API at {API}. Is api.py running?")

    if resp.status_code == 404:
        print(f"Email not found: {args.email_id}")
        return
    if resp.status_code != 200:
        sys.exit(f"ERROR: API returned {resp.status_code}: {resp.text}")

    e = resp.json()
    print(f"Subject    : {e.get('subject', '')}")
    print(f"Sender     : {e.get('sender', '')} <{e.get('sender_addr', '')}>")
    print(f"Date (UTC) : {e.get('sent_at', '')}")
    print(f"Email ID   : {e.get('email_id', '')}")
    print(f"SHA-256    : {e.get('body_sha256', '')}")
    print()
    print("Body:")
    print(e.get("body") or "(no body)")


def cmd_thread(args: argparse.Namespace) -> None:
    """
    Fetch all emails in a thread by subject, oldest first.

    Re: prefixes are stripped automatically by the API so any email
    subject from the thread works as input.

    Maps to: GET /thread
    MCP equivalent: get_thread(subject, limit)
    """
    params: dict = {"subject": args.subject, "limit": args.limit}
    add_auth(params)

    data = get("/thread", params)
    total = data["total_returned"]

    if total == 0:
        print("No thread found.")
        return

    print(f"Thread: {total} email(s), oldest first:\n")
    for i, e in enumerate(data["results"], 1):
        print(f"[{i}] {e.get('subject') or '(no subject)'}")
        print(f"    Sender     : {e.get('sender', '')} <{e.get('sender_addr', '')}>")
        print(f"    Date (UTC) : {e.get('sent_at', '')}")
        print(f"    Email ID   : {e.get('email_id', '')}")
        print()


def cmd_repos(_args: argparse.Namespace) -> None:
    """
    List all 219 mailing list repos with email counts.

    Useful for finding exact repo names to pass to --repo in search.

    Maps to: GET /repos
    MCP equivalent: list_repos()
    """
    data = get("/repos", {})
    repos = data["repos"]
    print(f"{len(repos)} repositories:\n")
    for item in repos:
        print(f"  {item['email_count']:>9,}  {item['repo']}")


def cmd_stats(_args: argparse.Namespace) -> None:
    """
    Print total email and repo counts.

    Maps to: GET /stats
    MCP equivalent: get_stats()
    """
    data = get("/stats", {})
    print(f"Total emails : {data['total_emails']:,}")
    print(f"Repositories : {data['total_repos']}")


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lkml-cli",
        description="Query the LKML email database from the command line.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── search ────────────────────────────────────────────────────────────────
    p_search = sub.add_parser("search", help="Search emails by keyword and/or filters.")
    p_search.add_argument(
        "query", nargs="?", default="",
        help="Full-text keyword search on subject. e.g. 'use-after-free netdev'",
    )
    p_search.add_argument("--repo",        default="", help="Filter by repo/list name (partial). e.g. 'bpf'")
    p_search.add_argument("--sender",      default="", help="Filter by sender display name (partial). e.g. 'torvalds'")
    p_search.add_argument("--sender-addr", default="", dest="sender_addr", help="Filter by sender email address (partial).")
    p_search.add_argument("--subject",     default="", help="Filter by subject line (partial). e.g. '[PATCH]'")
    p_search.add_argument("--date-from",   default="", dest="date_from", metavar="YYYY-MM-DD", help="Emails on or after this date.")
    p_search.add_argument("--date-to",     default="", dest="date_to",   metavar="YYYY-MM-DD", help="Emails on or before this date.")
    p_search.add_argument("--limit",       default=20, type=int, help="Max results (default 20, max 200).")
    p_search.set_defaults(func=cmd_search)

    # ── get ───────────────────────────────────────────────────────────────────
    p_get = sub.add_parser("get", help="Fetch one full email by integer ID.")
    p_get.add_argument("email_id", type=int, help="Integer ID from search results.")
    p_get.set_defaults(func=cmd_get)

    # ── thread ────────────────────────────────────────────────────────────────
    p_thread = sub.add_parser("thread", help="Fetch all emails in a thread by subject.")
    p_thread.add_argument("subject", help="Any subject from the thread (Re: prefix ignored).")
    p_thread.add_argument("--limit", default=200, type=int, help="Max results (default 200).")
    p_thread.set_defaults(func=cmd_thread)

    # ── repos ─────────────────────────────────────────────────────────────────
    p_repos = sub.add_parser("repos", help="List all 219 repos with email counts.")
    p_repos.set_defaults(func=cmd_repos)

    # ── stats ─────────────────────────────────────────────────────────────────
    p_stats = sub.add_parser("stats", help="Print total email and repo counts.")
    p_stats.set_defaults(func=cmd_stats)

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
