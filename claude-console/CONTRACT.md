# Claude Console — Build Contract (authoritative)

A local web **control panel** that reads the user's `~/.claude` directory in
real time and surfaces every Claude-Code configuration domain. Read-only and
safe: binds to `127.0.0.1` only, redacts secrets, never serves credential files.

Stack (all already installed, do NOT pip-install): Python 3.10, **FastAPI**,
**uvicorn**, **watchdog**, **websockets**. Frontend is dependency-free vanilla
HTML/CSS/JS (no build step). This file is the SINGLE SOURCE OF TRUTH — every
module must match the signatures and JSON shapes below exactly.

---

## 0. File layout (each builder owns its files; do not edit another builder's files)

```
claude-console/
  README.md                    # builder D
  requirements.txt             # builder D
  run.sh                       # builder D  (chmod +x)
  CONTRACT.md                  # (this file, already written)
  claude_console/
    __init__.py                # builder A  (empty or version string)
    config.py                  # builder A
    redact.py                  # builder A
    scanner.py                 # builder A
    watcher.py                 # builder B
    server.py                  # builder B
  static/
    index.html                 # builder C
    app.js                     # builder C
    style.css                  # builder C
  tests/
    __init__.py                # builder D
    conftest.py                # builder D  (builds synthetic ~/.claude fixture tree)
    test_redact.py             # builder D
    test_scanner.py            # builder D
```

---

## 1. config.py  (builder A)

```python
import os
from pathlib import Path

def claude_root() -> Path:
    # Honor override for tests; else ~/.claude
    return Path(os.environ.get("CLAUDE_CONSOLE_ROOT", Path.home() / ".claude"))

def home_claude_json() -> Path:
    # Top-level ~/.claude.json (sibling of ~/.claude). Override via env for tests.
    env = os.environ.get("CLAUDE_CONSOLE_HOME_JSON")
    if env:
        return Path(env)
    return claude_root().parent / ".claude.json"

HOST = "127.0.0.1"
PORT = int(os.environ.get("CLAUDE_CONSOLE_PORT", "8765"))

# Subdirs/files that are high-churn or irrelevant to "config" — the watcher
# ignores events whose path contains any of these segments, and overview skips
# deep-walking them for file COUNT (it still reports their total size cheaply).
HEAVY_OR_NOISE_SEGMENTS = {
    "shell-snapshots", "file-history", "paste-cache", "image-cache",
    "audit", "audit-logs", "telemetry", "tasks", "cache", "session-env",
    "sessions", "backups", "ide", "debug", "statsig", "local-marketplaces",
    "__pycache__", ".git",
}
IGNORE_SUFFIXES = (".lock", ".bak", ".DS_Store")

# Map a changed path (relative to claude_root) to the affected UI domain(s).
# domain names MUST be exactly one of:
DOMAINS = ["overview", "settings", "skills", "plugins", "agents",
           "commands", "hooks", "mcp", "projects", "history", "plans"]

def path_to_domains(rel_path: str) -> list[str]:
    """Return affected domains for a changed path relative to claude_root.
    Examples:
      'settings.json' -> ['settings','hooks','plugins','overview']
      'skills/foo/SKILL.md' -> ['skills','overview']
      'history.jsonl' -> ['history','overview']
      'plugins/installed_plugins.json' -> ['plugins','overview']
      'agents/x.md' -> ['agents','overview']
      'commands/x.md' -> ['commands','overview']
      'projects/<key>/<id>.jsonl' -> ['projects','history','overview']
    Anything else -> ['overview'] (so size/counts refresh) — or [] if it is a
    noise segment (caller already filters those, but be defensive).
    """
```

Notes: `path_to_domains` must be pure and unit-tested. `settings.json` changes
fan out to settings+hooks+plugins (enabled map lives there)+overview.

---

## 2. redact.py  (builder A)

Goal: never leak secrets through the API. Pure, fully unit-tested.

```python
REDACTED = "<REDACTED>"

# Key looks secret-y (case-insensitive substring match on any of these):
SECRET_KEY_PARTS = ("token", "secret", "password", "passwd", "api_key",
                    "apikey", "api-key", "credential", "authorization",
                    "auth_token", "access_key", "private_key", "cookie",
                    "client_secret", "refresh_token", "session_token")

def is_secret_key(key: str) -> bool: ...

# Value looks like a credential even under an innocuous key:
#   sk-... / sk-ant-... , ghp_/gho_/github_pat_ , xox[baprs]-... (slack),
#   JWT (eyJ...\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+), AWS AKIA..., long hex>=32,
#   long base64-ish >=40. Empty/short/plain values pass through.
def looks_like_secret_value(value: str) -> bool: ...

def redact_value(key: str, value):
    """If key is secret-y OR (value is str and looks_like_secret_value) -> REDACTED.
    Non-str values under secret-y keys -> REDACTED. Else unchanged."""

def redact_obj(obj):
    """Deep-copy obj (dict/list/scalar) applying redact_value at every dict entry
    and scanning list/str scalars. Returns a new structure; never mutates input.
    For a top-level string value not under a key, redact if looks_like_secret_value."""
```

