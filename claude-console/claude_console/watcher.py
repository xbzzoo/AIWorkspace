"""Filesystem watcher for Claude Console (builder B).

Wraps a single :class:`watchdog.observers.Observer` and schedules a *curated*
set of paths so the 168 MB ``projects/`` tree and the high-churn caches under
``~/.claude`` do not produce an event storm. Every surfaced event is mapped to
the affected UI domains via :func:`config.path_to_domains` and forwarded to the
caller's ``on_change`` callback.

Debounce / coalescing is intentionally NOT done here — that lives on the server
side (see :mod:`claude_console.server`). This module only filters noise and
forwards.

The watcher must never raise out into the caller: scheduling a missing dir is
silently skipped, and the event handler swallows its own exceptions so a single
malformed event can never kill the observer thread.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import config


# Callback signature: (domains, rel_path, kind) -> None
OnChange = Callable[[list, str, str], None]


# Lowercased copy of config.IGNORE_SUFFIXES so suffix matching is
# case-insensitive. config.IGNORE_SUFFIXES contains mixed-case entries such as
# ".DS_Store"; basenames are lowercased before comparison, so the suffixes must
# be lowered too or e.g. ".ds_store" would never endswith(".DS_Store").
_IGNORE_SUFFIXES_LOWER = tuple(s.lower() for s in config.IGNORE_SUFFIXES)


def _event_kind(event_type: str) -> str:
    """Normalize a watchdog event_type into the contract's kind vocabulary."""
    if event_type in ("created", "modified", "deleted", "moved"):
        return event_type
    # Unknown/empty event types degrade to "modified" (a safe re-fetch trigger).
    return "modified"


class _Handler(FileSystemEventHandler):
    """Single handler shared by every scheduled watch.

    Computes the path relative to ``root``, drops noise (heavy/cache segments,
    ignored suffixes, and non-transcript files under ``projects/``), maps the
    remaining path to domains, and forwards to ``on_change``.
    """

    def __init__(self, root: Path, on_change: OnChange):
        super().__init__()
        self._root = root
        self._on_change = on_change

    # ----- path filtering -------------------------------------------------

    def _rel(self, abs_path: str) -> str | None:
        """Return the path relative to root (posix-style), or None if outside."""
        if not abs_path:
            return None
        try:
            rel = os.path.relpath(abs_path, str(self._root))
        except (ValueError, OSError):
            return None
        rel = rel.replace("\\", "/")
        # A path outside the root resolves to something starting with "..".
        if rel == "." or rel.startswith("../") or rel == "..":
            return None
        return rel

    def _should_ignore(self, rel: str) -> bool:
        """True if this relative path is pure noise and must be dropped."""
        parts = [p for p in rel.split("/") if p and p != "."]
        if not parts:
            return True

        lower_parts = [p.lower() for p in parts]
        base_lower = parts[-1].lower()

        # Ignored suffixes (.lock/.bak/.DS_Store) — drop everywhere. Compare
        # case-insensitively: config.IGNORE_SUFFIXES holds the mixed-case
        # ".DS_Store", but base_lower is already lowercased, so we must lower
        # the suffix tuple too or ".ds_store" would never match ".DS_Store".
        if base_lower.endswith(_IGNORE_SUFFIXES_LOWER):
            return True

        # Any heavy/noise segment anywhere in the path -> drop. `projects` is
        # explicitly watched and is NOT a member of HEAVY_OR_NOISE_SEGMENTS, so
        # transcripts survive this check.
        for seg in lower_parts:
            if seg in config.HEAVY_OR_NOISE_SEGMENTS:
                return True

        # Under projects/ keep only transcripts and the sessions index. The
        # observer watches projects/ recursively, so without this we'd surface
        # noise from arbitrary per-project files.
        if lower_parts[0] == "projects" and len(parts) > 1:
            if not (base_lower.endswith(".jsonl")
                    or base_lower == "sessions-index.json"):
                return True

        return False

    def _emit(self, abs_path: str, event_type: str) -> None:
        try:
            rel = self._rel(abs_path)
            if rel is None:
                return
            if self._should_ignore(rel):
                return
            domains = config.path_to_domains(rel)
            if not domains:
                return
            self._on_change(domains, rel, _event_kind(event_type))
        except Exception:
            # Never let a single bad event escape into the observer thread.
            return

    # ----- watchdog dispatch ---------------------------------------------

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Directory events are uninteresting for domain mapping (the file event
        # that accompanies them carries the signal); skip to cut churn. We still
        # honor a directory deletion because the file-level "deleted" events may
        # not fire on some platforms when a whole dir is removed.
        if getattr(event, "is_directory", False) and event.event_type != "deleted":
            return

        self._emit(getattr(event, "src_path", "") or "", event.event_type)

        # A "moved" event also has a destination; surface it as a creation at
        # the new path so a renamed SKILL.md / settings file is picked up.
        if event.event_type == "moved":
            dest = getattr(event, "dest_path", "") or ""
            if dest:
                self._emit(dest, "created")


class ClaudeWatcher:
    """Watches the curated subset of ``~/.claude`` and forwards change events.

    Parameters
    ----------
    root:
        The ``~/.claude`` directory (``config.claude_root()``).
    on_change:
        ``Callable[[list[str] domains, str rel_path, str kind], None]`` invoked
        for every surfaced change. ``kind`` is one of
        ``{"created","modified","deleted","moved"}``.
    """

    def __init__(self, root: Path, on_change: OnChange):
        self.root = Path(root)
        self.on_change = on_change
        self._observer = Observer()
        self._handler = _Handler(self.root, on_change)
        self._started = False

    # ----- scheduling -----------------------------------------------------

    def _schedule(self, path: Path, recursive: bool) -> None:
        """Schedule a watch on ``path`` if it exists and is a directory."""
        try:
            if path.exists() and path.is_dir():
                self._observer.schedule(self._handler, str(path), recursive=recursive)
        except Exception:
            # A path that vanishes between exists() and schedule(), or a
            # permission error, must not abort start().
            return

    def start(self) -> None:
        """Schedule the curated watch set and start the observer (non-blocking)."""
        if self._started:
            return

        # root, non-recursive: settings*.json, history.jsonl, CLAUDE.md, *.json
        self._schedule(self.root, recursive=False)

        # skills/agents/commands: recursive, only if they exist
        self._schedule(self.root / "skills", recursive=True)
        self._schedule(self.root / "agents", recursive=True)
        self._schedule(self.root / "commands", recursive=True)

        # plans: non-recursive (flat plans/*.md plan-mode documents)
        self._schedule(self.root / "plans", recursive=False)

        # plugins: non-recursive (installed_plugins.json, known_marketplaces.json)
        self._schedule(self.root / "plugins", recursive=False)

        # projects: recursive, but the handler drops everything except
        # *.jsonl / sessions-index.json and noise segments.
        self._schedule(self.root / "projects", recursive=True)

        try:
            self._observer.start()
            self._started = True
        except Exception:
            # If the observer cannot start (e.g. inotify limits), degrade to a
            # no-op rather than crashing the server. REST stays fully usable.
            self._started = False

    def stop(self) -> None:
        """Stop the observer and join its thread; safe to call repeatedly."""
        if not self._started:
            return
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception:
            pass
        finally:
            self._started = False
