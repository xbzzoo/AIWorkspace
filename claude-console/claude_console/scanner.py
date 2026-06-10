"""The read layer for Claude Console.

Every function reads from `config.claude_root()` (re-read each call so tests can
override env). All return JSON-serializable dicts/lists. All are robust to
missing files/dirs/corrupt JSON — NEVER raise; on error put an ``"error"`` string
in the relevant field and continue. Redaction is applied to anything derived from
settings env, mcp config, hook commands, history, session text, and raw reads.
"""

import ast
import datetime as _dt
import json
import os
import re
from pathlib import Path

from . import config
from .redact import redact_obj, redact_value, REDACTED

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(n: int) -> str:
    """Human-readable byte size, e.g. ``1.2 MB``."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    if n < 0:
        n = 0.0
    if n < 1024:
        return f"{int(n)} B"
    size = float(n)
    for unit in _UNITS[1:]:
        size /= 1024.0
        if size < 1024.0 or unit == _UNITS[-1]:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} {_UNITS[-1]}"


def safe_json(path):
    """Load JSON from ``path``; return the parsed object or ``None`` on any error."""
    try:
        p = Path(path)
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except Exception:
        return None


def _iso(epoch_seconds) -> str:
    """Local ISO-ish string for an epoch-seconds value; '' on failure."""
    try:
        return _dt.datetime.fromtimestamp(float(epoch_seconds)).isoformat()
    except Exception:
        return ""


def _ms_to_iso(ms) -> str:
    """Local ISO string for an epoch-milliseconds value; '' on failure."""
    try:
        return _dt.datetime.fromtimestamp(float(ms) / 1000.0).isoformat()
    except Exception:
        return ""


def file_meta(path) -> dict:
    """Metadata for a file path.

    Returns ``{name, path, size, size_h, mtime, mtime_iso}``. ``mtime`` is epoch
    SECONDS (float); ``mtime_iso`` is a local ISO string. Robust to stat errors.
    """
    p = Path(path)
    name = p.name
    size = 0
    mtime = 0.0
    try:
        st = p.stat()
        size = int(st.st_size)
        mtime = float(st.st_mtime)
    except Exception:
        pass
    return {
        "name": name,
        "path": str(p),
        "size": size,
        "size_h": human_size(size),
        "mtime": mtime,
        "mtime_iso": _iso(mtime) if mtime else "",
    }


def _now() -> float:
    return _dt.datetime.now().timestamp()


def _dir_size_fast(path: Path) -> int:
    """Sum file sizes under ``path`` using os.scandir (du-like). Never raises."""
    total = 0
    stack = [str(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except Exception:
                        continue
        except Exception:
            continue
    return total


def _count_lines(path: Path) -> int:
    """Count newline-delimited lines in a file via binary chunk reads. Cheap."""
    count = 0
    last_byte = b"\n"
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                count += chunk.count(b"\n")
                last_byte = chunk[-1:]
        # Count a trailing partial line with no final newline.
        if last_byte not in (b"\n", b""):
            count += 1
    except Exception:
        return 0
    return count


def _frontmatter(text: str) -> dict:
    """Tolerant manual parse of leading ``---`` YAML-ish frontmatter.

    Pulls ``name`` and ``description`` only, supporting ``>-`` / ``|`` folded
    blocks (join following more-indented lines until a less-indented key). No
    pyyaml dependency. Returns a dict that may contain 'name' and 'description'.
    """
    result: dict = {}
    if not text:
        return result
    # Normalize newlines; strip a UTF-8 BOM if present.
    text = text.lstrip("﻿")
    lines = text.split("\n")
    # Find the opening fence (first non-empty line must be ---).
    i = 0
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or lines[i].strip() not in ("---", "---\r"):
        return result
    start = i + 1
    end = None
    for j in range(start, len(lines)):
        if lines[j].strip() in ("---", "...", "---\r"):
            end = j
            break
    if end is None:
        # No closing fence — be tolerant, scan to end.
        end = len(lines)

    block = lines[start:end]

    def key_indent(line: str):
        """Return (indent, key, rest) if line is 'key: value', else None."""
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if not stripped or stripped.startswith("#"):
            return None
        if ":" not in stripped:
            return None
        key, _, rest = stripped.partition(":")
        key = key.strip()
        if not key or " " in key:
            return None
        return indent, key, rest.strip()

    n = 0
    while n < len(block):
        parsed = key_indent(block[n])
        if parsed is None:
            n += 1
            continue
        indent, key, rest = parsed
        key_l = key.lower()
        if key_l not in ("name", "description"):
            n += 1
            continue

        if rest in (">", ">-", "|", "|-", ">+", "|+"):
            # Folded/literal block: gather more-indented following lines.
            collected = []
            m = n + 1
            base_indent = None
            while m < len(block):
                ln = block[m]
                if ln.strip() == "":
                    collected.append("")
                    m += 1
                    continue
                cur_indent = len(ln) - len(ln.lstrip(" "))
                # Stop if we hit another key at <= the parent indent.
                nxt = key_indent(ln)
                if cur_indent <= indent and nxt is not None:
                    break
                if base_indent is None:
                    base_indent = cur_indent
                collected.append(ln[base_indent:] if base_indent else ln.strip())
                m += 1
            if rest.startswith(">"):
                # Folded: join lines with spaces, blank line -> newline.
                out_parts = []
                buf = []
                for c in collected:
                    if c == "":
                        if buf:
                            out_parts.append(" ".join(buf))
                            buf = []
                        out_parts.append("")
                    else:
                        buf.append(c.strip())
                if buf:
                    out_parts.append(" ".join(buf))
                value = "\n".join(out_parts).strip()
            else:
                value = "\n".join(collected).rstrip()
            result[key_l] = value
            n = m
            continue
        else:
            # Inline scalar (may be quoted).
            value = rest
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result[key_l] = value.strip()
            n += 1
            continue
    return result


def _first_markdown_line(text: str) -> str:
    """First non-empty, non-frontmatter, non-heading markdown line."""
    if not text:
        return ""
    lines = text.split("\n")
    i = 0
    # Skip a leading frontmatter block.
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i < len(lines) and lines[i].strip() == "---":
        i += 1
        while i < len(lines) and lines[i].strip() not in ("---", "..."):
            i += 1
        i += 1
    for j in range(i, len(lines)):
        s = lines[j].strip()
        if not s:
            continue
        # Strip leading markdown heading markers for a cleaner description.
        s2 = s.lstrip("#").strip()
        if s2:
            return s2
    return ""


def _read_text(path: Path, cap: int = 200_000) -> str:
    """Read a text file with a size cap; '' on error."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return fh.read(cap)
    except Exception:
        return ""


def _safe_listdir(path: Path):
    try:
        return sorted(os.listdir(path))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 3.1 Overview
# ---------------------------------------------------------------------------

def scan_overview() -> dict:
    root = config.claude_root()
    out = {
        "root": str(root),
        "exists": False,
        "generated_at": _now(),
        "totals": {"size": 0, "size_h": "0 B"},
        "counts": {"skills": 0, "plugins": 0, "projects": 0, "sessions": 0,
                   "history_entries": 0, "agents": 0, "commands": 0, "plans": 0},
        "subdirs": [],
        "top_files": [],
    }
    try:
        if not root.exists():
            return out
        out["exists"] = True

        total_size = 0
        subdirs = []
        top_files = []

        try:
            entries = list(os.scandir(root))
        except Exception:
            entries = []

        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    sz = _dir_size_fast(Path(entry.path))
                    total_size += sz
                    try:
                        mt = entry.stat(follow_symlinks=False).st_mtime
                    except Exception:
                        mt = 0.0
                    subdirs.append({
                        "name": entry.name,
                        "size": sz,
                        "size_h": human_size(sz),
                        "mtime": float(mt),
                        "mtime_iso": _iso(mt) if mt else "",
                        "is_noise": entry.name in config.HEAVY_OR_NOISE_SEGMENTS,
                    })
                elif entry.is_file(follow_symlinks=False):
                    try:
                        sz = entry.stat(follow_symlinks=False).st_size
                    except Exception:
                        sz = 0
                    total_size += sz
                    top_files.append({
                        "name": entry.name,
                        "size": int(sz),
                        "size_h": human_size(sz),
                    })
            except Exception:
                continue

        subdirs.sort(key=lambda d: d["size"], reverse=True)
        top_files.sort(key=lambda f: f["size"], reverse=True)
        out["subdirs"] = subdirs
        out["top_files"] = top_files
        out["totals"] = {"size": total_size, "size_h": human_size(total_size)}

        # Counts.
        out["counts"]["skills"] = _count_skill_dirs(root)
        out["counts"]["plugins"] = _count_plugins(root)
        proj_count, sess_count = _count_projects_sessions(root)
        out["counts"]["projects"] = proj_count
        out["counts"]["sessions"] = sess_count
        out["counts"]["history_entries"] = _count_lines(root / "history.jsonl")
        out["counts"]["agents"] = _count_md(root / "agents", recursive=False)
        out["counts"]["commands"] = _count_md(root / "commands", recursive=True)
        out["counts"]["plans"] = _count_md(root / "plans", recursive=False)
    except Exception as e:  # pragma: no cover - last-ditch guard
        out["error"] = str(e)
    return out