Rule of thumb: any dict value whose KEY is secret-y is redacted regardless of
type; any string value (any key) that matches a credential pattern is redacted.

---

## 3. scanner.py  (builder A)  — the read layer, all functions PURE & deterministic

Every function reads from `config.claude_root()` (re-read each call, so tests can
override env). All return JSON-serializable dicts/lists. All must be robust to
missing files/dirs/corrupt JSON — never raise; on error put an `"error"` string
in the relevant field and continue. Apply redaction to anything derived from
settings env, mcp config, and raw file reads.

Helpers (exported): `human_size(n:int)->str` ("1.2 MB"), `safe_json(path)->obj|None`,
`file_meta(path)->{"name","path","size","size_h","mtime","mtime_iso"}` (mtime is
epoch **seconds** float; mtime_iso local ISO string).

### 3.1 `scan_overview() -> dict`
```jsonc
{
  "root": "/Users/.../.claude",
  "exists": true,
  "generated_at": <epoch_seconds float>,
  "totals": {"size": <bytes>, "size_h": "1.2 GB"},
  "counts": {"skills": 9, "plugins": 12, "projects": 22,
             "sessions": 134, "history_entries": 3938, "agents": 0, "commands": 0},
  "subdirs": [ {"name":"projects","size":..., "size_h":"168 MB",
                "mtime":..., "mtime_iso":"...", "is_noise": false}, ... ],
                // sorted by size desc; size via fast du-like walk (os.scandir),
                // for HEAVY_OR_NOISE_SEGMENTS still compute size but mark is_noise
  "top_files": [ {"name":"history.jsonl","size":...,"size_h":"..."}, ... ] // root-level files only
}
```
`counts.sessions` = total number of `*.jsonl` transcript files across `projects/*/`.
`counts.history_entries` = line count of `history.jsonl`. Counts must be cheap
(no full parse of big files for line count — read in binary chunks).

### 3.2 `read_settings() -> dict`
```jsonc
{
  "files": [ file_meta for each of settings.json, settings.local.json (if present),
             and any settings*.bak (mark "is_backup": true) ],
  "active_path": ".../settings.json",
  "claude_md": { "exists": true, "content": "<redacted markdown, <=512KB>",
                 "size": .., "truncated": false },   // ~/.claude/CLAUDE.md; exists:false when absent
  "settings": { ...parsed settings.json, REDACTED... },   // null if missing/corrupt
  "summary": {
     "effortLevel": "xhigh",
     "permissions": {"allow":[...], "deny":[...], "ask":[...]},  // redacted
     "hook_events": ["SessionStart","Stop",...],                // just the event names present
     "enabled_plugins_count": 13,
     "flags": {"skipDangerousModePermissionPrompt": true, ...}   // any top-level bool flags
  },
  "error": null
}
```

### 3.3 `scan_skills() -> dict`
Reads `skills/*/SKILL.md`. Parse YAML-ish frontmatter between leading `---`
fences WITHOUT a yaml dependency: take the block, pull `name:` and
`description:` (support `>-`/`|` folded blocks → join following more-indented
lines until a less-indented key). Be tolerant.
```jsonc
{ "count": 9,
  "items": [ {"name":"daily-work-summary",
              "key":"daily-work-summary",            // dir basename → builds skills/<key>/<file> rels
              "description":"... (may be long, keep full, server/UI truncates)",
              "dir":".../skills/daily-work-summary",
              "has_references": true,
              "files": ["SKILL.md","references/..."],   // shallow list, max ~20
              "mtime_iso":"..."}, ... ]   // sorted by name
}
```
The UI uses `key` + `files` to open each file through `/api/file?rel=skills/<key>/<file>`.

### 3.4 `scan_plugins() -> dict`
Merge three sources: `plugins/installed_plugins.json` (`{version, plugins:{ "key@marketplace":[{scope,installPath,version,installedAt,lastUpdated,...}] }}`),
`settings.json.enabledPlugins` (`{"key@marketplace": true/false}`), and
`plugins/known_marketplaces.json` (`{ name: {source:{source,repo}, installLocation, lastUpdated} }`).
```jsonc
{
  "marketplaces": [ {"name":"claude-plugins-official","source":"github:anthropics/claude-plugins-official",
                     "installLocation":"...","lastUpdated":"..."}, ... ],
  "items": [ {"key":"code-review@claude-plugins-official",
              "name":"code-review", "marketplace":"claude-plugins-official",
              "version":"1.0.0", "enabled":true, "scope":"user",
              "installPath":"...", "installedAt":"...", "lastUpdated":"..."}, ... ],
  "count": <len(items)>
}
```
`enabled` comes from settings.enabledPlugins[key] (default false if absent).
Include plugins that are enabled-but-not-in-installed and vice-versa (union of keys).

