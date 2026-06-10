"""FastAPI app + WebSocket + static mount + ``main()`` (builder B).

Serves the read-only Claude Console. Every REST endpoint returns the
corresponding scanner dict verbatim, wrapped so that any scanner exception
degrades to an HTTP 200 ``{"error": str(e)}`` (never a 500 — the UI must stay
usable). The watcher runs in its own thread; its callbacks are bridged into the
asyncio loop via ``loop.call_soon_threadsafe`` onto an ``asyncio.Queue``. A
background task drains that queue with a 300 ms debounce + domain coalescing and
broadcasts a single merged ``change`` message to all connected WebSockets.

Binds 127.0.0.1 only. Runnable as ``python -m claude_console.server``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, scanner
from .watcher import ClaudeWatcher


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

_PKG_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _PKG_DIR.parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"

# Debounce window for coalescing a burst of filesystem events.
_DEBOUNCE_S = 0.3


# --------------------------------------------------------------------------
# Connection manager
# --------------------------------------------------------------------------


class ConnectionManager:
    """Tracks active WebSocket clients and broadcasts JSON to all of them."""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self.active.remove(ws)
        except ValueError:
            pass

    async def broadcast(self, message: dict) -> None:
        """Send ``message`` (a JSON-serializable dict) to every client.

        Dead sockets are pruned; a failure to one client never blocks others.
        """
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self.active)


# --------------------------------------------------------------------------
# App + shared state
# --------------------------------------------------------------------------

app = FastAPI(title="Claude Console")


@app.middleware("http")
async def _revalidate_static(request, call_next):
    """Force the SPA assets (index.html / app.js / style.css) to revalidate on
    every load. Without an explicit Cache-Control these get heuristically cached
    by the browser, so a normal refresh would keep serving a stale build. With
    `no-cache` the browser still revalidates via ETag (cheap 304 on localhost)
    but always picks up a changed file."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


manager = ConnectionManager()

# Populated on startup.
_state: dict = {
    "watcher": None,        # ClaudeWatcher
    "queue": None,          # asyncio.Queue
    "loop": None,           # running event loop
    "drain_task": None,     # asyncio.Task
    "watching": False,
}


def _schedule_emit(domains: list, rel: str, kind: str) -> None:
    """Watchdog-thread callback: hand the event to the asyncio loop safely.

    Runs in the watchdog observer thread, so it must not touch the queue
    directly — it schedules a put on the loop via ``call_soon_threadsafe``.
    """
    loop = _state.get("loop")
    queue = _state.get("queue")
    if loop is None or queue is None:
        return
    payload = (list(domains), rel, kind, time.time())
    try:
        loop.call_soon_threadsafe(queue.put_nowait, payload)
    except RuntimeError:
        # Loop is closed/closing — drop the event.
        return


async def _drain_loop() -> None:
    """Drain the change queue with a 300 ms debounce + domain coalescing.

    On the first event, wait up to ``_DEBOUNCE_S`` collecting any further events
    that arrive, merge their domains (preserving the canonical domain order),
    remember the last rel/kind/ts, then broadcast a single merged change msg.
    """
    queue: asyncio.Queue = _state["queue"]
    while True:
        try:
            first = await queue.get()
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(_DEBOUNCE_S)
            continue

        merged_domains: set = set(first[0])
        last_rel = first[1]
        last_kind = first[2]
        last_ts = first[3]

        # Collect everything that lands within the debounce window.
        deadline = time.monotonic() + _DEBOUNCE_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                break
            merged_domains.update(nxt[0])
            last_rel = nxt[1]
            last_kind = nxt[2]
            last_ts = nxt[3]

        # Emit domains in the canonical contract order for stable UI behavior.
        ordered = [d for d in config.DOMAINS if d in merged_domains]
        # Defensive: include any unexpected domain not in the canonical list.
        for d in merged_domains:
            if d not in ordered:
                ordered.append(d)

        message = {
            "type": "change",
            "domains": ordered,
            "path": last_rel,
            "kind": last_kind,
            "ts": last_ts,
        }
        try:
            await manager.broadcast(message)
        except Exception:
            # Broadcasting must never kill the drain loop.
            pass


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