def _count_skill_dirs(root: Path) -> int:
    skills = root / "skills"
    count = 0
    try:
        if skills.is_dir():
            for entry in os.scandir(skills):
                try:
                    if entry.is_dir(follow_symlinks=False) and (
                            Path(entry.path) / "SKILL.md").exists():
                        count += 1
                except Exception:
                    continue
    except Exception:
        return count
    return count


def _count_plugins(root: Path) -> int:
    data = safe_json(root / "plugins" / "installed_plugins.json")
    if isinstance(data, dict):
        plugins = data.get("plugins")
        if isinstance(plugins, dict):
            return len(plugins)
    return 0


def _count_projects_sessions(root: Path):
    projects = root / "projects"
    proj_count = 0
    sess_count = 0
    try:
        if projects.is_dir():
            for entry in os.scandir(projects):
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    proj_count += 1
                    for f in os.scandir(entry.path):
                        try:
                            if f.is_file(follow_symlinks=False) and \
                                    f.name.endswith(".jsonl"):
                                sess_count += 1
                        except Exception:
                            continue
                except Exception:
                    continue
    except Exception:
        pass
    return proj_count, sess_count


def _count_md(path: Path, recursive: bool) -> int:
    count = 0
    try:
        if not path.is_dir():
            return 0
        if recursive:
            for dirpath, _dirs, files in os.walk(path):
                for f in files:
                    if f.endswith(".md"):
                        count += 1
        else:
            for entry in os.scandir(path):
                try:
                    if entry.is_file(follow_symlinks=False) and \
                            entry.name.endswith(".md"):
                        count += 1
                except Exception:
                    continue
    except Exception:
        return count
    return count


# ---------------------------------------------------------------------------
# 3.2 Settings
# ---------------------------------------------------------------------------

def _read_claude_md(path: Path) -> dict:
    """Read a CLAUDE.md instructions file (redacted, size-capped). Returns
    {exists, content, size, truncated}; exists=False when the file is absent."""
    info = {"exists": False, "content": None, "size": 0, "truncated": False}
    try:
        if not path.is_file():
            return info
        info["exists"] = True
        info["size"] = int(path.stat().st_size)
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(_SIZE_CAP + 1)
        if len(data) > _SIZE_CAP:
            data = data[:_SIZE_CAP]
            info["truncated"] = True
        info["content"] = _redact_inline(data)
    except Exception:
        pass
    return info


def read_settings() -> dict:
    root = config.claude_root()
    out = {
        "files": [],
        "active_path": str(root / "settings.json"),
        # ~/.claude/CLAUDE.md global instructions (shown if present).
        "claude_md": _read_claude_md(root / "CLAUDE.md"),
        "settings": None,
        "summary": {
            "effortLevel": None,
            "permissions": {"allow": [], "deny": [], "ask": []},
            "hook_events": [],
            "enabled_plugins_count": 0,
            "flags": {},
        },
        "error": None,
    }
    try:
        # Collect settings files.
        candidates = []
        for name in ("settings.json", "settings.local.json"):
            p = root / name
            if p.exists():
                candidates.append((p, False))
        # Backups: settings*.bak
        try:
            for entry in os.scandir(root):
                try:
                    if entry.is_file(follow_symlinks=False) and \
                            entry.name.startswith("settings") and \
                            entry.name.endswith(".bak"):
                        candidates.append((Path(entry.path), True))
                except Exception:
                    continue
        except Exception:
            pass

        files = []
        for p, is_backup in candidates:
            meta = file_meta(p)
            meta["is_backup"] = bool(is_backup)
            files.append(meta)
        out["files"] = files

        raw = safe_json(root / "settings.json")
        if raw is None:
            if not (root / "settings.json").exists():
                out["error"] = None  # simply missing
            else:
                out["error"] = "settings.json missing or corrupt"
            return out

        if not isinstance(raw, dict):
            out["error"] = "settings.json is not an object"
            return out

        redacted = redact_obj(raw)
        out["settings"] = redacted

        summary = out["summary"]
        summary["effortLevel"] = redacted.get("effortLevel")

        perms = raw.get("permissions") or {}
        if isinstance(perms, dict):
            for kind in ("allow", "deny", "ask"):
                val = perms.get(kind) or []
                if isinstance(val, list):
                    summary["permissions"][kind] = [
                        redact_value("", v) if isinstance(v, str) else redact_obj(v)
                        for v in val
                    ]

        hooks = raw.get("hooks") or {}
        if isinstance(hooks, dict):
            summary["hook_events"] = list(hooks.keys())

        enabled = raw.get("enabledPlugins") or {}
        if isinstance(enabled, dict):
            summary["enabled_plugins_count"] = sum(
                1 for v in enabled.values() if v is True)

        flags = {}
        for k, v in raw.items():
            if isinstance(v, bool):
                flags[k] = v
        summary["flags"] = flags
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# 3.3 Skills
# ---------------------------------------------------------------------------

def scan_skills() -> dict:
    root = config.claude_root()
    skills_dir = root / "skills"
    items = []
    try:
        if skills_dir.is_dir():
            for entry in os.scandir(skills_dir):
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    d = Path(entry.path)
                    skill_md = d / "SKILL.md"
                    if not skill_md.exists():
                        continue
                    fm = _frontmatter(_read_text(skill_md))
                    name = fm.get("name") or entry.name
                    description = fm.get("description") or _first_markdown_line(
                        _read_text(skill_md))
                    files = _shallow_files(d, limit=20)
                    has_refs = (d / "references").is_dir() or any(
                        f.startswith("references/") for f in files)
                    try:
                        mt = skill_md.stat().st_mtime
                    except Exception:
                        mt = 0.0
                    items.append({
                        "name": name,
                        "key": entry.name,          # dir basename → builds rel paths
                        "description": description,
                        "dir": str(d),
                        "has_references": bool(has_refs),
                        "files": files,
                        "mtime_iso": _iso(mt) if mt else "",
                    })
                except Exception:
                    continue
    except Exception:
        pass
    items.sort(key=lambda it: str(it.get("name", "")).lower())
    return {"count": len(items), "items": items}


def _shallow_files(d: Path, limit: int = 20) -> list:
    """Shallow-ish file list (relative paths), capped at ``limit``."""
    out = []
    try:
        for dirpath, dirs, files in os.walk(d):
            # Skip noise dirs.
            dirs[:] = [x for x in dirs if x not in config.HEAVY_OR_NOISE_SEGMENTS]
            rel_dir = os.path.relpath(dirpath, d)
            for f in sorted(files):
                rel = f if rel_dir == "." else f"{rel_dir}/{f}"
                out.append(rel.replace("\\", "/"))
                if len(out) >= limit:
                    return out
    except Exception:
        return out
    return out


# ---------------------------------------------------------------------------
# 3.4 Plugins
# ---------------------------------------------------------------------------

def scan_plugins() -> dict:
    root = config.claude_root()
    out = {"marketplaces": [], "items": [], "count": 0}
    try:
        installed = safe_json(root / "plugins" / "installed_plugins.json")
        markets = safe_json(root / "plugins" / "known_marketplaces.json")
        settings = safe_json(root / "settings.json")

        # Marketplaces.
        marketplaces = []
        if isinstance(markets, dict):
            for name, info in markets.items():
                if not isinstance(info, dict):
                    continue
                src = info.get("source")
                src_str = ""
                if isinstance(src, dict):
                    kind = src.get("source") or ""
                    repo = src.get("repo") or src.get("path") or ""
                    if kind and repo:
                        src_str = f"{kind}:{repo}"
                    else:
                        src_str = repo or kind
                elif isinstance(src, str):
                    src_str = src
                marketplaces.append({
                    "name": name,
                    "source": src_str,
                    "installLocation": info.get("installLocation", ""),
                    "lastUpdated": info.get("lastUpdated", ""),
                })
        marketplaces.sort(key=lambda m: str(m.get("name", "")).lower())
        out["marketplaces"] = marketplaces

        enabled_map = {}
        if isinstance(settings, dict):
            em = settings.get("enabledPlugins")
            if isinstance(em, dict):
                enabled_map = em

        installed_map = {}
        if isinstance(installed, dict):
            pl = installed.get("plugins")
            if isinstance(pl, dict):
                installed_map = pl

        keys = set()
        keys.update(installed_map.keys())
        keys.update(enabled_map.keys())

        items = []
        for key in keys:
            name, _, marketplace = str(key).partition("@")
            entry = {
                "key": key,
                "name": name or key,
                "marketplace": marketplace,
                "version": "",
                "enabled": enabled_map.get(key) is True,
                "scope": "",
                "installPath": "",
                "installedAt": "",
                "lastUpdated": "",
            }
            inst = installed_map.get(key)
            rec = None
            if isinstance(inst, list) and inst:
                rec = inst[0] if isinstance(inst[0], dict) else None
            elif isinstance(inst, dict):
                rec = inst
            if isinstance(rec, dict):
                entry["version"] = rec.get("version", "") or ""
                entry["scope"] = rec.get("scope", "") or ""
                entry["installPath"] = rec.get("installPath", "") or ""
                entry["installedAt"] = rec.get("installedAt", "") or ""
                entry["lastUpdated"] = rec.get("lastUpdated", "") or ""
            items.append(entry)

        items.sort(key=lambda it: str(it.get("key", "")).lower())
        out["items"] = items
        out["count"] = len(items)
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# 3.5 Agents & Commands
# ---------------------------------------------------------------------------