### 3.5 `scan_agents() -> dict` and `scan_commands() -> dict`
Read `agents/*.md` and `commands/**/*.md`. Each item: `{name, description, path, mtime_iso}`.
`name` = frontmatter name or filename stem. `description` = frontmatter description
or first non-empty markdown line. Empty dir → `{"count":0,"items":[]}`. Never raise.

### 3.5b `scan_plans() -> dict`
Plan-mode documents under `plans/*.md` — plain markdown, NO frontmatter,
conventionally `# Plan — <title>`. Newest-first (by mtime). `title` = first
markdown heading with a leading `Plan —`/`Plan:`/`Plan -` prefix trimmed, falling
back to the file stem. Only the file head is read for the title (big plans aren't
fully loaded here). Item CONTENT is served through the existing
`/api/file?rel=plans/<name>.md` (`read_file_safe`), so there is no separate read
endpoint.
```jsonc
{ "count": 3,
  "items": [ {"name":"lively-soaring-moon", "title":"…",
              "rel":"plans/lively-soaring-moon.md",
              "size":.., "size_h":"..", "mtime_iso":".."} ] }
```
`scan_overview().counts` also gains a `plans` count (`_count_md(plans/, recursive=False)`).

### 3.6 `scan_hooks() -> dict`
From `settings.json.hooks` shape `{ EventName: [ {matcher?, hooks:[{type,command,...}]} , ...] }`.
```jsonc
{ "events": [ {"event":"PreToolUse",
               "entries":[ {"matcher":"Bash", "hooks":[{"type":"command","command":"<REDACTED-if-secret> ..."} ]} ]} ],
  "count": <number of event groups> }
```
Redact any command string via redact (commands rarely hold secrets but be safe;
do NOT truncate commands — UI handles display).

### 3.7 `read_mcp() -> dict`
From `home_claude_json()` top-level `mcpServers` (`{ name: {command?,args?,url?,type?,env?,headers?} }`)
plus auth state from `mcp-needs-auth-cache.json` in claude_root if present.
```jsonc
{ "servers": [ {"name":"yuque", "transport":"stdio|http|sse",
                "command":"...", "args":[...], "url":"...",
                "env_keys":["TOKEN"],            // KEYS only, values never included
                "needs_auth": true|false,
                "raw": { ...redacted server config... }} ],
  "count": <n>, "source": ".../.claude.json", "error": null }
```
transport: "stdio" if `command` present, else "http"/"sse" from `type`/`url`.
NEVER include env/header VALUES — only key names. `raw` is the full server entry
passed through `redact_obj`.

### 3.8 `scan_projects() -> dict`
Each subdir of `projects/` is a sanitized cwd. **DISPLAY `real_path` =
`_decode_project_key`** — the straightforward decoded folder path, uniform across
all projects (leading/internal `-` → `/`). It is lossy (sanitization maps both
`/` and `_` to `-`), but per product decision the UI shows this plain decoded
folder, prominently styled, rather than recovering the exact cwd. The raw `key`
is kept too (shown in the UI `title`). NOTE: the **reveal / open-folder** action
(`resolve_project_path` → `_project_real_path` → `_read_cwd_from_dir`) still
resolves the verbatim `cwd` from a transcript so Finder opens the actual on-disk
directory; only the displayed path uses the simple decode. 
```jsonc
{ "count": 22,
  "items": [ {"key":"-Users-qianyi-DevWorkspace",
              "real_path":"/Users/qianyi/DevWorkspace",
              "session_count": 5, "size": <bytes>, "size_h":"...",
              "last_activity": <epoch_s>, "last_activity_iso":"..."}, ... ]
              // sorted by last_activity desc
}
```
session_count = number of `*.jsonl` in that dir. last_activity = max mtime of its files.

