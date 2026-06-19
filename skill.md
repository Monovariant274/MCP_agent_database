# LKML Email Search — CLI Skill

You have access to a database of **6.87 million Linux kernel mailing list emails** from 219 repositories. Use the `lkml-cli.py` script to search it via bash.

The API server must be running before you use any command. If you get a connection error, stop and report it.

---

## Commands

### Examples 
# Stats and repos
python3 lkml-cli.py stats
python3 lkml-cli.py repos

# Basic search
python3 lkml-cli.py search "deadlock"
python3 lkml-cli.py search "use-after-free" --limit 5

# Filtered search
python3 lkml-cli.py search "deadlock" --repo bpf
python3 lkml-cli.py search --sender torvalds --limit 5
python3 lkml-cli.py search "[PATCH]" --date-from 2023-01-01 --date-to 2024-01-01

# Fetch full email (replace 12345 with a real ID from search results)
python3 lkml-cli.py get 12345

# Fetch thread (replace subject with one from search results)
python3 lkml-cli.py thread "kernel BUG at mm/slub.c"

### Search emails

```bash
python3 /home/jinghezhang/MCP_agent_database/lkml-cli.py search "KEYWORD" [OPTIONS]
```

| Option | Description | Example |
|---|---|---|
| `"KEYWORD"` | Full-text search on subject (optional) | `"use-after-free"` |
| `--repo NAME` | Filter by mailing list name (partial) | `--repo bpf` |
| `--sender NAME` | Filter by sender display name (partial) | `--sender torvalds` |
| `--sender-addr ADDR` | Filter by sender email address (partial) | `--sender-addr @kernel.org` |
| `--subject TEXT` | Filter by subject line (partial) | `--subject "[PATCH]"` |
| `--date-from YYYY-MM-DD` | Emails on or after this date | `--date-from 2023-01-01` |
| `--date-to YYYY-MM-DD` | Emails on or before this date | `--date-to 2024-06-01` |
| `--limit N` | Max results, default 20, max 200 | `--limit 50` |

**Output:** A numbered list of matching emails, each showing subject, sender, date, and email ID.

---

### Fetch one full email

```bash
python3 /home/jinghezhang/MCP_agent_database/lkml-cli.py get EMAIL_ID
```

`EMAIL_ID` is the integer shown in search results. This returns the full email body.

---

### Fetch a full thread

```bash
python3 /home/jinghezhang/MCP_agent_database/lkml-cli.py thread "SUBJECT"
```

Fetches all emails in the same thread, oldest first. You can use any subject from the thread — `Re:` prefixes are ignored automatically.

---

### List all mailing lists

```bash
python3 /home/jinghezhang/MCP_agent_database/lkml-cli.py repos
```

Lists all 219 repos with their email counts. Use this to find exact repo names for `--repo`.

---

### Database stats

```bash
python3 /home/jinghezhang/MCP_agent_database/lkml-cli.py stats
```

---

## Typical workflow

1. **Search** for relevant emails using keywords or filters.
2. Note the **Email ID** of a promising result.
3. Use **`get`** to read the full email body.
4. Use **`thread`** to get the full discussion around it.

## Tips

- When investigating a bug, use `--date-to <bug-report-date>` to avoid seeing post-fix discussions.
- Use `--limit 50` or higher if you need broader coverage.
- Combine filters: `search "deadlock" --repo bpf --date-to 2024-01-01`
- The cutoff date (if any) is enforced automatically — you do not need to pass it manually.