def _scan_md_items(base: Path, recursive: bool) -> list:
    items = []
    try:
        if not base.is_dir():
            return items
        md_paths = []
        if recursive:
            for dirpath, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs
                           if d not in config.HEAVY_OR_NOISE_SEGMENTS]
                for f in files:
                    if f.endswith(".md"):
                        md_paths.append(Path(dirpath) / f)
        else:
            for entry in os.scandir(base):
                try:
                    if entry.is_file(follow_symlinks=False) and \
                            entry.name.endswith(".md"):
                        md_paths.append(Path(entry.path))
                except Exception:
                    continue
        for p in md_paths:
            try:
                text = _read_text(p)
                fm = _frontmatter(text)
                name = fm.get("name") or p.stem
                description = fm.get("description") or _first_markdown_line(text)
                try:
                    mt = p.stat().st_mtime
                except Exception:
                    mt = 0.0
                items.append({
                    "name": name,
                    "description": description,
                    "path": str(p),
                    "mtime_iso": _iso(mt) if mt else "",
                })
            except Exception:
                continue
    except Exception:
        return items
    items.sort(key=lambda it: str(it.get("name", "")).lower())
    return items


def scan_agents() -> dict:
    root = config.claude_root()
    items = _scan_md_items(root / "agents", recursive=False)
    return {"count": len(items), "items": items}


def scan_commands() -> dict:
    root = config.claude_root()
    items = _scan_md_items(root / "commands", recursive=True)
    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------------
# 3.5b Plans — plan-mode documents persisted under plans/*.md
# ---------------------------------------------------------------------------

_PLAN_TITLE_PREFIX_RE = re.compile(r"^plan\s*[—:\-]\s*", re.IGNORECASE)


def _plan_title(head: str, fallback: str) -> str:
    """The plan's title = its first markdown heading line, with a leading
    'Plan —'/'Plan:'/'Plan -' prefix trimmed. Falls back to the file stem when
    there is no heading. `head` is just the first chunk of the file."""
    if isinstance(head, str):
        for line in head.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                s = s.lstrip("#").strip()
                if s:
                    return _PLAN_TITLE_PREFIX_RE.sub("", s) or s
            break  # first non-empty line isn't a heading → no title heading
    return fallback


def scan_plans() -> dict:
    """Plan-mode documents under `plans/*.md` — plain markdown (no frontmatter),
    conventionally `# Plan — <title>`. Newest-first. File CONTENT is served
    through the existing `/api/file?rel=plans/<name>.md` (read_file_safe), so no
    separate read endpoint is needed.
    ```jsonc
    { "count": 3,
      "items": [ {"name":"lively-soaring-moon", "title":"…",
                  "rel":"plans/lively-soaring-moon.md",
                  "size":.., "size_h":"..", "mtime_iso":".."} ] }
    ```
    """
    root = config.claude_root()
    base = root / "plans"
    out = {"count": 0, "items": []}
    try:
        if not base.is_dir():
            return out
        items = []
        for entry in os.scandir(base):
            try:
                if not (entry.is_file(follow_symlinks=False)
                        and entry.name.endswith(".md")):
                    continue
                p = Path(entry.path)
                # Only the head is needed for the title — avoid reading big files.
                head = ""
                try:
                    with p.open("r", encoding="utf-8", errors="replace") as fh:
                        head = fh.read(4096)
                except Exception:
                    head = ""
                st = entry.stat(follow_symlinks=False)
                items.append({
                    "name": p.stem,
                    "title": _plan_title(head, p.stem),
                    "rel": "plans/" + entry.name,
                    "size": int(st.st_size),
                    "size_h": human_size(st.st_size),
                    "mtime": float(st.st_mtime),
                    "mtime_iso": _iso(st.st_mtime),
                })
            except Exception:
                continue
        items.sort(key=lambda d: d.get("mtime", 0.0), reverse=True)
        for it in items:
            it.pop("mtime", None)
        out["items"] = items
        out["count"] = len(items)
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# 3.6 Hooks
# ---------------------------------------------------------------------------

def scan_hooks() -> dict:
    root = config.claude_root()
    out = {"events": [], "count": 0}
    try:
        settings = safe_json(root / "settings.json")
        if not isinstance(settings, dict):
            return out
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return out
        events = []
        for event_name, group in hooks.items():
            entries = []
            if isinstance(group, list):
                for g in group:
                    if not isinstance(g, dict):
                        continue
                    matcher = g.get("matcher")
                    hk_list = g.get("hooks")
                    safe_hooks = []
                    if isinstance(hk_list, list):
                        for h in hk_list:
                            if isinstance(h, dict):
                                safe_hooks.append(_redact_hook(h))
                    entries.append({"matcher": matcher, "hooks": safe_hooks})
            events.append({"event": event_name, "entries": entries})
        out["events"] = events
        out["count"] = len(events)
    except Exception as e:
        out["error"] = str(e)
    return out


def _redact_hook(h: dict) -> dict:
    """Redact a single hook dict (command string passed through redact)."""
    result = {}
    for k, v in h.items():
        if k == "command" and isinstance(v, str):
            # Do NOT truncate; only redact if it looks secret-y.
            result[k] = redact_value("", v)
        else:
            result[k] = redact_obj(v)
    return result


# ---------------------------------------------------------------------------
# 3.7 MCP
# ---------------------------------------------------------------------------

def read_mcp() -> dict:
    home_json = config.home_claude_json()
    root = config.claude_root()
    out = {"servers": [], "count": 0, "source": str(home_json), "error": None}
    try:
        data = safe_json(home_json)
        if data is None:
            if not Path(home_json).exists():
                out["error"] = None
            else:
                out["error"] = "could not parse .claude.json"
            return out
        if not isinstance(data, dict):
            out["error"] = ".claude.json is not an object"
            return out

        auth_cache = safe_json(root / "mcp-needs-auth-cache.json")
        needs_auth_keys = set()
        if isinstance(auth_cache, dict):
            needs_auth_keys = set(auth_cache.keys())

        servers_raw = data.get("mcpServers")
        if not isinstance(servers_raw, dict):
            return out

        servers = []
        for name, cfg in servers_raw.items():
            if not isinstance(cfg, dict):
                continue
            command = cfg.get("command")
            url = cfg.get("url")
            ctype = cfg.get("type")
            if command:
                transport = "stdio"
            elif ctype in ("http", "sse"):
                transport = ctype
            elif url:
                transport = "http"
            else:
                transport = ctype or "stdio"

            # env / headers: KEYS only, never values.
            env_keys = []
            env = cfg.get("env")
            if isinstance(env, dict):
                env_keys = list(env.keys())
            header_keys = []
            headers = cfg.get("headers")
            if isinstance(headers, dict):
                header_keys = list(headers.keys())

            servers.append({
                "name": name,
                "transport": transport,
                "command": command or "",
                "args": cfg.get("args") if isinstance(cfg.get("args"), list) else [],
                "url": url or "",
                "env_keys": env_keys,
                "header_keys": header_keys,
                "needs_auth": name in needs_auth_keys,
                "raw": redact_obj(cfg),
            })
        out["servers"] = servers
        out["count"] = len(servers)
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# Project key decode / validation
# ---------------------------------------------------------------------------

def _decode_project_key(key: str) -> str:
    """Decode a sanitized cwd dir name back to a best-effort real path.

    Leading '-' -> '/', internal '-' -> '/'. Lossy: sanitization maps BOTH '/'
    and '_' to '-', so e.g. `fcar_workspace` and `fcar/workspace` collide. Only
    a fallback — prefer the real cwd via _project_real_path().
    """
    if not key:
        return ""
    decoded = key.replace("-", "/")
    if not decoded.startswith("/"):
        decoded = "/" + decoded
    return decoded