### 3.9 `list_sessions(project_key:str) -> dict`
Prefer `projects/<key>/sessions-index.json` (`{version, entries:[{sessionId,fullPath,fileMtime(ms),firstPrompt,summary}]}`).
If absent, derive from the `*.jsonl` files (sessionId = filename stem, firstPrompt
from first user message, mtime from file).
```jsonc
{ "project_key":"...", "real_path":"...",
  "sessions": [ {"session_id":"uuid", "first_prompt":"...", "summary":"...",
                 "mtime_iso":"...", "size":..., "size_h":"...",
                 "tokens": {"input":..,"output":..,"cache_read":..,
                            "cache_creation":..,"total":..,"messages":..}} , ... ],
  "totals": {"tokens": {<same flat token shape, summed over sessions>},
             "sessions": <n>},
  "count": <n>, "error": null }
```
Token usage is summed from each session transcript's assistant `usage` records via
`session_token_usage(path)` (cached by path+mtime+size). `total` = input+output+
cache_read+cache_creation. Only TOP-LEVEL usage counters are summed — never the
nested `iterations` (avoids double-count). Sessions with no assistant turns → all zeros.
`project_key` must be validated: only `[A-Za-z0-9._-]` allowed (no `/`, no `..`).
Reject traversal → return `{"error":"invalid project key", ...}`.

### 3.10 `read_session(project_key:str, session_id:str, limit:int=400) -> dict`
Parse `projects/<key>/<session_id>.jsonl`. session_id validated `[A-Za-z0-9-]`.
The message field in user/assistant records is a JSON object
`{role, content}` where content is a string OR a list of blocks
(`{"type":"text","text":...}`, `{"type":"tool_use","name":...}`,
`{"type":"tool_result",...}`, `{"type":"thinking",...}`). Flatten content to a
short text preview. Skip record types: `file-history-snapshot`, `attachment`,
`last-prompt`, and `isMeta:true` user records. When the session has more than
`limit` displayable messages, keep the LAST `limit` (the tail — where appended /
most-recent turns live; head-truncation would hide exactly the newest content)
and set `truncated:true`. `tokens` is a separate whole-session pass, unaffected by
the limit. The UI requests `limit=2000`.
```jsonc
{ "project_key":"...", "session_id":"...",
  "messages": [ {"role":"user|assistant|system", "type":"user",
                 "text":"flattened preview (<= 4000 chars)",
                 "ts_iso":"...", "model":"claude-opus-4-7|null",
                 "blocks":["text","tool_use:Bash", ...],  // kind tags for badges
                 "tokens": <int total for that assistant turn, or null>,
                 "is_prompt": <bool>  // genuine user-typed question (see below)
                }, ... ],
  "tokens": {"input":..,"output":..,"cache_read":..,"cache_creation":..,
             "total":..,"messages":..},   // whole-session roll-up (ignores display limit)
  "prompt_count": <n is_prompt messages>,
  "count": <n returned>, "truncated": <bool>, "error": null }
```
`is_prompt` flags a GENUINE user question — used by the UI to highlight what the
user actively asked. True only when role==user, no `tool_result` block, and the
RAW (pre-redaction) text does not start with a noise marker
(`<command-name>`, `<local-command-stdout>`, `<task-notification>`,
`<system-reminder>`, `<bash-*>`, caveat, etc.). Classify on raw text — redaction
can rewrite a leading marker and let noise slip through. Same field on
`read_subagent` messages.
```
Be defensive: the `message` value may be a Python-repr-ish string in some dumps —
try `json.loads`; if it fails, keep the raw string as text. Redact message text
via redact (so pasted tokens in prompts are scrubbed).

### 3.11 `scan_history(limit:int=100, offset:int=0, q:str="") -> dict`
`history.jsonl`, newest first (file is oldest-first; reverse). Each line
`{display, pastedContents, timestamp(ms), project, sessionId}`.
```jsonc
{ "total": <total matching>, "limit":..., "offset":...,
  "items": [ {"display":"...(redacted, <=2000 chars)", "ts":<ms>, "ts_iso":"...",
              "project":"...", "session_id":"..."}, ... ] }
```
`q` (if non-empty) case-insensitive substring filter on display+project. Read the
whole file once; OK to hold lines in memory (≈1MB). Redact display text.

### 3.12 `read_file_safe(rel_path:str) -> dict`
For the "view raw" feature. Validate: resolved path MUST stay within claude_root;
extension MUST be in `{.json,.md,.txt,.jsonl,.sh,.toml,.yaml,.yml,.local,.js,.ts,
.mjs,.cjs,.py,.dart}` (text source only — served redacted, capped, read-only);
basename MUST NOT contain "credential"/"creds"/"token"/".env"; size cap 512 KB
(else return head). Return:
```jsonc
{ "path":"...", "rel":"...", "size":..., "truncated":bool,
  "content":"<redacted text>", "error": null }
```
On any violation: `{"error":"forbidden", "content":null, ...}`.

### 3.13 `scan_project_memory(project_key) -> dict`
The per-project auto-memory store at `projects/<key>/memory/`.
```jsonc
{ "project_key":"...", "real_path":"...", "exists": true,
  "index": "<MEMORY.md text, redacted, <=20k>" | null,
  "items": [ {"file":"x.md", "rel":"projects/<key>/memory/x.md",
              "name":"...", "description":"...(redacted)",
              "type":"user|project|feedback|reference|''",
              "size":.., "size_h":"..", "mtime_iso":".."} ],  // newest first; MEMORY.md excluded
  "count": <n>, "error": null }
