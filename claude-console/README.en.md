# ⌁ Claude Console

A local, **read-only**, real-time control panel for your `~/.claude` directory.
It surfaces every Claude Code configuration domain — settings, skills, plugins,
agents, commands, hooks, MCP servers, projects, sessions and history — in a
dependency-free "developer console" web UI that updates live as the files on
disk change. A sidebar toggle switches between **夜间模式 (dark, default)** and
**正常模式 (light)**; the choice is remembered (localStorage) and applied before
first paint.

## What it is (and is not)

- **Read-only.** No endpoint ever writes, deletes, or mutates anything under
  `~/.claude`. It never shells out to the `claude` CLI. It only *reads* files.
- **Local-only.** The server binds to `127.0.0.1` exclusively — it is not
  reachable from the network.
- **Secret-safe.** Everything that flows out of the API is passed through a
  redaction layer first:
  - Any value under a secret-y key (`token`, `secret`, `password`, `api_key`,
    `authorization`, `cookie`, `client_secret`, `refresh_token`, …) is replaced
    with `<REDACTED>`.
  - Any string that *looks like* a credential (`sk-…`, `sk-ant-…`, `ghp_…`,
    GitHub PATs, Slack `xox…` tokens, JWTs, AWS `AKIA…` keys, long hex/base64
    blobs) is redacted even under an innocuous key.
  - MCP server configs expose **env/header KEY NAMES only — never their values**.
  - History entries and session transcripts (which contain arbitrary pasted
    user text) are redacted before display.
- **Path-safe.** The "view raw" endpoint validates that the resolved path stays
  inside `~/.claude`, restricts to a small allowlist of text extensions, refuses
  any file whose name contains `credential` / `creds` / `token` / `.env`, and
  caps the served size.
- **Robust.** Scanners never raise: missing or corrupt files degrade to an
  `{"error": …}` field and the app stays usable.

## Requirements

Python 3.10+ and four libraries — **all of which are usually already installed**
in a Claude Code environment, so you typically do **not** need to install
anything:

- `fastapi`, `uvicorn[standard]`, `watchdog`, `websockets` (runtime)
- `pytest` (tests only)

If any are missing:

```bash
pip install -r requirements.txt
```

## How to run

From the project root:

```bash
./run.sh
```

or equivalently:

```bash
python3 -m claude_console.server
```

Then open:

> **http://127.0.0.1:8765**

The browser opens automatically; pass `--no-browser` to suppress it, or
`--port <N>` to use a different port (you can also set `CLAUDE_CONSOLE_PORT`).

To point the console at a non-default location (e.g. for a sandbox), set
`CLAUDE_CONSOLE_ROOT` (the `~/.claude` directory) and `CLAUDE_CONSOLE_HOME_JSON`
(the sibling `~/.claude.json`).

## Moving to another Mac

The console is fully portable across macOS machines — it hardcodes no paths or
usernames and keeps no state of its own (it is read-only). To run it on another
Mac:

1. Copy the project folder (`claude_console/`, `static/`, `run.sh`,
   `requirements.txt` — `tests/` and `screenshots/` are optional).
2. Make sure that Mac has **Python 3.10+**, then `pip install -r requirements.txt`
   (the four libraries are not bundled).
3. `./run.sh` and open **http://127.0.0.1:8765** (use `--port <N>` or
   `CLAUDE_CONSOLE_PORT` if 8765 is taken).

A few things follow from being read-only and path-agnostic:

- **It shows that Mac's own data.** The console reads the local `~/.claude` and
  `~/.claude.json` of whatever account runs it — nothing is carried over from the
  machine you copied it from.
- **The background-task `⚙` feature adapts automatically.** Its runtime source,
  `/tmp/claude-<uid>`, is resolved per-machine via `os.getuid()`, so it just works
  for the new account with no configuration. If a given Mac's Claude Code keeps
  those runtime buffers elsewhere, the chips simply don't appear (everything else
  is unaffected) — point the console at the real location with
  `CLAUDE_CONSOLE_RUNTIME_ROOT=<path>`.

## Domains

The left sidebar navigates these domains, each backed by a `GET /api/<domain>`
endpoint:

| Domain      | What it shows |
|-------------|---------------|
| **Settings**  | Effort level, permissions, hook events, flags + redacted, highlighted `settings.json`; renders `~/.claude/CLAUDE.md` as markdown when present |
| **Skills**    | Each `skills/*/SKILL.md` — name, description, refs badge; click a card to read its `SKILL.md` (rendered markdown) + browse its files |
| **Plugins**   | Installed plugins merged with the enabled map + marketplaces |
| **Agents**    | `agents/*.md` entries (empty-state aware) |
| **Commands**  | `commands/**/*.md` entries |
| **Hooks**     | Per-event hook commands from `settings.json` (redacted) |
| **MCP**       | MCP servers from `~/.claude.json` — transport, command/url, env **keys** |
| **Projects**  | Each project dir decoded to its real cwd; drill into sessions/transcripts |
| **History**   | `history.jsonl`, newest-first, searchable, paginated (redacted); rows whose session still exists on disk are clickable → open that session's transcript |
| **Plans**     | Plan-mode documents from `plans/*.md`, newest-first; click a row to read the plan rendered as markdown in the drawer |

Under **Projects → Sessions**, a session that has background-task output buffers
shows a green `⚙ N` chip. Those buffers live in Claude Code's session-runtime
scratch dir `/tmp/claude-<uid>/<project-key>/<session-id>/tasks/*.output` — the
captured stdout/result of `run_in_background` Bash commands and background
workflow/agent runs — which is a **second read-only source outside `~/.claude`**.
Clicking the chip lists them; clicking a task shows its output (redacted,
capped at 256 KB). Loose `/tmp` scratch (`tmpXXXX`, `pytest-of-*`, classifier
error logs) is treated as noise and not surfaced.

## Live updates

A [watchdog](https://pypi.org/project/watchdog/) observer watches a curated set
of paths under `~/.claude` (root, `skills/`, `agents/`, `commands/`, `plugins/`,
and `projects/` — while ignoring high-churn caches and bulk transcript noise).
When a relevant file changes, the affected UI domains are computed and pushed to
the browser over a WebSocket (`/ws`) with a 300 ms debounce and domain
coalescing. The UI then re-fetches just the affected domain, flashes the matching
nav item, and appends a row to the collapsible **Live activity** strip
(`[hh:mm:ss] kind path → domains`). A connection dot shows green when the
WebSocket is open, red when it is reconnecting (auto-reconnect with backoff).

## How to run the tests

```bash
python3 -m pytest -q
```

The suite builds a synthetic mini `~/.claude` tree in a temporary directory
(see `tests/conftest.py`) and asserts, among other things, that:

- secrets in `settings.json`, `history.jsonl`, and session prompts are redacted;
- MCP configs expose env **keys**, never values;
- `read_file_safe` rejects `../../etc/passwd` traversal and credential-named
  files while accepting `settings.json`;
- history is newest-first and the `q` filter works;
- `read_session` skips snapshots/meta records and flattens block content;
- project dir keys decode back to their real filesystem paths.

The tests depend only on the standard library plus `pytest`.
