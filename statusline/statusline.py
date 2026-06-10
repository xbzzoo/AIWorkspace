#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code statusLine — flat Catppuccin Mocha, no backgrounds.

Three tiers (identity / session / quota), each a row of items. Every item is
just a colour-tinted Nerd-Font icon + its value — no pills, no panel, no border.
Items float on the terminal's own (translucent) background. Slim line gauges
shift green->peach->red by severity.

All glyphs are written as \\u escapes so the source stays pure ASCII (the Nerd
Font private-use icons do not survive being stored as literal characters).

Data comes from the Claude Code status JSON on stdin (2.1+: context_window +
rate_limits). No network, no credentials, no plugins.
"""

import sys, os, json, time, re, unicodedata, subprocess, urllib.request

# ─────────────────────────── config ───────────────────────────
CTX_BAR   = 12                 # bar length in cells
QUOTA_BAR = 9
ITEM_GAP  = 4                  # spaces between items in a row
INDENT    = 2                  # left margin from the terminal edge
CARD_PADX = 2                  # horizontal padding inside the card
CARD_PADY = 0                  # full blank card rows above/below the content
CARD_HALF = True               # + a half-height card row top/bottom (half-row padding)
ROW_GAP   = 0                  # blank card rows between content rows (use ghostty cell-height for sub-line spacing)

# ─────────────────────────── Catppuccin Mocha ─────────────────
TEXT     = (205, 214, 244)
SUBTEXT0 = (166, 173, 200)
OVERLAY0 = (108, 112, 134)
TRACK    = (69, 71, 90)        # unfilled gauge line (surface1)
BLUE     = (137, 180, 250)
LAVENDER = (180, 190, 254)
SAPPHIRE = (116, 199, 236)
TEAL     = (148, 226, 213)
GREEN    = (166, 227, 161)
YELLOW   = (249, 226, 175)
PEACH    = (250, 179, 135)
RED      = (243, 139, 168)
MAUVE    = (203, 166, 247)
CARD     = (42, 44, 64)        # unified card surface (raised above the base)
DIVIDER  = (88, 92, 122)       # light row border

def fg(c):  return f"\033[38;2;{c[0]};{c[1]};{c[2]}m"
def bg(c):  return f"\033[48;2;{c[0]};{c[1]};{c[2]}m"
HARD = "\033[0m"

ANSI_RE = re.compile(r"\033\[[0-9;]*m")
def vis_width(s):
    w = 0
    for ch in ANSI_RE.sub("", s):
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w

# Nerd Font glyphs (verified present) — by codepoint, never as literals
I_MODEL = chr(0xf2db)   # chip
I_PATH  = chr(0xf07b)   # folder
I_GIT   = chr(0xe0a0)   # branch
I_CTX   = chr(0xf0e4)   # gauge
I_TIME  = chr(0xf017)   # clock
I_COST  = chr(0xf155)   # dollar
I_DIFF  = chr(0xf121)   # code
I_LIMIT = chr(0xf0e7)   # bolt
I_RESET = chr(0xf021)   # refresh
I_EFFORT = chr(0xf085)  # cogs — reasoning effort
I_NET   = chr(0xf0ac)   # globe — network / ipinfo
I_TOOL  = chr(0xf0ad)   # wrench — tools
I_SKILL = chr(0xf12e)   # puzzle — skills
I_MCP   = chr(0xf1e6)   # plug — mcp servers
I_SESS  = chr(0xf1da)   # history — session elapsed
I_AGENT = chr(0xf0c0)   # users — subagents
I_ERR   = chr(0xf071)   # warning — errors
I_TODO  = chr(0xf0ae)   # tasks — todos
I_THINK = chr(0xf0eb)   # bulb — thinking
I_FAST  = chr(0xf135)   # rocket — fast mode
UP      = chr(0x2191)   # ↑
DOWN    = chr(0x2193)   # ↓
DOT     = chr(0x25cf)   # ●
BAR     = chr(0x2501)   # ━
ELLIP   = chr(0x2026)   # …
MINUS   = chr(0x2212)   # −

# ─────────────────────────── helpers ──────────────────────────
def level(pct):
    return GREEN if pct < 75 else (PEACH if pct < 90 else RED)

def human(n):
    n = int(n or 0)
    if n >= 1_000_000: return f"{n / 1e6:.1f}M"
    if n >= 10_000:    return f"{n // 1000}k"
    if n >= 1_000:     return f"{n / 1000:.1f}k"
    return str(n)

def gauge(pct, width):
    """Slim line bar — colour fill + dim track."""
    pct = max(0.0, min(100.0, pct))
    filled = max(0, min(width, round(pct / 100 * width)))
    seg = chr(0x2550)   # ═ centered continuous bar (double rule — shorter than ■)
    return f"{fg(level(pct))}{seg * filled}{fg(TRACK)}{seg * (width - filled)}"

def item(icon, color, value, value_fg=TEXT):
    return f"{fg(color)}{icon} {fg(value_fg)}{value}"

def shorten_path(p, home):
    if home and p.startswith(home):
        p = "~" + p[len(home):]
    parts = p.split("/")
    if len(p) <= 38 or len(parts) <= 4:
        return p
    return f"{parts[0]}/{ELLIP}/" + "/".join(parts[-2:])

def fmt_dur(ms):
    s = int(ms) // 1000
    if s < 60:  return f"{s}s"
    m = s // 60
    if m < 60:  return f"{m}m"
    return f"{m // 60}h{m % 60:02d}m"

def fmt_reset(delta):
    delta = int(delta)
    if delta <= 0:      return "now"
    if delta >= 86400:  return f"{delta // 86400}d"
    if delta >= 3600:
        h, m = delta // 3600, (delta % 3600) // 60
        return f"{h}h{m:02d}m" if m else f"{h}h"
    return f"{max(1, delta // 60)}m"

def git_info(cwd):
    if not cwd or not os.path.isdir(cwd):
        return None
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    def g(*a):
        return subprocess.run(["git", "-C", cwd, *a],
                              capture_output=True, text=True, timeout=0.4, env=env).stdout.strip()
    try:
        branch = g("symbolic-ref", "--short", "HEAD") or (f"@{g('rev-parse','--short','HEAD')}" if g("rev-parse","--short","HEAD") else "")
        if not branch:
            return None
        porcelain = g("status", "--porcelain")
        changes = sum(1 for ln in porcelain.splitlines() if ln.strip())
        ahead = behind = 0
        lr = g("rev-list", "--left-right", "--count", "@{u}...HEAD")   # "behind\tahead"; empty if no upstream
        if "\t" in lr:
            try:
                b_, a_ = lr.split("\t")
                behind, ahead = int(b_), int(a_)
            except Exception:
                pass
        return {"branch": branch, "dirty": bool(porcelain), "changes": changes, "ahead": ahead, "behind": behind}
    except Exception:
        return None

def read_context_fallback(path, limit):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 262_144))
            data = f.read().decode("utf-8", "ignore")
        used = None
        for line in data.split("\n"):
            if '"usage"' not in line:
                continue
            try:
                u = (json.loads(line).get("message") or {}).get("usage")
            except Exception:
                continue
            if not u:
                continue
            tot = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            if tot > 0:
                used = tot
        return (used / limit * 100) if used else None
    except Exception:
        return None

def context_pct(data):
    cw = data.get("context_window") or {}
    if cw.get("used_percentage") is not None:
        return float(cw["used_percentage"])
    size, cu = cw.get("context_window_size"), cw.get("current_usage") or {}
    if size:
        used = (cu.get("input_tokens") or 0) + (cu.get("cache_read_input_tokens") or 0) + (cu.get("cache_creation_input_tokens") or 0)
        if used:
            return used / size * 100
    mid = str((data.get("model") or {}).get("id", "")).lower()
    return read_context_fallback(data.get("transcript_path", ""), 1_000_000 if "1m" in mid else 200_000)

# ─────────────────────────── ipinfo (cached) ──────────────────
IPINFO_CACHE = os.path.expanduser("~/.claude/.statusline-ipinfo.json")
IPINFO_LOCK  = os.path.expanduser("~/.claude/.statusline-ipinfo.lock")
IPINFO_TTL   = 1800            # 30 min — location rarely changes; throttle the call

def _acquire_lock(path, stale=30):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode()); os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(path) > stale:
                os.unlink(path)
                return _acquire_lock(path, stale)
        except OSError:
            pass
        return False
    except Exception:
        return False

def _release_lock(path):
    try:
        os.unlink(path)
    except OSError:
        pass

def get_ipinfo():
    """Cached ipinfo.io lookup. Fetches at most once / IPINFO_TTL; stale on failure."""
    now = time.time()
    cache = None
    try:
        with open(IPINFO_CACHE) as f:
            cache = json.load(f)
        if now - cache.get("ts", 0) < IPINFO_TTL:
            return cache
    except Exception:
        pass
    if not _acquire_lock(IPINFO_LOCK):            # another session is fetching
        return cache
    try:
        try:
            with open(IPINFO_CACHE) as f:
                fresh = json.load(f)
            if now - fresh.get("ts", 0) < IPINFO_TTL:
                return fresh
        except Exception:
            pass
        req = urllib.request.Request("https://ipinfo.io/json", headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=2) as r:
            d = json.load(r)
        out = {"ip": d.get("ip"), "city": d.get("city"), "country": d.get("country"), "ts": now}
        try:
            tmp = IPINFO_CACHE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(out, f)
            os.replace(tmp, IPINFO_CACHE)
        except Exception:
            pass
        return out
    except Exception:
        return cache
    finally:
        _release_lock(IPINFO_LOCK)

# ─────────────────── session activity (tools / skills / mcp) ──
TOOLS_CACHE = os.path.expanduser("~/.claude/.statusline-tools.json")

def get_activity(path):
    """Aggregate tool_use calls from the transcript, parsing only new bytes each render."""
    if not path or not os.path.exists(path):
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    cache = None
    try:
        with open(TOOLS_CACHE) as f:
            cache = json.load(f)
    except Exception:
        pass
    if cache and cache.get("path") == path and 0 <= cache.get("offset", 0) <= size:
        offset = cache["offset"]
        tools, mcp, skills = cache.get("tools", {}), cache.get("mcp", {}), cache.get("skills", {})
        agents, errors, todos = cache.get("agents", 0), cache.get("errors", 0), cache.get("todos", [])
    else:
        offset, tools, mcp, skills, agents, errors, todos = 0, {}, {}, {}, 0, 0, []
    if offset < size:
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
            nl = data.rfind(b"\n")                 # only parse complete lines
            if nl != -1:
                for line in data[:nl].decode("utf-8", "ignore").split("\n"):
                    if '"tool_use"' not in line and '"is_error"' not in line:
                        continue
                    try:
                        content = (json.loads(line).get("message") or {}).get("content")
                    except Exception:
                        continue
                    if not isinstance(content, list):
                        continue
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "tool_result" and b.get("is_error"):
                            errors += 1
                            continue
                        if bt != "tool_use" or not b.get("name"):
                            continue
                        name = b["name"]
                        if name == "Task":
                            agents += 1
                        elif name == "TodoWrite":
                            td = (b.get("input") or {}).get("todos")
                            if isinstance(td, list):
                                todos = [{"status": t.get("status")} for t in td if isinstance(t, dict)]
                        elif name.startswith("mcp__"):
                            parts = name.split("__")
                            srv = parts[1] if len(parts) > 1 else "mcp"
                            mcp[srv] = mcp.get(srv, 0) + 1
                        elif name == "Skill":
                            inp = b.get("input") or {}
                            sk = inp.get("skill") or inp.get("command") or "skill"
                            skills[sk] = skills.get(sk, 0) + 1
                        else:
                            tools[name] = tools.get(name, 0) + 1
                offset += nl + 1
                try:
                    tmp = TOOLS_CACHE + ".tmp"
                    with open(tmp, "w") as f:
                        json.dump({"path": path, "offset": offset, "tools": tools, "mcp": mcp,
                                   "skills": skills, "agents": agents, "errors": errors, "todos": todos}, f)
                    os.replace(tmp, TOOLS_CACHE)
                except Exception:
                    pass
        except Exception:
            pass
    if not (tools or mcp or skills or agents or errors or todos):
        return None
    return {"tools": tools, "mcp": mcp, "skills": skills, "agents": agents, "errors": errors, "todos": todos}

# ─────────────────────────── build ────────────────────────────
def build_rows(data):
    home = os.path.expanduser("~")
    cwd = data.get("cwd") or (data.get("workspace") or {}).get("current_dir") or os.getcwd()
    model = ((data.get("model") or {}).get("display_name") or "Claude").replace("(1M context)", "· 1M").replace("  ", " ").strip()
    cost = data.get("cost") or {}
    rows = []

    # Row 1 — identity
    r1 = [item(I_MODEL, MAUVE, model)]
    eff = (data.get("effort") or {}).get("level")
    if eff:
        r1.append(item(I_EFFORT, LAVENDER, f"{fg(OVERLAY0)}effort {fg(TEXT)}{eff}"))
    if (data.get("thinking") or {}).get("enabled"):
        r1.append(item(I_THINK, LAVENDER, f"{fg(SUBTEXT0)}think"))
    if data.get("fast_mode"):
        r1.append(item(I_FAST, PEACH, f"{fg(SUBTEXT0)}fast"))
    r1.append(item(I_PATH, LAVENDER, shorten_path(cwd, home), value_fg=SUBTEXT0))
    gi = git_info(cwd)
    if gi:
        val = gi["branch"]
        extra = []
        extra.append(f"{fg(PEACH)}{DOT}{gi['changes']}" if gi["changes"] else f"{fg(GREEN)}{DOT}")
        if gi["ahead"]:
            extra.append(f"{fg(GREEN)}{UP}{gi['ahead']}")
        if gi["behind"]:
            extra.append(f"{fg(RED)}{DOWN}{gi['behind']}")
        r1.append(item(I_GIT, LAVENDER, f"{val}  " + " ".join(extra)))
    rows.append(r1)

    # Row 2 — session
    r2 = []
    pct = context_pct(data)
    if pct is not None:
        cw = data.get("context_window") or {}
        tin, tout = cw.get("total_input_tokens"), cw.get("total_output_tokens")
        tok = f"  {fg(OVERLAY0)}{UP}{human(tin)} {DOWN}{human(tout)}" if (tin or tout) else ""
        r2.append(item(I_CTX, LAVENDER, f"{fg(OVERLAY0)}context  {gauge(pct, CTX_BAR)} {fg(level(pct))}{round(pct)}%{tok}"))
    if cost.get("total_duration_ms"):
        r2.append(item(I_SESS, LAVENDER, f"{fg(OVERLAY0)}session {fg(SUBTEXT0)}{fmt_dur(cost['total_duration_ms'])}"))
    if cost.get("total_cost_usd") is not None:
        r2.append(item(I_COST, LAVENDER, f"{fg(OVERLAY0)}cost {fg(TEXT)}${cost['total_cost_usd']:.2f}"))
    add, rem = cost.get("total_lines_added") or 0, cost.get("total_lines_removed") or 0
    if add or rem:
        r2.append(item(I_DIFF, LAVENDER, f"{fg(OVERLAY0)}diff {fg(GREEN)}+{add} {fg(RED)}{MINUS}{rem}"))
    if not r2:
        r2.append(item(I_TIME, OVERLAY0, "idle", value_fg=SUBTEXT0))
    rows.append(r2)

    # Row 3 — account quota (native rate_limits) + network location
    rl = data.get("rate_limits") or {}
    fh, sd = rl.get("five_hour") or {}, rl.get("seven_day") or {}
    now = time.time()
    r3 = []
    for label, p, reset in (("5h", fh.get("used_percentage"), fh.get("resets_at")),
                            ("7d", sd.get("used_percentage"), sd.get("resets_at"))):
        if p is None:
            continue
        val = f"{fg(SUBTEXT0)}{label} {gauge(p, QUOTA_BAR)} {fg(level(p))}{p}%"
        if reset:
            val += f"  {fg(OVERLAY0)}{I_RESET} {fmt_reset(reset - now)}"
        r3.append(item(I_LIMIT, LAVENDER, val))
    ip = get_ipinfo()
    if ip and ip.get("ip"):
        loc = " ".join(x for x in (ip.get("city"), ip.get("country")) if x)
        r3.append(item(I_NET, LAVENDER, f"{fg(TEXT)}{ip['ip']}  {fg(SUBTEXT0)}{loc}"))
    r3.append(item(I_TIME, LAVENDER, f"{fg(SUBTEXT0)}{time.strftime('%H:%M')}"))
    if r3:
        rows.append(r3)

    # Row 4 — session activity: tools / skills / mcp called during the run
    act = get_activity(data.get("transcript_path", ""))
    if act:
        def tidy(name):
            if name.startswith("plugin_"):
                name = name.split("_")[-1]      # plugin_github_github → github
            if ":" in name:
                name = name.split(":")[-1]      # code-review:code-review → code-review
            return name[:18]
        def counts(d, n, clean=False):
            top = sorted(d.items(), key=lambda kv: -kv[1])[:n]
            return "  ".join(f"{fg(TEXT)}{tidy(k) if clean else k} {fg(OVERLAY0)}{v}" for k, v in top)
        r4 = []
        if act["tools"]:
            r4.append(item(I_TOOL, LAVENDER, f"{fg(OVERLAY0)}tools  {counts(act['tools'], 3)}"))
        if act["skills"]:
            r4.append(item(I_SKILL, LAVENDER, f"{fg(OVERLAY0)}skills  {counts(act['skills'], 2, clean=True)}"))
        if act["mcp"]:
            r4.append(item(I_MCP, LAVENDER, f"{fg(OVERLAY0)}mcp  {counts(act['mcp'], 2, clean=True)}"))
        if act.get("agents"):
            r4.append(item(I_AGENT, LAVENDER, f"{fg(OVERLAY0)}agents {fg(TEXT)}{act['agents']}"))
        if act.get("errors"):
            r4.append(item(I_ERR, RED, f"{fg(OVERLAY0)}errors {fg(RED)}{act['errors']}"))
        todos = act.get("todos") or []
        if todos:
            done = sum(1 for t in todos if t.get("status") == "completed")
            r4.append(item(I_TODO, LAVENDER, f"{fg(OVERLAY0)}todos {fg(TEXT)}{done}/{len(todos)}"))
        if r4:
            rows.append(r4)

    return rows

def frame(rows):
    """Lay the rows on one unified, gap-free card: solid fill + generous padding."""
    body = bg(CARD)
    sep = " " * ITEM_GAP
    contents = [sep.join(r) for r in rows]
    inner = max(vis_width(c) for c in contents)
    px = " " * CARD_PADX
    indent = " " * INDENT
    span = inner + 2 * CARD_PADX
    blank = f"{indent}{body}{' ' * span}{HARD}"
    half_t = f"{indent}{fg(CARD)}{chr(0x2584) * span}{HARD}"   # ▄ lower half → half-row top padding
    half_b = f"{indent}{fg(CARD)}{chr(0x2580) * span}{HARD}"   # ▀ upper half → half-row bottom padding
    rows_out = [f"{indent}{body}{px}{c}{' ' * (inner - vis_width(c))}{px}{HARD}" for c in contents]
    divider = f"{indent}{body}{px}{fg(DIVIDER)}{chr(0x2500) * inner}{px}{HARD}"
    spaced = []
    for i, r in enumerate(rows_out):
        if i:
            spaced += [blank] * ROW_GAP + [divider]
        spaced.append(r)
    top = [blank] * CARD_PADY + ([half_t] if CARD_HALF else [])
    bot = ([half_b] if CARD_HALF else []) + [blank] * CARD_PADY
    return "\n".join(top + spaced + bot)

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    sys.stdout.write(frame(build_rows(data)))

if __name__ == "__main__":
    main()