```
`name`/`description`/`type` come from a tolerant `---` frontmatter parse (no yaml
dep; `type` read from the nested `metadata:` block). Memory file CONTENT is served
through the existing `/api/file?rel=...` (read_file_safe) using each item's `rel`.

### 3.14 `scan_project_subagents(project_key) -> dict`
Subagent/workflow invocations anywhere in the project. Two kinds: **workflow
runs** (structured `projects/<key>/<session>/workflows/wf_*.json`) and **direct
Task agents** (`projects/<key>/<session>/subagents/agent-*.jsonl`, NOT under
`workflows/`). Token totals are NOT computed here (cheap list) — surfaced on
drill-down.
```jsonc
{ "project_key":"...", "real_path":"...",
  "workflows": [ {"run_id":"wf_..", "session_id":"..", "name":"..",
                  "summary":"..(redacted)", "status":"completed|..",
                  "agent_count":N, "duration_ms":N|null, "model":"..",
                  "started_iso":"..", "phases":["Build",..],
                  "agents":[ {"label":"..","phase":"..","agent_id":".."} ]} ],
  "tasks": [ {"agent_id":"..","session_id":"..","task":"..(first prompt, redacted)",
              "size":..,"size_h":"..","mtime_iso":".."} ],
  "scripts": [ {"name":"..","run_id":"wf_..","session_id":"..","file":"..",
                "rel":"projects/<key>/<session>/workflows/scripts/<file>.js",
                "size":..,"size_h":"..","mtime_iso":".."} ],   // authored workflow
                // scripts (workflows/scripts/*.js) — a session may have ONLY these
                // (workflow written, no run json/agents); viewable via /api/file
  "counts": {"workflows":N,"workflow_agents":N,"tasks":N,"scripts":N}, "error": null }
