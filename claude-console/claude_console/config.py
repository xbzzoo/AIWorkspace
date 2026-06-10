"""Configuration + path/domain mapping for Claude Console.

Pure, side-effect-free helpers. `claude_root()` / `home_claude_json()` are
re-read on every call so tests can override the location via env vars.
"""

import os
from pathlib import Path


def claude_root() -> Path:
    """Root of the Claude directory to inspect.

    Honors the CLAUDE_CONSOLE_ROOT override (used by tests); else ~/.claude.
    """
    return Path(os.environ.get("CLAUDE_CONSOLE_ROOT", Path.home() / ".claude"))


def home_claude_json() -> Path:
    """Path to the top-level ~/.claude.json (sibling of ~/.claude).

    Override via CLAUDE_CONSOLE_HOME_JSON for tests.
    """
    env = os.environ.get("CLAUDE_CONSOLE_HOME_JSON")
    if env:
        return Path(env)
    return claude_root().parent / ".claude.json"


def runtime_root() -> Path:
    """Root of Claude Code's per-user session-runtime scratch dir.

    This is `/tmp/claude-<uid>` (on macOS `/tmp` resolves to `/private/tmp`).
    It holds background-task output buffers laid out as
    `<project-key>/<session-id>/tasks/<task-id>.output` — the live stdout/result
    capture of `run_in_background` Bash commands and background workflow/agent
    runs, keyed by the SAME project-key + session-id as `projects/`. Outside
    `claude_root()`, so it is treated as a second, read-only source.

    Override via CLAUDE_CONSOLE_RUNTIME_ROOT for tests. The returned path may not
    exist (the dir is volatile — the OS / harness prunes it); callers must
    tolerate absence.
    """
    env = os.environ.get("CLAUDE_CONSOLE_RUNTIME_ROOT")
    if env:
        return Path(env)
    uid = os.getuid() if hasattr(os, "getuid") else os.environ.get("UID", "")
    return Path("/tmp") / ("claude-" + str(uid))


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
      'plans/foo.md' -> ['plans','overview']
      'projects/<key>/<id>.jsonl' -> ['projects','history','overview']
    Anything else -> ['overview'] (so size/counts refresh) — or [] if it is a
    noise segment (caller already filters those, but be defensive).
    """
    if rel_path is None:
        return ["overview"]

    # Normalize separators and strip leading "./" or "/".
    rel = str(rel_path).replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel:
        return ["overview"]

    parts = [p for p in rel.split("/") if p and p != "."]
    if not parts:
        return ["overview"]

    lower_parts = [p.lower() for p in parts]
    base = parts[-1]
    base_lower = base.lower()
    top = lower_parts[0]

    # Defensive: ignore pure noise segments / ignored suffixes.
    for seg in lower_parts:
        if seg in HEAVY_OR_NOISE_SEGMENTS:
            # `projects` is explicitly watched, so it is NOT in the noise set —
            # nothing to special-case here. Any other noise segment -> [].
            return []
    if base_lower.endswith(IGNORE_SUFFIXES):
        return []

    domains: list[str] = []

    def add(d: str) -> None:
        if d not in domains:
            domains.append(d)

    # --- root-level files ---
    if len(parts) == 1:
        if base_lower == "settings.json" or (
            base_lower.startswith("settings") and base_lower.endswith(".json")
        ):
            # settings fans out to settings + hooks + plugins (enabled map) + overview
            add("settings")
            add("hooks")
            add("plugins")
            add("overview")
            return domains
        if base_lower == "history.jsonl":
            add("history")
            add("overview")
            return domains
        # Any other root-level file (CLAUDE.md, *.json, etc.)
        add("overview")
        return domains

    # --- nested under a known top-level dir ---
    if top == "skills":
        add("skills")
        add("overview")
        return domains
    if top == "agents":
        add("agents")
        add("overview")
        return domains
    if top == "commands":
        add("commands")
        add("overview")
        return domains
    if top == "plans":
        add("plans")
        add("overview")
        return domains
    if top == "plugins":
        add("plugins")
        add("overview")
        return domains
    if top == "projects":
        add("projects")
        add("history")
        add("overview")
        return domains

    # Unknown nested path -> just refresh overview totals.
    add("overview")
    return domains