_REALPATH_CACHE: dict = {}


def _cwd_in_jsonl(path):
    """First verbatim `cwd` in the head of one transcript jsonl, or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 80:
                    break
                if '"cwd"' not in line:
                    continue
                rec = _loads_loose(line.strip())
                if isinstance(rec, dict):
                    cwd = rec.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
    except Exception:
        pass
    return None


def _read_cwd_from_dir(proj_dir: Path):
    """The true cwd recorded inside a project's transcripts. Sanitizing a cwd
    into the dir name is lossy, but every user/assistant/system record stores
    the verbatim `cwd`. Pass 1 reads top-level session jsonls (fast, common);
    pass 2 (only when there are none — e.g. a project with just subagent data)
    does a bounded walk into nested transcripts. Returns the cwd or None."""
    try:
        for entry in os.scandir(proj_dir):
            if entry.is_file(follow_symlinks=False) \
                    and entry.name.endswith(".jsonl"):
                cwd = _cwd_in_jsonl(entry.path)
                if cwd:
                    return cwd
    except Exception:
        pass
    checked = 0
    try:
        for r, _dirs, files in os.walk(proj_dir):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                cwd = _cwd_in_jsonl(os.path.join(r, name))
                if cwd:
                    return cwd
                checked += 1
                if checked >= 30:
                    return None
    except Exception:
        pass
    return None


def _project_real_path(proj_dir: Path, key: str) -> str:
    """Unambiguous real path of a project — the verbatim `cwd` from a transcript,
    falling back to the lossy key decode only when no transcript carries one.
    Cached by the absolute project dir (cwd is stable; keying by dir not bare key
    keeps it correct if the root changes). The lossy fallback is NOT cached so a
    project that later gains a transcript can upgrade."""
    ck = str(proj_dir)
    cached = _REALPATH_CACHE.get(ck)
    if cached is not None:
        return cached
    cwd = _read_cwd_from_dir(proj_dir)
    if cwd:
        _REALPATH_CACHE[ck] = cwd
        return cwd
    return _decode_project_key(key)


def resolve_project_path(project_key: str, which: str = "cwd") -> dict:
    """Resolve a project key to an absolute path for the 'reveal in file manager'
    action. which='cwd' -> the real working directory (from a transcript's cwd);
    which='transcript' -> the ~/.claude/projects/<key> transcript dir. Pure: it
    only computes + stats the path; the OS-open side effect lives in the server.
    Returns {path, exists, is_dir, which, error}."""
    out = {"path": None, "exists": False, "is_dir": False,
           "which": which, "error": None}
    if not _valid_project_key(project_key):
        out["error"] = "invalid project key"
        return out
    root = config.claude_root()
    proj_dir = root / "projects" / project_key
    if which == "transcript":
        path = str(proj_dir)
    else:
        out["which"] = "cwd"
        path = _project_real_path(proj_dir, project_key)
    out["path"] = path
    try:
        p = Path(path)
        out["exists"] = p.exists()
        out["is_dir"] = p.is_dir()
    except Exception:
        pass
    return out


def resolve_reveal_path(rel_path: str) -> dict:
    """Resolve a path under claude_root for the 'reveal in file manager' action
    (e.g. a skill's dir or source file). Validates the path stays within
    claude_root and exists. NO file content is read — only a validated absolute
    path is returned for the OS file manager. Returns {path, is_dir, error}."""
    out = {"path": "", "is_dir": False, "error": None}
    if not isinstance(rel_path, str) or not rel_path:
        out["error"] = "forbidden"
        return out
    rel = rel_path.replace("\\", "/").strip()
    # Reject absolute paths and traversal — only root-relative paths allowed.
    if rel.startswith("/"):
        out["error"] = "forbidden"
        return out
    parts = [p for p in rel.split("/") if p and p != "."]
    if not parts or ".." in parts:
        out["error"] = "forbidden"
        return out
    try:
        root_r = config.claude_root().resolve()
        target = (root_r / Path(*parts)).resolve()
        if target != root_r and root_r not in target.parents:
            out["error"] = "forbidden"
            return out
        if not target.exists():
            out["error"] = "not found"
            return out
        out["path"] = str(target)
        out["is_dir"] = target.is_dir()
    except Exception as e:
        out["error"] = str(e)
    return out


_VALID_KEY_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_VALID_SESSION_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
_VALID_TASK_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _valid_project_key(key: str) -> bool:
    if not isinstance(key, str) or not key:
        return False
    if ".." in key or "/" in key or "\\" in key:
        return False
    return all(c in _VALID_KEY_CHARS for c in key)


def _valid_session_id(sid: str) -> bool:
    if not isinstance(sid, str) or not sid:
        return False
    if ".." in sid or "/" in sid or "\\" in sid:
        return False
    return all(c in _VALID_SESSION_CHARS for c in sid)


# ---------------------------------------------------------------------------
# 3.8 Projects
# ---------------------------------------------------------------------------

def scan_projects() -> dict:
    root = config.claude_root()
    projects_dir = root / "projects"
    items = []
    try:
        if projects_dir.is_dir():
            for entry in os.scandir(projects_dir):
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    d = Path(entry.path)
                    session_count = 0
                    size = 0
                    last_activity = 0.0
                    # Recursive total: `size` and `last_activity` reflect ALL
                    # content (incl. subagents/workflows/scripts nested in session
                    # subdirs), not just top-level transcript files — otherwise a
                    # project whose content lives only in a session subdir reports
                    # 0 B / mtime 0. Cheap: ~700 files across all projects (~30ms).
                    top = True
                    for root_dir, subdirs, files in os.walk(str(d)):
                        if top:
                            for sd in subdirs:   # seed activity from session subdirs
                                try:
                                    m = os.stat(os.path.join(root_dir, sd)).st_mtime
                                    if m > last_activity:
                                        last_activity = m
                                except Exception:
                                    pass
                            for name in files:
                                if name.endswith(".jsonl"):
                                    session_count += 1   # top-level transcripts
                            top = False
                        for name in files:
                            try:
                                st = os.stat(os.path.join(root_dir, name))
                                size += st.st_size
                                if st.st_mtime > last_activity:
                                    last_activity = st.st_mtime
                            except Exception:
                                continue
                    items.append({
                        "key": entry.name,
                        "real_path": _decode_project_key(entry.name),
                        "session_count": session_count,
                        "size": int(size),
                        "size_h": human_size(size),
                        "last_activity": float(last_activity),
                        "last_activity_iso": _iso(last_activity)
                        if last_activity else "",
                    })
                except Exception:
                    continue
    except Exception:
        pass
    items.sort(key=lambda it: it.get("last_activity", 0.0), reverse=True)
    return {"count": len(items), "items": items}


# ---------------------------------------------------------------------------
# Token usage accounting (per session transcript)
# ---------------------------------------------------------------------------

# Maps our normalized field -> the Anthropic `usage` key in assistant records.
_USAGE_MAP = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cache_read", "cache_read_input_tokens"),
    ("cache_creation", "cache_creation_input_tokens"),
)

# Cache token tallies by file path -> ((mtime, size), result). Invalidated
# automatically when the transcript is appended to (mtime/size change), which is
# exactly what the watcher signals on, so live sessions stay accurate and cheap.
_TOKEN_CACHE: dict = {}


def _empty_tokens() -> dict:
    return {"input": 0, "output": 0, "cache_read": 0,
            "cache_creation": 0, "total": 0, "messages": 0}


def _usage_one(usage) -> dict:
    """Normalize a single assistant `usage` dict to our flat token shape.

    Only the TOP-LEVEL counters are summed — never the nested `iterations`
    breakdown (that would double-count, since the top level already aggregates
    a multi-step turn).
    """
    r = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0}
    if isinstance(usage, dict):
        for key, src in _USAGE_MAP:
            v = usage.get(src)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                r[key] = int(v)
        r["total"] = r["input"] + r["output"] + r["cache_read"] + r["cache_creation"]
    return r


def session_token_usage(path) -> dict:
    """Sum token usage across every assistant record in a session jsonl.

    Returns {input, output, cache_read, cache_creation, total, messages} where
    `messages` is the count of assistant turns that carried usage. Result is
    cached by (path, mtime, size). Never raises — returns zeros on any error.
    """
    try:
        p = Path(path)
        st = p.stat()
        sig = (st.st_mtime, st.st_size)
    except Exception:
        return _empty_tokens()

    key = str(p)
    cached = _TOKEN_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return dict(cached[1])

    acc = _empty_tokens()
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                # Cheap pre-filter: usage only ever lives in assistant records,
                # which always contain the substring 'usage'. Skip the rest
                # without paying for a JSON parse.
                if "usage" not in line:
                    continue
                rec = _loads_loose(line.strip())
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                msg = rec.get("message")
                if isinstance(msg, str):
                    msg = _loads_loose(msg)
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                one = _usage_one(usage)
                acc["input"] += one["input"]
                acc["output"] += one["output"]
                acc["cache_read"] += one["cache_read"]
                acc["cache_creation"] += one["cache_creation"]
                acc["total"] += one["total"]
                acc["messages"] += 1
    except Exception:
        return _empty_tokens()

    try:
        _TOKEN_CACHE[key] = (sig, dict(acc))
    except Exception:
        pass
    return acc


# ---------------------------------------------------------------------------
# 3.9 list_sessions
# ---------------------------------------------------------------------------

def list_sessions(project_key: str) -> dict:
    out = {
        "project_key": project_key,
        "real_path": "",
        "sessions": [],
        "count": 0,
        "error": None,
    }
    if not _valid_project_key(project_key):
        out["error"] = "invalid project key"
        return out
    root = config.claude_root()
    proj_dir = root / "projects" / project_key
    out["real_path"] = _decode_project_key(project_key)
    try:
        if not proj_dir.is_dir():
            out["error"] = None  # no such project (empty result)
            return out

        index = safe_json(proj_dir / "sessions-index.json")
        sessions = []
        index_by_id = {}
        if isinstance(index, dict):
            entries = index.get("entries")
            if isinstance(entries, list):
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    sid = e.get("sessionId") or ""
                    index_by_id[sid] = e

        # Always derive the file list (authoritative for size/mtime).
        jsonl_files = {}
        for f in os.scandir(proj_dir):
            try:
                if f.is_file(follow_symlinks=False) and f.name.endswith(".jsonl"):
                    jsonl_files[f.name[:-len(".jsonl")]] = Path(f.path)
            except Exception:
                continue

        # Build a unified key set (prefer index when present).
        all_ids = set(jsonl_files.keys()) | set(index_by_id.keys())
        for sid in all_ids:
            path = jsonl_files.get(sid)
            idx = index_by_id.get(sid, {})
            first_prompt = ""
            summary = ""
            size = 0
            mtime = 0.0
            if isinstance(idx, dict):
                first_prompt = idx.get("firstPrompt") or ""
                summary = idx.get("summary") or ""
                fm = idx.get("fileMtime")
                if isinstance(fm, (int, float)):
                    mtime = float(fm) / 1000.0
            if path is not None:
                try:
                    st = path.stat()
                    size = int(st.st_size)
                    if not mtime:
                        mtime = float(st.st_mtime)
                    else:
                        mtime = float(st.st_mtime)
                except Exception:
                    pass
                if not first_prompt:
                    first_prompt = _first_user_prompt(path)
            tokens = session_token_usage(path) if path is not None \
                else _empty_tokens()
            sessions.append({
                "session_id": sid,
                "first_prompt": _redact_inline(first_prompt)
                if isinstance(first_prompt, str) else first_prompt,
                "summary": _redact_inline(summary)
                if isinstance(summary, str) else summary,
                "mtime_iso": _iso(mtime) if mtime else "",
                "size": size,
                "size_h": human_size(size),
                "tokens": tokens,
                "subagent_count": _count_session_subagents(proj_dir / sid),
                "task_count": _count_session_tasks(project_key, sid),
                "_mtime": mtime,
            })

        sessions.sort(key=lambda s: s.get("_mtime", 0.0), reverse=True)
        for s in sessions:
            s.pop("_mtime", None)

        # Project-level token roll-up across all its sessions.
        totals = _empty_tokens()
        for s in sessions:
            t = s.get("tokens") or {}
            for k in totals:
                v = t.get(k)
                if isinstance(v, (int, float)):
                    totals[k] += int(v)

        out["sessions"] = sessions
        out["count"] = len(sessions)
        out["totals"] = {"tokens": totals, "sessions": len(sessions)}
    except Exception as e:
        out["error"] = str(e)
    return out


def _count_session_subagents(sdir: Path) -> int:
    """Count agent-*.jsonl transcripts under a session dir's subagents/ tree
    (direct Task agents + workflow agents). Cheap: short-circuits when the
    session has no subagents/ dir (the common case)."""
    sa = sdir / "subagents"
    if not sa.is_dir():
        return 0
    n = 0
    try:
        for e in os.scandir(sa):
            if e.is_file(follow_symlinks=False) and \
                    e.name.startswith("agent-") and e.name.endswith(".jsonl"):
                n += 1
        wf = sa / "workflows"
        if wf.is_dir():
            for run in os.scandir(wf):
                if not run.is_dir(follow_symlinks=False):
                    continue
                for e in os.scandir(run.path):
                    if e.is_file(follow_symlinks=False) and \
                            e.name.startswith("agent-") and \
                            e.name.endswith(".jsonl"):
                        n += 1
    except Exception:
        pass
    return n


# ---------------------------------------------------------------------------
# 3.16 Session runtime task outputs  (/tmp/claude-<uid>/<key>/<session>/tasks/)
#
# Background-task output buffers: the live stdout/result capture of
# run_in_background Bash commands and background workflow/agent runs. Keyed by
# the SAME project-key + session-id as projects/, so they attach to the sessions
# list_sessions already returns. Volatile (the OS prunes /tmp) and may carry
# secrets in command output, so reads are capped and redacted.
# ---------------------------------------------------------------------------

_TASK_OUTPUT_CAP = 256 * 1024  # bytes returned per output view


def _valid_task_id(tid: str) -> bool:
    if not isinstance(tid, str) or not tid or len(tid) > 128:
        return False
    if ".." in tid or "/" in tid or "\\" in tid:
        return False
    return all(c in _VALID_TASK_CHARS for c in tid)


def _task_kind(tid: str) -> str:
    """Best-effort label from the harness task-id prefix — informational only:
    'b…' = background Bash, 'w…' = background workflow/agent, else 'other'."""
    if not isinstance(tid, str) or not tid:
        return "other"
    if tid[0] == "b":
        return "bash"
    if tid[0] == "w":
        return "agent"
    return "other"


def _session_tasks_dir(project_key: str, session_id: str) -> Path:
    return config.runtime_root() / project_key / session_id / "tasks"


def _count_session_tasks(project_key: str, session_id: str) -> int:
    """Cheap count of `*.output` task buffers for a session. Short-circuits when
    the runtime tasks dir is absent (the common case)."""
    if not (_valid_project_key(project_key) and _valid_session_id(session_id)):
        return 0
    d = _session_tasks_dir(project_key, session_id)
    if not d.is_dir():
        return 0
    n = 0
    try:
        for e in os.scandir(d):
            if e.is_file(follow_symlinks=False) and e.name.endswith(".output"):
                n += 1
    except Exception:
        pass
    return n


def list_session_tasks(project_key: str, session_id: str) -> dict:
    """List the background-task output buffers for one session.
    ```jsonc
    { "project_key":"...", "session_id":"...", "root":"/tmp/claude-<uid>",
      "tasks": [ {"task_id":"b18if3ptd", "kind":"bash|agent|other",
                  "size":..,"size_h":"..","mtime":..,"mtime_iso":".."} ],
                  // newest first
      "count": <n>, "error": null }
    ```
    Never raises; tolerates the volatile dir being absent."""
    out = {"project_key": project_key, "session_id": session_id,
           "root": str(config.runtime_root()), "tasks": [], "count": 0,
           "error": None}
    if not (_valid_project_key(project_key) and _valid_session_id(session_id)):
        out["error"] = "invalid key"
        return out
    d = _session_tasks_dir(project_key, session_id)
    try:
        if not d.is_dir():
            return out
        items = []
        for e in os.scandir(d):
            if not (e.is_file(follow_symlinks=False)
                    and e.name.endswith(".output")):
                continue
            tid = e.name[:-len(".output")]
            try:
                st = e.stat(follow_symlinks=False)
                size = int(st.st_size)
                mtime = float(st.st_mtime)
            except Exception:
                size, mtime = 0, 0.0
            items.append({
                "task_id": tid,
                "kind": _task_kind(tid),
                "size": size,
                "size_h": human_size(size),
                "mtime": mtime,
                "mtime_iso": _iso(mtime) if mtime else "",
            })
        items.sort(key=lambda d: d.get("mtime", 0.0), reverse=True)
        out["tasks"] = items
        out["count"] = len(items)
    except Exception as e:
        out["error"] = str(e)
    return out


def read_task_output(project_key: str, session_id: str, task_id: str,
                     limit_bytes: int = _TASK_OUTPUT_CAP) -> dict:
    """Read one task output buffer (redacted, size-capped) for the drawer.
    ```jsonc
    { "project_key":"...", "session_id":"...", "task_id":"...",
      "kind":"bash|agent|other", "content":"<redacted text>",
      "size":<bytes on disk>, "truncated":bool, "error": null }
    ```
    All three path components are validated (no `/`, no `..`). On any violation
    or missing file: `{"error": "...", "content": null, ...}`."""
    out = {"project_key": project_key, "session_id": session_id,
           "task_id": task_id, "kind": _task_kind(task_id),
           "content": None, "size": 0, "truncated": False, "error": None}
    if not (_valid_project_key(project_key) and _valid_session_id(session_id)
            and _valid_task_id(task_id)):
        out["error"] = "invalid key"
        return out
    path = _session_tasks_dir(project_key, session_id) / (task_id + ".output")
    try:
        if not path.is_file():
            out["error"] = "not found"
            return out
        out["size"] = int(path.stat().st_size)
        try:
            limit_bytes = int(limit_bytes)
        except Exception:
            limit_bytes = _TASK_OUTPUT_CAP
        if limit_bytes <= 0:
            limit_bytes = _TASK_OUTPUT_CAP
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(limit_bytes + 1)
        if len(data) > limit_bytes:
            data = data[:limit_bytes]
            out["truncated"] = True
        out["content"] = _redact_inline(data)
    except Exception as e:
        out["error"] = str(e)
    return out


def _first_user_prompt(path: Path) -> str:
    """First user message text from a session jsonl. Best effort, capped."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _i, line in enumerate(fh):
                if _i > 200:
                    break
                line = line.strip()
                if not line:
                    continue
                rec = _loads_loose(line)
                if not isinstance(rec, dict):
                    continue
                if rec.get("type") != "user":
                    continue
                if rec.get("isMeta") is True:
                    continue
                msg = rec.get("message")
                text = _flatten_message_content(msg)[0]
                if text:
                    return text[:300]
    except Exception:
        return ""
    return ""


# ---------------------------------------------------------------------------
# 3.10 read_session
# ---------------------------------------------------------------------------

_SKIP_RECORD_TYPES = {"file-history-snapshot", "attachment", "last-prompt"}


def _loads_loose(s):
    """Try json.loads; fall back to python-literal eval; else return the string."""
    if not isinstance(s, str):
        return s
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        return ast.literal_eval(s)
    except Exception:
        return s


def _flatten_message_content(message):
    """Return (text_preview, block_tags) from a message value.

    `message` may be a dict {role, content}, a JSON/py-repr string, or plain str.
    Content may be a string or a list of typed blocks.
    """
    block_tags: list[str] = []
    if message is None:
        return "", block_tags

    if isinstance(message, str):
        parsed = _loads_loose(message)
        if isinstance(parsed, (dict, list)):
            message = parsed
        else:
            return str(parsed), block_tags

    content = None
    if isinstance(message, dict):
        content = message.get("content")
    elif isinstance(message, list):
        content = message
    else:
        return str(message), block_tags

    if isinstance(content, str):
        if content:
            block_tags.append("text")
        return content, block_tags

    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                block_tags.append("text")
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type") or "text"
            if btype == "text":
                t = block.get("text") or ""
                if t:
                    parts.append(t)
                block_tags.append("text")
            elif btype == "tool_use":
                name = block.get("name") or "tool"
                block_tags.append(f"tool_use:{name}")
                parts.append(f"[tool_use: {name}]")
            elif btype == "tool_result":
                block_tags.append("tool_result")
                rc = block.get("content")
                if isinstance(rc, str):
                    parts.append(rc)
                elif isinstance(rc, list):
                    for sub in rc:
                        if isinstance(sub, dict) and sub.get("type") == "text":
                            parts.append(sub.get("text") or "")
                else:
                    parts.append("[tool_result]")
            elif btype == "thinking":
                block_tags.append("thinking")
                parts.append("[thinking]")
            elif btype == "image":
                block_tags.append("image")
                parts.append("[image]")
            else:
                block_tags.append(str(btype))
                t = block.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
        return "\n".join(p for p in parts if p), block_tags

    if content is None:
        return "", block_tags
    return str(content), block_tags


# User-role records whose text starts with one of these are NOT a genuine
# user question — they are slash-command machinery, command/tool output, hook
# injections, or system notifications that merely ride on the "user" role.
_USER_NOISE_PREFIXES = (
    "<local-command-caveat>", "<local-command-stdout>", "<command-name>",
    "<command-message>", "<command-args>", "<command-stdout>",
    "<task-notification>", "<system-reminder>", "<user-prompt-submit-hook>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>",
    "[Request interrupted", "Caveat:",
)


def _is_user_prompt(rtype, role, block_tags, text) -> bool:
    """True only for a genuine user-typed question/request — used to visually
    highlight what the user actively asked, separate from tool results, slash
    commands, command stdout, and system notifications (all 'user' role too).
    Meta user records are already dropped before this is called."""
    if rtype != "user" or role != "user":
        return False
    if "tool_result" in (block_tags or []):
        return False
    if not isinstance(text, str):
        return False
    t = text.lstrip()
    if not t:
        return False
    return not t.startswith(_USER_NOISE_PREFIXES)


def _parse_transcript(path, limit: int):
    """Parse a transcript jsonl (session OR subagent) into display messages.

    Returns (messages, truncated). Shared by read_session and read_subagent —
    the two file kinds use the identical record schema. Caller handles
    existence + exceptions. Each message carries `is_prompt` flagging genuine
    user questions.
    """
    messages = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = _loads_loose(line)
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype in _SKIP_RECORD_TYPES:
                continue
            if rtype == "user" and rec.get("isMeta") is True:
                continue
            if rtype not in ("user", "assistant", "system"):
                # Unknown structural record; skip if no message payload.
                if "message" not in rec:
                    continue

            msg = rec.get("message")
            role = None
            model = None
            m_tokens = None
            if isinstance(msg, dict):
                role = msg.get("role")
                model = msg.get("model")
                _u = msg.get("usage")
                if isinstance(_u, dict):
                    m_tokens = _usage_one(_u)["total"]
            if role is None:
                role = rtype

            text, block_tags = _flatten_message_content(msg)
            # Classify BEFORE redaction: redaction can rewrite a leading marker
            # (e.g. a tool-use-id inside <task-notification>) and let noise slip
            # past the prompt filter. The raw text is only inspected, never shown.
            is_prompt = _is_user_prompt(rtype, role, block_tags, text)
            # Redact any pasted secrets in transcript text (inline so
            # surrounding context survives).
            if isinstance(text, str) and text:
                text = _redact_inline(text)
            if isinstance(text, str) and len(text) > 4000:
                text = text[:4000]

            ts = rec.get("timestamp")
            ts_iso = ""
            if isinstance(ts, str):
                ts_iso = ts  # already ISO in real dumps
            elif isinstance(ts, (int, float)):
                ts_iso = _ms_to_iso(ts) if ts > 1e11 else _iso(ts)

            messages.append({
                "role": role,
                "type": rtype,
                "text": text,
                "ts_iso": ts_iso,
                "model": model,
                "blocks": block_tags,
                "tokens": m_tokens,
                "is_prompt": is_prompt,
            })
    # Keep the LAST `limit` messages — the tail is where appended/most-recent
    # content lives, so an ongoing session always shows its latest turns (the
    # old head-truncation dropped exactly what the user was looking for).
    truncated = len(messages) > limit
    if truncated:
        messages = messages[-limit:]
    return messages, truncated


def read_session(project_key: str, session_id: str, limit: int = 400) -> dict:
    out = {
        "project_key": project_key,
        "session_id": session_id,
        "messages": [],
        "count": 0,
        "truncated": False,
        "error": None,
    }
    if not _valid_project_key(project_key):
        out["error"] = "invalid project key"
        return out
    if not _valid_session_id(session_id):
        out["error"] = "invalid session id"
        return out
    try:
        limit = int(limit)
    except Exception:
        limit = 400
    if limit <= 0:
        limit = 400

    root = config.claude_root()
    path = root / "projects" / project_key / (session_id + ".jsonl")
    try:
        if not path.exists():
            out["error"] = None  # missing -> empty
            return out
        messages, truncated = _parse_transcript(path, limit)
        out["messages"] = messages
        out["truncated"] = truncated
        out["count"] = len(messages)
        out["prompt_count"] = sum(1 for m in messages if m.get("is_prompt"))
        # Whole-session token roll-up (independent of the display limit).
        out["tokens"] = session_token_usage(path)
    except Exception as e:
        out["error"] = str(e)
    return out


_WS_SPLIT_RE = re.compile(r"(\s+)")


def _redact_inline(text: str) -> str:
    """Redact secret-looking tokens embedded in a longer text block.

    Splits on whitespace RUNS while preserving them as separators, tests each
    word token (stripped of surrounding punctuation), and replaces only the
    secret tokens with the placeholder. Original whitespace — newlines, tabs,
    indentation — is preserved verbatim, so multi-line content (diffs, JSON,
    code) keeps its line structure even when a token is redacted.
    """
    from .redact import looks_like_secret_value
    if not isinstance(text, str) or not text:
        return text
    # Fast path: if the whole stripped value is itself one secret token.
    stripped = text.strip()
    if stripped and " " not in stripped and "\t" not in stripped \
            and "\n" not in stripped and looks_like_secret_value(stripped):
        return REDACTED
    # Odd indices are whitespace runs (kept as-is); even indices are word tokens.
    parts = _WS_SPLIT_RE.split(text)
    changed = False
    for i in range(0, len(parts), 2):
        tok = parts[i]
        core = tok.strip("\"'`,;:()[]{}<>")
        if core and looks_like_secret_value(core):
            parts[i] = tok.replace(core, REDACTED)
            changed = True
    return "".join(parts) if changed else text


# ---------------------------------------------------------------------------
# 3.11 History
# ---------------------------------------------------------------------------

def scan_history(limit: int = 100, offset: int = 0, q: str = "") -> dict:
    root = config.claude_root()
    try:
        limit = int(limit)
    except Exception:
        limit = 100
    try:
        offset = int(offset)
    except Exception:
        offset = 0
    if limit < 0:
        limit = 100
    if offset < 0:
        offset = 0
    q = (q or "").strip().lower()

    out = {"total": 0, "limit": limit, "offset": offset, "items": []}
    path = root / "history.jsonl"
    try:
        if not path.exists():
            return out
        rows = []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = _loads_loose(line)
                if not isinstance(rec, dict):
                    continue
                display = rec.get("display")
                if display is None:
                    display = ""
                display = str(display)
                project = rec.get("project") or ""
                # q filter on display + project (pre-redaction view text).
                if q:
                    hay = (display + " " + str(project)).lower()
                    if q not in hay:
                        continue
                ts = rec.get("timestamp")
                ts_ms = None
                try:
                    ts_ms = int(ts)
                except Exception:
                    try:
                        ts_ms = int(float(ts))
                    except Exception:
                        ts_ms = None
                rows.append({
                    "display": display,
                    "ts": ts_ms,
                    "project": project,
                    "session_id": rec.get("sessionId") or "",
                })

        # Newest first (file is oldest-first).
        rows.reverse()
        out["total"] = len(rows)

        page = rows[offset:offset + limit] if limit else rows[offset:]
        items = []
        for r in page:
            disp = r["display"]
            # Redact display text (inline; embedded tokens scrubbed).
            disp = _redact_inline(disp)
            if len(disp) > 2000:
                disp = disp[:2000]
            ts = r["ts"]
            items.append({
                "display": disp,
                "ts": ts,
                "ts_iso": _ms_to_iso(ts) if isinstance(ts, (int, float)) else "",
                "project": r["project"],
                "session_id": r["session_id"],
            })
        out["items"] = items
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# 3.12 read_file_safe
# ---------------------------------------------------------------------------

_ALLOWED_SUFFIXES = {".json", ".md", ".txt", ".jsonl", ".sh", ".toml",
                     ".yaml", ".yml", ".local", ".js", ".ts", ".mjs", ".cjs",
                     ".py", ".dart"}
_FORBIDDEN_NAME_PARTS = ("credential", "creds", "token", ".env")
_SIZE_CAP = 512 * 1024


def read_file_safe(rel_path: str) -> dict:
    root = config.claude_root()
    out = {
        "path": "",
        "rel": rel_path,
        "size": 0,
        "truncated": False,
        "content": None,
        "error": None,
    }
    try:
        if not isinstance(rel_path, str) or not rel_path:
            out["error"] = "forbidden"
            return out
        # Reject absolute paths and obvious traversal early.
        if rel_path.startswith("/") or rel_path.startswith("\\"):
            out["error"] = "forbidden"
            return out
        if ".." in rel_path.replace("\\", "/").split("/"):
            out["error"] = "forbidden"
            return out

        try:
            root_resolved = root.resolve()
            target = (root / rel_path).resolve()
        except Exception:
            out["error"] = "forbidden"
            return out

        # Must stay within claude_root.
        try:
            target.relative_to(root_resolved)
        except Exception:
            out["error"] = "forbidden"
            return out

        # Extension check.
        if target.suffix.lower() not in _ALLOWED_SUFFIXES:
            out["error"] = "forbidden"
            return out

        # Forbidden basename parts.
        base_lower = target.name.lower()
        if any(part in base_lower for part in _FORBIDDEN_NAME_PARTS):
            out["error"] = "forbidden"
            return out

        if not target.exists() or not target.is_file():
            out["error"] = "not found"
            return out

        try:
            size = target.stat().st_size
        except Exception:
            size = 0
        out["path"] = str(target)
        out["size"] = int(size)

        truncated = size > _SIZE_CAP
        read_n = _SIZE_CAP if truncated else size + 1
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            content = fh.read(read_n)
        out["truncated"] = bool(truncated)

        # Redact line-by-line so individual secrets get scrubbed.
        out["content"] = _redact_text_block(content)
    except Exception as e:
        out["error"] = str(e)
        out["content"] = None
    return out


# ---------------------------------------------------------------------------
# 3.13 Project memory store  (projects/<key>/memory/)
# ---------------------------------------------------------------------------

_FOLD_MARKERS = (">", ">-", ">+", "|", "|-", "|+", "")


def _md_frontmatter(path) -> dict:
    """Tolerant parse of a memory markdown file's leading --- frontmatter.

    Returns {name, description, type}. No yaml dependency. Handles both real
    layouts of `type:` (flat top-level AND nested under `metadata:`) and folded
    `description: >-` blocks (the value continues on following indented lines).
    Missing pieces fall back to "".
    """
    info = {"name": "", "description": "", "type": ""}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() != "---":
                return info
            in_meta = False
            collecting = None    # field name whose folded block we're gathering
            fold_parts = []
            for _i, line in enumerate(fh):
                if _i > 80:
                    break
                if line.strip() == "---":
                    break
                stripped = line.strip()
                indented = line[:1] in (" ", "\t")

                # A folded block continues on more-indented lines.
                if collecting is not None:
                    if indented:
                        fold_parts.append(stripped)
                        continue
                    info[collecting] = " ".join(fold_parts).strip()
                    collecting = None
                    fold_parts = []
                    # fall through to process this (non-indented) line normally

                if not indented and stripped.startswith("name:"):
                    info["name"] = stripped[len("name:"):].strip()
                    in_meta = False
                elif not indented and stripped.startswith("description:"):
                    val = stripped[len("description:"):].strip()
                    if val in _FOLD_MARKERS:
                        collecting = "description"  # gather following lines
                    else:
                        info["description"] = val
                    in_meta = False
                elif not indented and stripped.startswith("type:"):
                    # Flat frontmatter: `type:` at the top level.
                    info["type"] = stripped[len("type:"):].strip()
                    in_meta = False
                elif not indented and stripped.startswith("metadata:"):
                    in_meta = True
                elif in_meta and stripped.startswith("type:"):
                    info["type"] = stripped[len("type:"):].strip()
            # flush a folded block that ran to the end of the frontmatter
            if collecting is not None and fold_parts:
                info[collecting] = " ".join(fold_parts).strip()
    except Exception:
        pass
    return info


def scan_project_memory(project_key: str) -> dict:
    out = {
        "project_key": project_key,
        "real_path": "",
        "exists": False,
        "index": None,          # MEMORY.md text (redacted) or None
        "items": [],            # individual memory files
        "count": 0,
        "error": None,
    }
    if not _valid_project_key(project_key):
        out["error"] = "invalid project key"
        return out
    root = config.claude_root()
    out["real_path"] = _decode_project_key(project_key)
    mem_dir = root / "projects" / project_key / "memory"
    try:
        if not mem_dir.is_dir():
            return out
        out["exists"] = True
        items = []
        for f in os.scandir(mem_dir):
            try:
                if not f.is_file(follow_symlinks=False):
                    continue
                name = f.name
                if not name.endswith(".md"):
                    continue
                path = Path(f.path)
                if name == "MEMORY.md":
                    txt = path.read_text(encoding="utf-8", errors="replace")
                    out["index"] = _redact_text_block(txt[:20000])
                    continue
                st = f.stat(follow_symlinks=False)
                fm = _md_frontmatter(path)
                items.append({
                    "file": name,
                    "rel": "projects/" + project_key + "/memory/" + name,
                    "name": _redact_inline(fm["name"] or name[:-3]),
                    "description": _redact_inline(fm["description"]),
                    "type": fm["type"] or "",
                    "size": int(st.st_size),
                    "size_h": human_size(st.st_size),
                    "mtime_iso": _iso(st.st_mtime),
                    "_mtime": st.st_mtime,
                })
            except Exception:
                continue
        items.sort(key=lambda it: it.get("_mtime", 0.0), reverse=True)
        for it in items:
            it.pop("_mtime", None)
        out["items"] = items
        out["count"] = len(items)
    except Exception as e:
        out["error"] = str(e)
    return out


# ---------------------------------------------------------------------------
# 3.14 Subagent / workflow invocations  (projects/<key>/<session>/...)
# ---------------------------------------------------------------------------

_VALID_AGENT_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _valid_token(s) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if ".." in s or "/" in s or "\\" in s:
        return False
    return all(c in _VALID_AGENT_CHARS for c in s)


def _workflow_from_json(data: dict, session_id: str) -> dict:
    """Extract the display-relevant fields from a workflows/<wf>.json blob."""
    run_id = data.get("runId") or ""
    progress = data.get("workflowProgress")
    agents = []
    if isinstance(progress, list):
        for p in progress:
            if isinstance(p, dict) and p.get("type") == "workflow_agent":
                agents.append({
                    "label": str(p.get("label") or "agent"),
                    "phase": str(p.get("phaseTitle") or ""),
                    "agent_id": str(p.get("agentId") or ""),
                })
    phases = []
    if isinstance(data.get("phases"), list):
        for ph in data["phases"]:
            if isinstance(ph, dict) and ph.get("title"):
                phases.append(str(ph["title"]))
    start_ms = data.get("startTime")
    started_iso = ""
    if isinstance(start_ms, (int, float)):
        started_iso = _ms_to_iso(start_ms)
    elif isinstance(data.get("timestamp"), str):
        started_iso = data["timestamp"]
    dur = data.get("durationMs")
    return {
        "run_id": run_id,
        "session_id": session_id,
        "name": str(data.get("workflowName") or "workflow"),
        "summary": _redact_inline(str(data.get("summary") or "")),
        "status": str(data.get("status") or ""),
        "agent_count": int(data["agentCount"])
        if isinstance(data.get("agentCount"), (int, float)) else len(agents),
        "duration_ms": int(dur) if isinstance(dur, (int, float)) else None,
        "model": str(data.get("defaultModel") or ""),
        "started_iso": started_iso,
        "phases": phases,
        "agents": agents,
        "_start": start_ms if isinstance(start_ms, (int, float)) else 0,
    }


def scan_project_subagents(project_key: str) -> dict:
    """Workflow runs + direct Task subagents spawned anywhere in this project.

    Reads structured workflows/<wf>.json for workflow runs, and direct
    subagents/agent-*.jsonl (NOT under workflows/) for plain Task invocations.
    Token totals are intentionally NOT computed here (cheap list); they are
    surfaced on drill-down via read_subagent.
    """
    out = {
        "project_key": project_key,
        "real_path": "",
        "workflows": [],
        "tasks": [],
        "scripts": [],
        "counts": {"workflows": 0, "workflow_agents": 0, "tasks": 0, "scripts": 0},
        "error": None,
    }
    if not _valid_project_key(project_key):
        out["error"] = "invalid project key"
        return out
    root = config.claude_root()
    proj_dir = root / "projects" / project_key
    out["real_path"] = _decode_project_key(project_key)
    try:
        if not proj_dir.is_dir():
            return out
        workflows = []
        tasks = []
        scripts = []
        for sess in os.scandir(proj_dir):
            try:
                if not sess.is_dir(follow_symlinks=False):
                    continue
                if sess.name == "memory":
                    continue
                sdir = Path(sess.path)
                session_id = sess.name

                # Workflow runs (structured json).
                wf_dir = sdir / "workflows"
                if wf_dir.is_dir():
                    for wf in os.scandir(wf_dir):
                        try:
                            if not wf.is_file(follow_symlinks=False):
                                continue
                            if not (wf.name.startswith("wf_")
                                    and wf.name.endswith(".json")):
                                continue
                            data = safe_json(Path(wf.path))
                            if isinstance(data, dict):
                                workflows.append(
                                    _workflow_from_json(data, session_id))
                        except Exception:
                            continue

                # Authored workflow scripts (workflows/scripts/*.js). A session
                # may have only these — the workflow was written but its run json
                # / agent transcripts are absent — so surface them too.
                scripts_dir = wf_dir / "scripts"
                if scripts_dir.is_dir():
                    for sf in os.scandir(scripts_dir):
                        try:
                            if not sf.is_file(follow_symlinks=False) \
                                    or not sf.name.endswith(".js"):
                                continue
                            stem = sf.name[:-len(".js")]
                            name, run_id = stem, ""
                            if "-wf_" in stem:
                                pre, post = stem.rsplit("-wf_", 1)
                                name, run_id = pre, "wf_" + post
                            st = sf.stat(follow_symlinks=False)
                            scripts.append({
                                "name": name,
                                "run_id": run_id,
                                "session_id": session_id,
                                "file": sf.name,
                                "rel": "projects/" + project_key + "/" +
                                       session_id + "/workflows/scripts/" + sf.name,
                                "size": int(st.st_size),
                                "size_h": human_size(st.st_size),
                                "mtime_iso": _iso(st.st_mtime),
                                "_mtime": st.st_mtime,
                            })
                        except Exception:
                            continue

                # Direct Task subagents (transcripts not under workflows/).
                sa_dir = sdir / "subagents"
                if sa_dir.is_dir():
                    for af in os.scandir(sa_dir):
                        try:
                            if not af.is_file(follow_symlinks=False):
                                continue
                            if not (af.name.startswith("agent-")
                                    and af.name.endswith(".jsonl")):
                                continue
                            apath = Path(af.path)
                            agent_id = af.name[len("agent-"):-len(".jsonl")]
                            st = af.stat(follow_symlinks=False)
                            tasks.append({
                                "agent_id": agent_id,
                                "session_id": session_id,
                                "task": _redact_inline(_first_user_prompt(apath)),
                                "size": int(st.st_size),
                                "size_h": human_size(st.st_size),
                                "mtime_iso": _iso(st.st_mtime),
                                "_mtime": st.st_mtime,
                            })
                        except Exception:
                            continue
            except Exception:
                continue

        workflows.sort(key=lambda w: w.get("_start", 0), reverse=True)
        for w in workflows:
            w.pop("_start", None)
        tasks.sort(key=lambda t: t.get("_mtime", 0.0), reverse=True)
        for t in tasks:
            t.pop("_mtime", None)
        scripts.sort(key=lambda s: s.get("_mtime", 0.0), reverse=True)
        for s in scripts:
            s.pop("_mtime", None)

        out["workflows"] = workflows
        out["tasks"] = tasks
        out["scripts"] = scripts
        out["counts"] = {
            "workflows": len(workflows),
            "workflow_agents": sum(len(w.get("agents", [])) for w in workflows),
            "tasks": len(tasks),
            "scripts": len(scripts),
        }
    except Exception as e:
        out["error"] = str(e)
    return out


def read_subagent(project_key: str, session_id: str, agent_id: str,
                  run_id: str = "", limit: int = 400) -> dict:
    """Transcript of a single subagent (workflow agent if run_id given, else a
    direct Task agent). Same parsing + token roll-up as read_session."""
    out = {
        "project_key": project_key,
        "session_id": session_id,
        "agent_id": agent_id,
        "run_id": run_id or "",
        "messages": [],
        "count": 0,
        "truncated": False,
        "tokens": _empty_tokens(),
        "error": None,
    }
    if not _valid_project_key(project_key):
        out["error"] = "invalid project key"
        return out
    if not _valid_session_id(session_id) or not _valid_token(agent_id):
        out["error"] = "invalid id"
        return out
    if run_id and not _valid_token(run_id):
        out["error"] = "invalid run id"
        return out
    try:
        limit = int(limit)
    except Exception:
        limit = 400
    if limit <= 0:
        limit = 400

    root = config.claude_root()
    base = root / "projects" / project_key / session_id / "subagents"
    if run_id:
        path = base / "workflows" / run_id / ("agent-" + agent_id + ".jsonl")
    else:
        path = base / ("agent-" + agent_id + ".jsonl")
    try:
        if not path.exists():
            out["error"] = None  # missing -> empty
            return out
        messages, truncated = _parse_transcript(path, limit)
        out["messages"] = messages
        out["truncated"] = truncated
        out["count"] = len(messages)
        out["prompt_count"] = sum(1 for m in messages if m.get("is_prompt"))
        out["tokens"] = session_token_usage(path)
    except Exception as e:
        out["error"] = str(e)
    return out


def _redact_text_block(text: str) -> str:
    """Redact secret-looking tokens across a multi-line text block."""
    if not isinstance(text, str) or not text:
        return text
    lines = text.split("\n")
    redacted_lines = [_redact_inline(line) for line in lines]
    return "\n".join(redacted_lines)