```

### 3.15 `read_subagent(project_key, session_id, agent_id, run_id="", limit=400) -> dict`
Transcript of one subagent. If `run_id` is given → workflow agent at
`.../<session>/subagents/workflows/<run_id>/agent-<agent_id>.jsonl`; else direct
Task agent at `.../<session>/subagents/agent-<agent_id>.jsonl`. Same parsing,
per-message `tokens`, and whole-transcript `tokens` roll-up as `read_session`
(both share `_parse_transcript`). All path components validated (no `/`, `..`).
`list_sessions` sessions also gain `subagent_count` (agent transcripts under that
session's `subagents/` tree).

### 3.16 session runtime task outputs — a SECOND read root, outside `~/.claude`
`config.runtime_root()` resolves Claude Code's per-user session-runtime scratch
dir `/tmp/claude-<uid>` (macOS: `/private/tmp`; override via
`CLAUDE_CONSOLE_RUNTIME_ROOT`). Layout
`<project-key>/<session-id>/tasks/<task-id>.output` — the live stdout/result
capture of `run_in_background` Bash commands and background workflow/agent runs,
keyed by the SAME project-key + session-id as `projects/`. The dir is volatile
(the OS / harness prunes `/tmp`) and outputs can carry secrets, so every read is
size-capped and redacted. Only `tasks/*.output` is surfaced; loose `tmpXXXX`
scratch, `pytest-of-*`, `auto-mode-classifier-errors`, etc. are ignored as noise.
`list_sessions` sessions also gain `task_count` (cheap `*.output` count).
```python
list_session_tasks(project_key, session_id) -> dict   # {root, tasks:[{task_id,
    # kind:"bash|agent|other", size, size_h, mtime, mtime_iso}], count, error}
    # newest-first; kind is a best-effort hint from the id prefix (b…/w…).
read_task_output(project_key, session_id, task_id, limit_bytes=256*1024) -> dict
    # {kind, content:<redacted, capped>, size, truncated, error}
    # all three path parts validated (no '/', '..'); missing -> {"error":"not found"}
```
NOT wired into the watcher (out of `claude_root`); the tasks list refreshes when
the session expansion re-renders or the chip is clicked.

---

## 4. watcher.py  (builder B)

```python
class ClaudeWatcher:
    def __init__(self, root: Path, on_change):
        # on_change: Callable[[list[str] domains, str rel_path, str kind], None]
        # kind in {"created","modified","deleted","moved"}
    def start(self) -> None: ...   # schedule observers; non-blocking
    def stop(self) -> None: ...
```
Implementation: a single `watchdog.observers.Observer`. Schedule the curated set
to avoid event storms from 168 MB `projects/` and big caches:
  - root, recursive=False  (catches settings*.json, history.jsonl, CLAUDE.md, *.json)
  - `skills/`  recursive=True   (if exists)
  - `agents/`  recursive=True   (if exists)
  - `commands/` recursive=True  (if exists)
  - `plugins/` recursive=False  (installed_plugins.json, known_marketplaces.json)
  - `projects/` recursive=True  (if exists) — but the handler DROPS any event whose
    path contains a HEAVY_OR_NOISE_SEGMENT or IGNORE_SUFFIX, and only keeps
    `*.jsonl` / `sessions-index.json` under projects.
A `PatternMatchingEventHandler`-style filter: compute rel path; if any segment in
HEAVY_OR_NOISE_SEGMENTS (except the explicitly-watched `projects`) or suffix in
IGNORE_SUFFIXES → ignore. Else `domains = config.path_to_domains(rel)`; if domains
non-empty call `on_change(domains, rel, kind)`.
Debounce/coalescing is done on the SERVER side, not here (here just forward).
Guard against scheduling a non-existent dir.

---

## 5. server.py  (builder B)  — FastAPI app + WebSocket + static + main()

- Create `app = FastAPI(title="Claude Console")`.
- Mount static dir at `/` LAST (so /api and /ws win). Serve `static/index.html` at `GET /`.
- A `ConnectionManager` holding active WebSockets; `broadcast(json)` to all.
- On startup: instantiate `ClaudeWatcher(config.claude_root(), on_change=_schedule_emit)`,
  `.start()`. `_schedule_emit` pushes (domains,rel,kind,ts) into an asyncio.Queue
  (use `loop.call_soon_threadsafe` since watchdog runs in its own thread). A
  background task drains the queue with **300 ms debounce + domain coalescing**
  and broadcasts one `{"type":"change","domains":[...],"path":rel,"kind":kind,"ts":...}`.
- On shutdown: watcher.stop().

REST endpoints (all `GET`, all return the scanner dicts verbatim as JSON):
```
GET /api/health      -> {"ok":true,"root":..., "clients":<n>, "watching":true}
GET /api/overview    -> scan_overview()
GET /api/settings    -> read_settings()
GET /api/skills      -> scan_skills()
GET /api/plugins     -> scan_plugins()
GET /api/agents      -> scan_agents()
GET /api/commands    -> scan_commands()
GET /api/plans       -> scan_plans()
GET /api/hooks       -> scan_hooks()
GET /api/mcp         -> read_mcp()
GET /api/projects    -> scan_projects()
GET /api/projects/{key}/sessions          -> list_sessions(key)   // sessions now carry subagent_count
GET /api/projects/{key}/memory            -> scan_project_memory(key)
GET /api/projects/{key}/subagents         -> scan_project_subagents(key)
GET /api/sessions/{key}/{session_id}?limit=400 -> read_session(key, session_id, limit)
GET /api/sessions/{key}/{session_id}/tasks            -> list_session_tasks(key, session_id)
GET /api/sessions/{key}/{session_id}/tasks/{task_id}  -> read_task_output(key, session_id, task_id)
GET /api/subagents/{key}/{session_id}/{agent_id}?run=<run_id>&limit=400 -> read_subagent(...)
GET /api/history?limit=100&offset=0&q=    -> scan_history(limit,offset,q)
GET /api/file?rel=<relpath>               -> read_file_safe(rel)   // also serves memory/*.md content
POST /api/reveal/{key}?which=cwd|transcript -> open the project's real working dir
        (or its transcript dir) in the OS file manager. A side-effecting endpoint.
        Resolves via scanner.resolve_project_path (validated key), stats the path,
        and opens it only if it is a directory; args passed as a list (never a
        shell). Returns {ok, path, which, error}.
POST /api/reveal-path?rel=<relpath> -> reveal a path under ~/.claude in the file
        manager — a file is SELECTED (macOS `open -R`, Windows `explorer /select,`,
        Linux opens its parent), a directory is opened. Validated by
        scanner.resolve_reveal_path (within claude_root, no `..`/absolute; no file
        content is read). Used by the Skills view (card ↗ = skill dir, drawer
        ↗ Finder = the current source file). Returns {ok, path, error}.
```
These two POSTs are the only side-effecting endpoints; everything else is read-only.
WebSocket:
```
WS  /ws   -> on connect send {"type":"hello","root":..., "domains":[...all...]}
            then stream {"type":"change",...} messages. Read+ignore client msgs
            (keepalive). Remove from manager on disconnect.
```
Wrap each endpoint body so a scanner exception becomes HTTP 200 with
`{"error": str(e)}` (never 500 — the UI stays usable). Add permissive CORS for
localhost is unnecessary (same origin); skip.

`def main():` parse `--port` (default config.PORT), `--no-browser`; run
`uvicorn.run(app, host=config.HOST, port=PORT, log_level="info")`. If browser
enabled, `webbrowser.open` after a short delay via a thread. `if __name__=="__main__": main()`.
Module must be runnable as `python -m claude_console.server`.

---

## 6. Frontend  (builder C)  — static/index.html, app.js, style.css

Single-page app, **no framework, no CDN** (works offline). Aesthetic: dark
"developer console" — monospace accents, left sidebar nav, a live connection dot,
a sliding "live activity" feed. Anthropic-ish warm accent (#d97757) on dark slate.

Layout:
- Left sidebar: brand "⌁ Claude Console", nav items = the domains:
  Settings, Skills, Plugins, Agents, Commands, Hooks, MCP, Projects, History, Plans.
  (There is no Overview nav tab; `/api/overview` is still fetched on load /
  reload-all purely to populate the per-nav-item count badges.)
  A connection status dot (green=ws open, red=closed) + reload-all + a theme
  toggle (正常/light ↔ 夜间/dark). Theme is a `data-theme` attribute on
  `<html>` driven entirely by CSS variables; the choice persists in
  `localStorage["cc-theme"]` and is applied by a tiny `<head>` script before
  first paint (no flash). Dark is the default.
- Main panel: header (title + subtitle + "refresh" button + last-updated time),
  then domain-specific content rendered from the API JSON.
- A collapsible bottom "Live activity" strip: appends each WS change as
  `[hh:mm:ss] kind path → domains` (max ~50 rows, newest on top), and flashes the
  affected nav item.

Behavior (app.js, plain ES modules or one IIFE):
- `api(path)` helper → fetch JSON.
- On load: open `WebSocket(ws://<host>/ws)`; render Settings by default.
- Each nav click loads + renders that domain (lazy; cache last payload).
- On WS `change` with domains: NEVER auto-refetch (an actively-written session
  jsonl fires constantly; auto-reloading would flash the view). Drop the stale
  cache for each affected domain and mark it pending — nav dot + "updates
  available" cue + refresh-button glow — then the user pulls fresh data manually
  (header refresh for the domain; the drawer's `⟳` for an open transcript). Always
  push a live-activity row. Auto-reconnect WS with backoff. (Count badges refresh
  on load / reload-all.)
- Renderers per domain:
  * Settings: summary chips (effort, flags, hook events, plugin count), a
    pretty-printed + syntax-highlighted JSON viewer of redacted settings
    (`jsonViewer(obj, rich=false)` → strict-JSON mode of the shared `appendJson`
    DOM renderer: key/string/number/bool/null colored, no external lib, no
    innerHTML; `rich=false` keeps long command/path strings as inline literals
    instead of markdown blocks); if `claude_md.exists`, a "CLAUDE.md" section
    renders `~/.claude/CLAUDE.md` as markdown (`renderMarkdown`); list of settings
    files (size/mtime, backups dimmed).
  * Skills: cards with name + description (clamped) + has-references badge; the
    whole card is clickable → drawer renders `SKILL.md` as markdown plus a file
    switcher strip (`skills/<key>/<file>` via `/api/file`) — `.md` → markdown,
    `.json` → JSON viewer, other text → raw; build-output / cache / binary files
    are filtered out, non-viewable extensions shown inert. A `↗` on the card opens
    the skill's folder in Finder; a `↗ Finder` chip in the drawer selects the
    currently-viewed source file (both via POST /api/reveal-path).
  * Plugins: table — name, marketplace, version, enabled (toggle-look badge,
    read-only), scope; marketplaces listed above.
  * Plans: newest-first list of `plans/*.md` (title + name/size/mtime); row click →
    fetch `/api/file?rel=plans/<name>.md` → render markdown (`renderMarkdown`) in the
    drawer. Reuses the rich-content markdown renderer (`renderMarkdown`): fenced
    code, ATX headings, GFM tables, nested bullet/number lists, horizontal rules,
    paragraphs, and inline **bold** / `code` / safe http(s) links — all DOM-built,
    never innerHTML.
  * Agents / Commands: simple card list (name + description + path); empty-state
    message when count 0.
  * Hooks: per-event accordion → matcher + command(s) in <code> blocks.
  * MCP: cards — name, transport badge, command/url, env_keys as chips,
    needs-auth badge.
  * Projects: table — real_path, sessions count, size, last activity; row click →
    fetch sessions list → expand inline; session row click → fetch transcript →
    modal/drawer rendering messages (role-colored bubbles, model badge, block tags).
    Every drawer carries a `⟳` button (`setDrawerReload` registers a re-open thunk
    per opener) that re-fetches the current content, so an open transcript picks up
    appended jsonl lines on demand without closing/reopening.
    A session with `task_count > 0` shows a green `⚙ N` chip (background task
    outputs from `/tmp/claude-<uid>`); clicking it (stopPropagation, not the
    transcript) opens a tasks drawer → each task row opens its redacted, capped
    output through the rich content viewer: `renderRichContent` detects JSON
    (pretty-print + token highlight) and unified diffs (per-line +/-/@@/file
    coloring), else plain text, with a format chip + line count header. Inside a
    JSON view, a *rich* string value (long or multi-line) renders as an indented
    markdown block (`renderRichString` → `renderMarkdown`: headings, bullet/number
    lists, fenced code, inline **bold**/`code`/safe http(s) links) or an embedded
    diff, instead of an unreadable escaped one-liner; short scalars stay inline.
    Built entirely from DOM nodes / textContent — never innerHTML of untrusted
    text; link href is gated to http(s) so a `javascript:` URI cannot slip in.
  * History: search box (debounced → /api/history?q=), paginated list (load more),
    each row: time, project, display text (clamped). A row is clickable when its
    `session_id` maps to a real project dir: the cwd is sanitized to a key by
    replacing every non-`[A-Za-z0-9]` char with `-` (same rule Claude Code uses
    for `projects/<key>/`), and the key must be present in `/api/projects`. Such
    rows open that session's transcript in the same drawer as Projects→Sessions
    (`openTranscript(key, session_id, …)`); dead rows (session pruned / never
    saved) stay inert. The project-key set is fetched lazily on first History load
    and refreshed for free whenever the Projects tab renders.