@app.on_event("startup")
async def _on_startup() -> None:
    loop = asyncio.get_event_loop()
    _state["loop"] = loop
    _state["queue"] = asyncio.Queue()

    watcher = ClaudeWatcher(config.claude_root(), on_change=_schedule_emit)
    try:
        watcher.start()
        _state["watching"] = True
    except Exception:
        _state["watching"] = False
    _state["watcher"] = watcher

    _state["drain_task"] = asyncio.ensure_future(_drain_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    task = _state.get("drain_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    watcher = _state.get("watcher")
    if watcher is not None:
        try:
            watcher.stop()
        except Exception:
            pass
    _state["watching"] = False


# --------------------------------------------------------------------------
# Endpoint safety wrapper
# --------------------------------------------------------------------------


def _safe(fn, *args, **kwargs) -> JSONResponse:
    """Run a scanner and wrap any exception as 200 ``{"error": str(e)}``."""
    try:
        result = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — scanners must never 500
        return JSONResponse({"error": str(e)}, status_code=200)
    return JSONResponse(result, status_code=200)


# --------------------------------------------------------------------------
# REST endpoints
# --------------------------------------------------------------------------


@app.get("/api/health")
async def api_health() -> JSONResponse:
    try:
        root = str(config.claude_root())
    except Exception as e:  # noqa: BLE001
        root = ""
        return JSONResponse(
            {"ok": True, "root": root, "clients": manager.count,
             "watching": bool(_state.get("watching")), "error": str(e)},
            status_code=200,
        )
    return JSONResponse(
        {
            "ok": True,
            "root": root,
            "clients": manager.count,
            "watching": bool(_state.get("watching")),
        },
        status_code=200,
    )


@app.get("/api/overview")
async def api_overview() -> JSONResponse:
    return _safe(scanner.scan_overview)


@app.get("/api/settings")
async def api_settings() -> JSONResponse:
    return _safe(scanner.read_settings)


@app.get("/api/skills")
async def api_skills() -> JSONResponse:
    return _safe(scanner.scan_skills)


@app.get("/api/plugins")
async def api_plugins() -> JSONResponse:
    return _safe(scanner.scan_plugins)


@app.get("/api/agents")
async def api_agents() -> JSONResponse:
    return _safe(scanner.scan_agents)


@app.get("/api/commands")
async def api_commands() -> JSONResponse:
    return _safe(scanner.scan_commands)


@app.get("/api/plans")
async def api_plans() -> JSONResponse:
    return _safe(scanner.scan_plans)


@app.get("/api/hooks")
async def api_hooks() -> JSONResponse:
    return _safe(scanner.scan_hooks)


@app.get("/api/mcp")
async def api_mcp() -> JSONResponse:
    return _safe(scanner.read_mcp)


@app.get("/api/projects")
async def api_projects() -> JSONResponse:
    return _safe(scanner.scan_projects)


@app.get("/api/projects/{key}/sessions")
async def api_project_sessions(key: str) -> JSONResponse:
    return _safe(scanner.list_sessions, key)


@app.get("/api/sessions/{key}/{session_id}")
async def api_session(key: str, session_id: str, limit: int = 400) -> JSONResponse:
    return _safe(scanner.read_session, key, session_id, limit)


@app.get("/api/sessions/{key}/{session_id}/tasks")
async def api_session_tasks(key: str, session_id: str) -> JSONResponse:
    return _safe(scanner.list_session_tasks, key, session_id)


@app.get("/api/sessions/{key}/{session_id}/tasks/{task_id}")
async def api_task_output(key: str, session_id: str, task_id: str) -> JSONResponse:
    return _safe(scanner.read_task_output, key, session_id, task_id)


@app.get("/api/projects/{key}/memory")
async def api_project_memory(key: str) -> JSONResponse:
    return _safe(scanner.scan_project_memory, key)


@app.get("/api/projects/{key}/subagents")
async def api_project_subagents(key: str) -> JSONResponse:
    return _safe(scanner.scan_project_subagents, key)


@app.get("/api/subagents/{key}/{session_id}/{agent_id}")
async def api_subagent(key: str, session_id: str, agent_id: str,
                       run: str = "", limit: int = 400) -> JSONResponse:
    return _safe(scanner.read_subagent, key, session_id, agent_id, run, limit)


def _open_in_file_manager(path: str) -> "tuple[bool, str | None]":
    """Open a directory in the OS file manager (macOS Finder / Windows Explorer /
    Linux xdg-open). Args passed as a list (never a shell) so a path with spaces
    or special chars cannot inject. Non-blocking. Returns (ok, error)."""
    if not path or not os.path.isdir(path):
        return False, "path not found on this machine"
    system = platform.system()
    if system == "Darwin":
        cmd = ["open", path]
    elif system == "Windows":
        cmd = ["explorer", path]
    else:
        cmd = ["xdg-open", path]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True, None
    except Exception as e:  # missing opener binary, etc.
        return False, str(e)


def _reveal_in_file_manager(path: str, is_dir: bool) -> "tuple[bool, str | None]":
    """Reveal a path in the OS file manager: open a directory, or SELECT a file
    (macOS `open -R`, Windows `explorer /select,`, Linux opens its parent dir).
    Args passed as a list (never a shell). Non-blocking. Returns (ok, error)."""
    if not path or not os.path.exists(path):
        return False, "path not found on this machine"
    system = platform.system()
    if system == "Darwin":
        cmd = ["open", path] if is_dir else ["open", "-R", path]
    elif system == "Windows":
        cmd = ["explorer", path] if is_dir else ["explorer", "/select,", path]
    else:
        cmd = ["xdg-open", path if is_dir else os.path.dirname(path)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, None
    except Exception as e:
        return False, str(e)


@app.post("/api/reveal-path")
async def api_reveal_path(rel: str = "") -> JSONResponse:
    """Reveal a path under ~/.claude in the local file manager — select a file or
    open a directory. Local-only side-effecting action; the path is validated to
    stay within claude_root. Used by the Skills view to jump to a skill's source.
    """
    info = scanner.resolve_reveal_path(rel)
    if info.get("error"):
        return JSONResponse({"ok": False, "error": info["error"], "rel": rel},
                            status_code=200)
    ok, err = _reveal_in_file_manager(info.get("path") or "",
                                      bool(info.get("is_dir")))
    return JSONResponse({"ok": ok, "path": info.get("path"), "error": err},
                        status_code=200)


@app.post("/api/reveal/{key}")
async def api_reveal(key: str, which: str = "cwd") -> JSONResponse:
    """Open a project's real working directory (or its transcript dir) in the
    local file manager. Local-only side-effecting action; the path is resolved
    from a validated project key, statted, and opened only if it is a directory.
    """
    info = scanner.resolve_project_path(key, which)
    if info.get("error"):
        return JSONResponse({"ok": False, "error": info["error"],
                             "path": info.get("path")}, status_code=200)
    ok, err = _open_in_file_manager(info.get("path") or "")
    return JSONResponse({"ok": ok, "path": info.get("path"),
                         "which": info.get("which"), "error": err},
                        status_code=200)


@app.get("/api/history")
async def api_history(limit: int = 100, offset: int = 0, q: str = "") -> JSONResponse:
    return _safe(scanner.scan_history, limit, offset, q)


@app.get("/api/file")
async def api_file(rel: str = "") -> JSONResponse:
    return _safe(scanner.read_file_safe, rel)


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        root = str(config.claude_root())
    except Exception:
        root = ""
    try:
        await ws.send_json(
            {"type": "hello", "root": root, "domains": list(config.DOMAINS)}
        )
        # Read and ignore client messages (keepalive). The loop ends on
        # disconnect, at which point the client is removed from the manager.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        manager.disconnect(ws)


# --------------------------------------------------------------------------
# Root + static (static mounted LAST so /api and /ws win)
# --------------------------------------------------------------------------


@app.get("/", response_model=None)
async def index() -> FileResponse | JSONResponse:
    if _INDEX_HTML.exists():
        return FileResponse(str(_INDEX_HTML))
    return JSONResponse({"error": "index.html not found"}, status_code=200)


if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _open_browser(url: str, delay: float = 1.0) -> None:
    def _run() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="claude-console",
        description="Read-only real-time control panel for ~/.claude.",
    )
    parser.add_argument(
        "--port", type=int, default=config.PORT,
        help=f"Port to bind on {config.HOST} (default: {config.PORT}).",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not open a web browser on startup.",
    )
    args = parser.parse_args()

    port = args.port
    if not args.no_browser:
        _open_browser(f"http://{config.HOST}:{port}/")

    import uvicorn

    uvicorn.run(app, host=config.HOST, port=port, log_level="info")


if __name__ == "__main__":
    main()