- Robust to `{"error":...}` payloads: show an inline error banner, keep app alive.
- Escape ALL text inserted into DOM (no innerHTML of untrusted strings; use
  textContent / a small escape helper). This is a security requirement — session
  transcripts and history contain arbitrary user text.

style.css: dark theme, responsive-ish, sticky sidebar, smooth, no external fonts
(system + monospace stacks). Keep it genuinely polished, not generic.

---

## 7. tests + docs  (builder D)

`tests/conftest.py`: a pytest fixture `claude_home` that builds a SYNTHETIC mini
`~/.claude` tree in a tmp_path and sets `CLAUDE_CONSOLE_ROOT` +
`CLAUDE_CONSOLE_HOME_JSON` env for the duration. Include: settings.json (with a
secret env var like `"API_TOKEN":"sk-ant-xxxxxxxxxxxxxxxxxxxx"`, hooks, enabledPlugins,
permissions, effortLevel), one skill dir with SKILL.md frontmatter + references/,
plugins/installed_plugins.json + known_marketplaces.json, agents/ (empty),
history.jsonl (a few lines incl. one with a secret to prove redaction),
projects/<key>/<uuid>.jsonl (a few mixed-type records incl. user+assistant+snapshot)
+ sessions-index.json, and a .claude.json with mcpServers (one stdio w/ env TOKEN,
one http w/ url). Restore env after.

`test_redact.py`: is_secret_key, looks_like_secret_value (sk-ant, ghp_, JWT, AWS,
long hex; and NON-secrets like "hello", "1.0.0", short strings), redact_value,
redact_obj deep nesting + non-mutation of input.

`test_scanner.py`: against the fixture — overview counts correct; settings redacts
the API_TOKEN; skills parses name+description; plugins merges enabled flag;
mcp returns env_keys NOT values + redacts; history newest-first + redacts the
secret line + `q` filter; read_session flattens user/assistant + skips snapshots;
projects decodes key→real_path + session_count; read_file_safe rejects traversal
(`../../etc/passwd`) and credential-named files, accepts settings.json.

Aim ≥ 20 assertions across the suite. Tests must pass with `python -m pytest -q`
using only stdlib + pytest (pytest is fine to assume; if missing, tests use plain
`assert` and an `if __name__` runner — but write them as pytest functions).

`requirements.txt`: fastapi, uvicorn[standard], watchdog, websockets, pytest.
`run.sh`: `#!/usr/bin/env bash`, `cd` to script dir, `exec python3 -m claude_console.server "$@"`.
`README.md`: what it is, the read-only/redaction safety note, how to run
(`./run.sh` or `python3 -m claude_console.server`), the URL (http://127.0.0.1:8765),
domain list, how the live updates work, how to run tests, and a note that all deps
are usually already present.

---

## 8. Cross-cutting rules
- Read-only. No endpoint mutates the filesystem. No shelling out to `claude`.
- Bind 127.0.0.1 only. Redact everywhere user/secret data flows out. `_redact_inline`
  splits on whitespace RUNS and preserves them verbatim — redacting a token must
  never swallow the surrounding newlines/indentation, so multi-line content keeps
  its line structure (diffs/JSON/code render correctly).
- Never raise out of a scanner — degrade to `{"error": ...}`.
- All timestamps: keep epoch where noted; also provide `_iso` local-time strings.
- No third-party deps beyond the four installed + pytest. No network calls.
- Match these names/shapes EXACTLY so modules integrate without edits.
