"""Integration tests for claude_console.scanner against the synthetic fixture.

Every test consumes the ``claude_home`` fixture (conftest.py) which points
config at a freshly-built synthetic ``~/.claude`` tree via env vars.

Note: scanner functions re-read ``config.claude_root()`` on every call, so the
fixture's env override takes effect for free.
"""

import json

from claude_console import scanner, config


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_human_size_formats():
    assert scanner.human_size(0) is not None
    # 1.2 MB-ish: just assert it produces a string with a unit, not an exact value
    s = scanner.human_size(1_200_000)
    assert isinstance(s, str) and any(u in s for u in ("B", "KB", "MB", "GB"))


def test_config_points_at_fixture(claude_home):
    assert config.claude_root() == claude_home["root"]
    assert config.home_claude_json() == claude_home["home_json"]


# ---------------------------------------------------------------------------
# 3.1 overview
# ---------------------------------------------------------------------------

def test_overview_counts(claude_home):
    ov = scanner.scan_overview()
    assert ov["exists"] is True
    assert ov["counts"]["skills"] == 2          # daily-work-summary + bare-skill
    assert ov["counts"]["projects"] == 1
    assert ov["counts"]["sessions"] == 2        # two *.jsonl under the project
    assert ov["counts"]["history_entries"] == 3
    assert ov["counts"]["agents"] == 0
    assert ov["counts"]["commands"] == 1
    assert ov["counts"]["plans"] == 2           # two plans/*.md
    # subdirs include a noise dir flagged is_noise
    noise = [s for s in ov["subdirs"] if s["name"] == "shell-snapshots"]
    assert noise and noise[0]["is_noise"] is True
    # top_files lists root-level history.jsonl
    assert any(f["name"] == "history.jsonl" for f in ov["top_files"])


# ---------------------------------------------------------------------------
# 3.2 settings (redaction of API_TOKEN)
# ---------------------------------------------------------------------------

def test_settings_redacts_api_token(claude_home):
    st = scanner.read_settings()
    blob = json.dumps(st, ensure_ascii=False)
    assert claude_home["settings_secret"] not in blob          # secret never leaks
    assert "<REDACTED>" in blob
    assert st["settings"]["env"]["API_TOKEN"] == "<REDACTED>"
    assert st["settings"]["env"]["EDITOR"] == "vim"            # innocuous survives
    assert st["summary"]["effortLevel"] == "xhigh"
    assert "SessionStart" in st["summary"]["hook_events"]
    assert st["summary"]["enabled_plugins_count"] == 2         # only the True ones


def test_settings_claude_md_absent(claude_home):
    # the fixture has no ~/.claude/CLAUDE.md
    cm = scanner.read_settings()["claude_md"]
    assert cm["exists"] is False
    assert cm["content"] is None


def test_settings_claude_md_present_and_redacted(claude_home):
    secret = "sk-ant-api03-CLAUDEMDSECRETZZZZZZZZZZ"
    (claude_home["root"] / "CLAUDE.md").write_text(
        "# House rules\n\nUse key " + secret + " carefully.\n", encoding="utf-8")
    cm = scanner.read_settings()["claude_md"]
    assert cm["exists"] is True
    assert "# House rules" in cm["content"]
    assert secret not in cm["content"]            # redacted
    assert cm["truncated"] is False


# ---------------------------------------------------------------------------
# 3.3 skills
# ---------------------------------------------------------------------------

def test_skills_parses_frontmatter(claude_home):
    sk = scanner.scan_skills()
    assert sk["count"] == 2
    by_name = {i["name"]: i for i in sk["items"]}
    assert "daily-work-summary" in by_name
    item = by_name["daily-work-summary"]
    assert "DingTalk" in item["description"]
    # body text must not leak into the description
    assert "Body text" not in item["description"]
    assert item["has_references"] is True
    # key = directory basename, used by the UI to build skills/<key>/<file> rels
    assert item["key"] == "daily-work-summary"


# ---------------------------------------------------------------------------
# 3.4 plugins (enabled flag merge + union of keys)
# ---------------------------------------------------------------------------

def test_plugins_merge_enabled_flag(claude_home):
    pl = scanner.scan_plugins()
    by_key = {i["key"]: i for i in pl["items"]}
    assert by_key["code-review@claude-plugins-official"]["enabled"] is True
    # installed-but-not-enabled defaults to False
    assert by_key["lonely-plugin@claude-plugins-official"]["enabled"] is False
    assert by_key["code-review@claude-plugins-official"]["version"] == "1.0.0"
    # marketplaces surfaced
    assert any(m["name"] == "claude-plugins-official" for m in pl["marketplaces"])


# ---------------------------------------------------------------------------
# 3.5 agents / commands
# ---------------------------------------------------------------------------

def test_agents_empty(claude_home):
    ag = scanner.scan_agents()
    assert ag["count"] == 0
    assert ag["items"] == []


def test_commands_lists_markdown(claude_home):
    cmd = scanner.scan_commands()
    assert cmd["count"] == 1
    assert cmd["items"][0]["name"] == "deploy"
    assert "Deploy" in cmd["items"][0]["description"]


# ---------------------------------------------------------------------------
# 3.6 hooks
# ---------------------------------------------------------------------------

def test_hooks_groups_events(claude_home):
    hk = scanner.scan_hooks()
    events = {e["event"] for e in hk["events"]}
    assert {"SessionStart", "PreToolUse", "Stop"} <= events
    assert hk["count"] == len(hk["events"])
    pre = [e for e in hk["events"] if e["event"] == "PreToolUse"][0]
    assert pre["entries"][0]["matcher"] == "Bash"


# ---------------------------------------------------------------------------
# 3.7 mcp (env_keys not values; redaction)
# ---------------------------------------------------------------------------

def test_mcp_returns_env_keys_not_values(claude_home):
    mcp = scanner.read_mcp()
    blob = json.dumps(mcp, ensure_ascii=False)
    # The token VALUE must never appear anywhere in the payload.
    assert claude_home["mcp_token_value"] not in blob
    by_name = {s["name"]: s for s in mcp["servers"]}
    assert by_name["yuque"]["transport"] == "stdio"
    assert by_name["yuque"]["env_keys"] == ["TOKEN"]          # KEY only
    assert by_name["yuque"]["command"] == "npx"
    assert by_name["remote-thing"]["transport"] == "http"
    assert by_name["remote-thing"]["url"] == "https://example.com/mcp"
    assert by_name["remote-thing"]["needs_auth"] is True
    assert by_name["yuque"]["needs_auth"] is False


# ---------------------------------------------------------------------------
# 3.8 projects (decode key -> real_path, session_count)
# ---------------------------------------------------------------------------

def test_projects_decode_key(claude_home):
    pr = scanner.scan_projects()
    assert pr["count"] == 1
    item = pr["items"][0]
    assert item["key"] == claude_home["project_key"]
    assert item["real_path"] == claude_home["project_real_path"]
    assert item["session_count"] == 2


# ---------------------------------------------------------------------------
# 3.9 list_sessions (+ traversal rejection)
# ---------------------------------------------------------------------------

def test_scan_projects_dir_only_session_still_has_activity(claude_home):
    # A project whose only content is an orphaned session dir (subagents but no
    # top-level transcript) must NOT report last_activity 0 — otherwise it sinks
    # to the very bottom of the list and looks "missing".
    root = config.claude_root()
    key = "-Users-qianyi-orphan-proj"
    (root / "projects" / key / "abc12345" / "subagents").mkdir(parents=True, exist_ok=True)
    (root / "projects" / key / "abc12345" / "subagents" / "agent-aaaa.jsonl") \
        .write_text("{}\n", encoding="utf-8")
    proj = {p["key"]: p for p in scanner.scan_projects()["items"]}[key]
    assert proj["session_count"] == 0            # no top-level transcript file
    assert proj["last_activity"] > 0             # but it DOES have real activity
    assert proj["last_activity_iso"] != ""
    assert proj["size"] > 0                      # size counts nested content too


def test_list_sessions_from_index(claude_home):
    ls = scanner.list_sessions(claude_home["project_key"])
    assert ls["error"] is None
    assert ls["real_path"] == claude_home["project_real_path"]
    ids = {s["session_id"] for s in ls["sessions"]}
    assert claude_home["session_id"] in ids


def test_list_sessions_rejects_traversal(claude_home):
    ls = scanner.list_sessions("../../etc")
    assert ls["error"] == "invalid project key"


def test_display_real_path_uses_decoded_folder(claude_home):
    # DISPLAY uses the straightforward decoded folder path (per user request) —
    # uniform across all projects, no per-project cwd recovery.
    root = config.claude_root()
    key = "-Users-qianyi-DevWorkspace-Flutter-fcar-workspace"
    pdir = root / "projects" / key
    pdir.mkdir(parents=True, exist_ok=True)
    real = "/Users/qianyi/DevWorkspace/Flutter/fcar_workspace"
    (pdir / "11110000-0000-0000-0000-000000000000.jsonl").write_text(
        json.dumps({"type": "user", "cwd": real,
                    "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8")
    decoded = "/Users/qianyi/DevWorkspace/Flutter/fcar/workspace"
    assert scanner._decode_project_key(key) == decoded
    assert scanner.list_sessions(key)["real_path"] == decoded
    projs = {p["key"]: p for p in scanner.scan_projects()["items"]}
    assert projs[key]["real_path"] == decoded
    # …but REVEAL still resolves the true on-disk cwd so it opens the right folder
    assert scanner.resolve_project_path(key, "cwd")["path"] == real


def test_real_path_falls_back_to_decode_without_transcript(claude_home):
    root = config.claude_root()
    key = "-Users-qianyi-tmp-empty"
    pdir = root / "projects" / key
    pdir.mkdir(parents=True, exist_ok=True)  # no jsonl -> lossy decode fallback
    assert scanner._project_real_path(pdir, key) == "/Users/qianyi/tmp/empty"


def test_resolve_project_path_for_reveal(claude_home):
    key = claude_home["project_key"]
    root = config.claude_root()
    # transcript dir exists in the fixture -> exists + is_dir true, exact path
    tr = scanner.resolve_project_path(key, "transcript")
    assert tr["error"] is None
    assert tr["which"] == "transcript"
    assert tr["path"] == str(root / "projects" / key)
    assert tr["exists"] is True and tr["is_dir"] is True
    # cwd path resolves (decoded here, since fixture transcripts carry no cwd).
    # exists/is_dir are real os.stat results (env-dependent) and just booleans.
    cw = scanner.resolve_project_path(key, "cwd")
    assert cw["which"] == "cwd"
    assert cw["path"] == claude_home["project_real_path"]
    assert isinstance(cw["exists"], bool) and isinstance(cw["is_dir"], bool)
    # a definitely-absent cwd reports exists False
    fake = "-Users-nobody-does-not-exist-xyz"
    (config.claude_root() / "projects" / fake).mkdir(parents=True, exist_ok=True)
    fk = scanner.resolve_project_path(fake, "cwd")
    assert fk["exists"] is False and fk["is_dir"] is False
    # invalid key is rejected (no path leaks)
    bad = scanner.resolve_project_path("../../etc", "cwd")
    assert bad["error"] == "invalid project key" and bad["path"] is None


# ---------------------------------------------------------------------------
# 3.10 read_session (skip snapshots/meta, flatten content, redact)
# ---------------------------------------------------------------------------

def test_read_session_flattens_and_skips(claude_home):
    rs = scanner.read_session(claude_home["project_key"], claude_home["session_id"])
    assert rs["error"] is None
    roles = [m["role"] for m in rs["messages"]]
    # 3 user records kept (prompt + slash-command + tool_result) + 2 assistant;
    # snapshot & meta dropped
    assert roles.count("user") == 3
    assert roles.count("assistant") == 2
    blob = json.dumps(rs, ensure_ascii=False)
    # pasted secret in the user prompt must be redacted
    assert claude_home["session_secret"] not in blob
    assert "<REDACTED>" in blob
    asst = [m for m in rs["messages"] if m["role"] == "assistant"][0]
    assert asst["model"] == "claude-opus-4-7"
    assert "Sure, running the deploy now." in asst["text"]
    # block kind tags include the tool_use with its name
    assert any(b.startswith("tool_use") for b in asst["blocks"])


def test_read_session_keeps_tail_when_truncated(claude_home):
    # A session longer than the limit must keep the LAST messages (where newly
    # appended turns live), not the first — the old head-truncation hid them.
    root = config.claude_root()
    key = claude_home["project_key"]
    sid = "tailtest1-2222-3333-4444-555555555555"
    p = root / "projects" / key / (sid + ".jsonl")
    lines = [json.dumps({"type": "user", "sessionId": sid,
                         "message": {"role": "user", "content": "msg %d" % i}})
             for i in range(10)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rs = scanner.read_session(key, sid, limit=3)
    assert rs["truncated"] is True
    assert rs["count"] == 3
    assert [m["text"] for m in rs["messages"]] == ["msg 7", "msg 8", "msg 9"]


# ---------------------------------------------------------------------------
# token usage accounting (per session + per project + per message)
# ---------------------------------------------------------------------------

def test_session_token_usage_sums_without_double_counting(claude_home):
    proj = config.claude_root() / "projects" / claude_home["project_key"]
    path = proj / (claude_home["session_id"] + ".jsonl")
    tok = scanner.session_token_usage(path)
    # turn1 (100/20/5/1000) + turn2 (200/30/0/0)
    assert tok["input"] == 300
    assert tok["output"] == 50
    assert tok["cache_creation"] == 5
    assert tok["cache_read"] == 1000
    # total must NOT include the nested `iterations` breakdown (no double count)
    assert tok["total"] == 1355
    assert tok["messages"] == 2


def test_list_sessions_includes_per_session_and_project_tokens(claude_home):
    ls = scanner.list_sessions(claude_home["project_key"])
    by_id = {s["session_id"]: s for s in ls["sessions"]}
    main = by_id[claude_home["session_id"]]
    assert main["tokens"]["total"] == 1355
    assert main["tokens"]["messages"] == 2
    # the second (user-only) session contributes zero tokens
    other = [s for sid, s in by_id.items() if sid != claude_home["session_id"]][0]
    assert other["tokens"]["total"] == 0
    # project roll-up equals the sum across sessions
    assert ls["totals"]["tokens"]["total"] == 1355
    assert ls["totals"]["tokens"]["output"] == 50


def test_read_session_flags_genuine_user_prompts(claude_home):
    rs = scanner.read_session(claude_home["project_key"], claude_home["session_id"])
    # exactly one genuine user question; slash-command + tool_result excluded
    prompts = [m for m in rs["messages"] if m.get("is_prompt")]
    assert len(prompts) == 1
    assert rs["prompt_count"] == 1
    assert "deploy" in prompts[0]["text"]
    # the slash-command and tool-result user records are kept but NOT prompts
    user_msgs = [m for m in rs["messages"] if m["role"] == "user"]
    not_prompts = [m for m in user_msgs if not m.get("is_prompt")]
    assert len(not_prompts) == 2
    # assistant messages are never user prompts
    assert all(not m.get("is_prompt")
               for m in rs["messages"] if m["role"] == "assistant")


def test_read_session_reports_session_and_message_tokens(claude_home):
    rs = scanner.read_session(claude_home["project_key"], claude_home["session_id"])
    assert rs["tokens"]["total"] == 1355
    asst = [m for m in rs["messages"] if m["role"] == "assistant"]
    # per-turn token totals: 1125 then 230
    assert {m["tokens"] for m in asst} == {1125, 230}
    user = [m for m in rs["messages"] if m["role"] == "user"][0]
    assert user["tokens"] is None  # non-assistant turns carry no usage


# ---------------------------------------------------------------------------
# 3.13 project memory store
# ---------------------------------------------------------------------------

def test_scan_project_memory(claude_home):
    md = scanner.scan_project_memory(claude_home["project_key"])
    assert md["error"] is None
    assert md["exists"] is True
    assert "Memory index" in (md["index"] or "")
    assert md["count"] == 3  # user_prefs (nested) + flat_note (flat) + folded
    by_name = {m["name"]: m for m in md["items"]}
    assert by_name["user-prefs"]["type"] == "user"       # nested metadata.type
    assert by_name["user-prefs"]["rel"].endswith("/memory/user_prefs.md")
    assert "tabs" in by_name["user-prefs"]["description"]
    # flat top-level `type:` must parse too (regression guard for both layouts)
    assert by_name["flat-note"]["type"] == "reference"
    # folded `description: >-` block is gathered + its nested type parses
    folded = by_name["folded-note"]
    assert folded["type"] == "feedback"
    assert "folded description" in folded["description"]


def test_scan_project_memory_rejects_traversal(claude_home):
    md = scanner.scan_project_memory("../../etc")
    assert md["error"] == "invalid project key"


def test_memory_file_readable_via_read_file_safe(claude_home):
    md = scanner.scan_project_memory(claude_home["project_key"])
    prefs = [m for m in md["items"] if m["name"] == "user-prefs"][0]
    rf = scanner.read_file_safe(prefs["rel"])
    assert rf["error"] is None
    assert "tabs" in (rf["content"] or "")


# ---------------------------------------------------------------------------
# 3.14 subagents / workflow invocations
# ---------------------------------------------------------------------------

def test_scan_project_subagents(claude_home):
    sub = scanner.scan_project_subagents(claude_home["project_key"])
    assert sub["error"] is None
    assert sub["counts"] == {"workflows": 1, "workflow_agents": 1,
                             "tasks": 1, "scripts": 1}
    wf = sub["workflows"][0]
    assert wf["name"] == "demo-workflow"
    assert wf["status"] == "completed"
    assert wf["run_id"] == claude_home["wf_run_id"]
    assert wf["agents"][0]["label"] == "builder"
    assert wf["agents"][0]["agent_id"] == claude_home["wf_agent_id"]
    assert wf["phases"] == ["Build"]
    task = sub["tasks"][0]
    assert task["agent_id"] == claude_home["task_agent_id"]
    assert "Review the codebase" in task["task"]
    # authored workflow scripts (workflows/scripts/*.js) are surfaced too
    assert sub["counts"]["scripts"] == 1
    scr = sub["scripts"][0]
    assert scr["name"] == "demo-workflow"
    assert scr["run_id"] == claude_home["wf_run_id"]
    assert scr["rel"].endswith("/workflows/scripts/demo-workflow-"
                               + claude_home["wf_run_id"] + ".js")
    # …and the script source is viewable through read_file_safe (.js allowed)
    assert "demo-workflow" in (scanner.read_file_safe(scr["rel"])["content"] or "")


def test_read_subagent_workflow_agent(claude_home):
    rs = scanner.read_subagent(
        claude_home["project_key"], claude_home["session_id"],
        claude_home["wf_agent_id"], run_id=claude_home["wf_run_id"])
    assert rs["error"] is None
    assert rs["tokens"]["total"] == 15  # 10 in + 5 out
    assert [m["role"] for m in rs["messages"]] == ["user", "assistant"]


def test_read_subagent_direct_task(claude_home):
    rs = scanner.read_subagent(
        claude_home["project_key"], claude_home["session_id"],
        claude_home["task_agent_id"])  # no run_id -> direct Task path
    assert rs["error"] is None
    assert rs["tokens"]["total"] == 10  # 7 in + 3 out


def test_read_subagent_rejects_traversal(claude_home):
    rs = scanner.read_subagent(claude_home["project_key"],
                               claude_home["session_id"], "../../etc", run_id="x")
    assert rs["error"] == "invalid id"


def test_list_sessions_reports_subagent_count(claude_home):
    ls = scanner.list_sessions(claude_home["project_key"])
    main = [s for s in ls["sessions"]
            if s["session_id"] == claude_home["session_id"]][0]
    # one workflow agent + one direct Task agent under this session's dir
    assert main["subagent_count"] == 2


# ---------------------------------------------------------------------------
# 3.16 session runtime task outputs (/tmp/claude-<uid>/<key>/<session>/tasks)
# ---------------------------------------------------------------------------

def test_list_sessions_reports_task_count(claude_home):
    ls = scanner.list_sessions(claude_home["project_key"])
    main = [s for s in ls["sessions"]
            if s["session_id"] == claude_home["session_id"]][0]
    # two *.output buffers; the scratch.tmp is ignored
    assert main["task_count"] == 2


def test_list_session_tasks_lists_outputs_only(claude_home):
    lt = scanner.list_session_tasks(claude_home["project_key"],
                                    claude_home["session_id"])
    assert lt["error"] is None
    assert lt["count"] == 2
    ids = {t["task_id"] for t in lt["tasks"]}
    assert ids == {claude_home["task_bash_id"], claude_home["task_flow_id"]}
    # kind hint from the id prefix
    kinds = {t["task_id"]: t["kind"] for t in lt["tasks"]}
    assert kinds[claude_home["task_bash_id"]] == "bash"
    assert kinds[claude_home["task_flow_id"]] == "agent"
    # newest-first ordering by mtime
    mtimes = [t["mtime"] for t in lt["tasks"]]
    assert mtimes == sorted(mtimes, reverse=True)


def test_list_session_tasks_absent_runtime_is_empty(claude_home):
    # a session with no runtime tasks dir -> empty, no error
    lt = scanner.list_session_tasks(claude_home["project_key"],
                                    "99999999-0000-0000-0000-000000000000")
    assert lt["error"] is None
    assert lt["count"] == 0
    assert lt["tasks"] == []


def test_list_session_tasks_rejects_traversal(claude_home):
    lt = scanner.list_session_tasks("../../etc", claude_home["session_id"])
    assert lt["error"] == "invalid key"


def test_read_task_output_redacts_secret(claude_home):
    rt = scanner.read_task_output(claude_home["project_key"],
                                  claude_home["session_id"],
                                  claude_home["task_bash_id"])
    assert rt["error"] is None
    assert rt["content"] is not None
    # the embedded credential must never leak through the API
    assert claude_home["task_secret"] not in rt["content"]
    assert rt["kind"] == "bash"
    assert rt["truncated"] is False


def test_read_task_output_truncates_large(claude_home):
    rt = scanner.read_task_output(claude_home["project_key"],
                                  claude_home["session_id"],
                                  claude_home["task_flow_id"],
                                  limit_bytes=8)
    assert rt["error"] is None
    assert rt["truncated"] is True
    assert len(rt["content"]) <= 8


def test_read_task_output_rejects_bad_task_id(claude_home):
    rt = scanner.read_task_output(claude_home["project_key"],
                                  claude_home["session_id"], "../escape")
    assert rt["error"] == "invalid key"
    assert rt["content"] is None


def test_read_task_output_missing_is_error(claude_home):
    rt = scanner.read_task_output(claude_home["project_key"],
                                  claude_home["session_id"], "bdoesnotexist")
    assert rt["error"] == "not found"
    assert rt["content"] is None


def test_redact_inline_preserves_newlines(claude_home):
    # A redacted token must not swallow the newline that follows it — multi-line
    # content (diffs, JSON, code) has to keep its line structure after redaction.
    secret = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA"
    text = ("diff --git a/x b/very/long/path/looks/secretish.dart\n"
            "@@ -0,0 +1,2 @@\n"
            "key " + secret + " end\n"
            "last line")
    out = scanner._redact_inline(text)
    assert secret not in out                       # secret scrubbed
    assert out.count("\n") == text.count("\n")     # line count preserved
    assert "\n@@ -0,0 +1,2 @@\n" in out            # hunk header still its own line


# ---------------------------------------------------------------------------
# 3.5b plans (plan-mode documents under plans/*.md)
# ---------------------------------------------------------------------------

def test_scan_plans_lists_and_titles(claude_home):
    sp = scanner.scan_plans()
    assert sp.get("error") is None
    assert sp["count"] == 2
    by = {p["name"]: p for p in sp["items"]}
    assert set(by) == {"lively-soaring-moon", "no-heading-plan"}
    # title = first heading with a leading 'Plan —' prefix trimmed
    assert by["lively-soaring-moon"]["title"] == "Build the thing"
    # no heading → fall back to the file stem
    assert by["no-heading-plan"]["title"] == "no-heading-plan"
    # rel points at plans/*.md so the existing /api/file viewer can read it
    assert by["lively-soaring-moon"]["rel"] == "plans/lively-soaring-moon.md"
    assert by["lively-soaring-moon"]["size"] > 0


def test_scan_plans_content_via_read_file_safe(claude_home):
    # content is served by the existing read_file_safe path (no new endpoint)
    rf = scanner.read_file_safe("plans/lively-soaring-moon.md")
    assert rf["error"] is None
    assert "# Plan — Build the thing" in rf["content"]


def test_scan_plans_absent_dir_is_empty(claude_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONSOLE_ROOT", str(claude_home["root"] / "nope"))
    sp = scanner.scan_plans()
    assert sp["count"] == 0 and sp["items"] == []


# ---------------------------------------------------------------------------
# 3.11 history (newest-first, redaction, q filter)
# ---------------------------------------------------------------------------

def test_history_newest_first_and_redacts(claude_home):
    h = scanner.scan_history(limit=100, offset=0, q="")
    assert h["total"] == 3
    # newest first: last-written line ("refactor the OVERVIEW renderer") is first
    assert h["items"][0]["display"].startswith("refactor the OVERVIEW")
    blob = json.dumps(h, ensure_ascii=False)
    assert claude_home["history_secret"] not in blob          # pasted token scrubbed
    assert "<REDACTED>" in blob


def test_history_q_filter(claude_home):
    h = scanner.scan_history(limit=100, offset=0, q="overview")
    assert h["total"] == 1
    assert "OVERVIEW" in h["items"][0]["display"]
    # filter also matches on project field
    h2 = scanner.scan_history(limit=100, offset=0, q="DevWorkspace")
    assert h2["total"] == 2


# ---------------------------------------------------------------------------
# 3.12 read_file_safe (path validation + credential-name rejection + redaction)
# ---------------------------------------------------------------------------

def test_read_file_safe_accepts_settings(claude_home):
    rf = scanner.read_file_safe("settings.json")
    assert rf["error"] is None
    assert rf["content"] is not None
    # redaction still applies to the raw file view
    assert claude_home["settings_secret"] not in rf["content"]


def test_read_file_safe_rejects_traversal(claude_home):
    rf = scanner.read_file_safe("../../etc/passwd")
    assert rf["error"] == "forbidden"
    assert rf["content"] is None


def test_read_file_safe_rejects_credential_named(claude_home):
    # Create a credential-named file inside root; it must still be refused.
    (claude_home["root"] / "my.credentials.json").write_text("{}", encoding="utf-8")
    rf = scanner.read_file_safe("my.credentials.json")
    assert rf["error"] == "forbidden"


def test_resolve_reveal_path_valid_and_safe(claude_home):
    # a real skill dir + file resolve within root
    rd = scanner.resolve_reveal_path("skills/daily-work-summary")
    assert rd["error"] is None and rd["is_dir"] is True
    assert rd["path"].endswith("daily-work-summary")
    rf = scanner.resolve_reveal_path("skills/daily-work-summary/SKILL.md")
    assert rf["error"] is None and rf["is_dir"] is False
    # traversal / absolute / outside-root are refused; missing → not found
    assert scanner.resolve_reveal_path("../../etc")["error"] == "forbidden"
    assert scanner.resolve_reveal_path("/etc/passwd")["error"] == "forbidden"
    assert scanner.resolve_reveal_path("skills/does-not-exist")["error"] == "not found"


def test_read_file_safe_rejects_bad_extension(claude_home):
    (claude_home["root"] / "secret.pem").write_text("x", encoding="utf-8")
    rf = scanner.read_file_safe("secret.pem")
    assert rf["error"] == "forbidden"


def test_read_file_safe_accepts_python(claude_home):
    # .py joined the allowlist (skill scripts) — served like .js, redacted+capped.
    sk = claude_home["root"] / "skills" / "daily-work-summary"
    (sk / "helper.py").write_text("print('hi')\n", encoding="utf-8")
    rf = scanner.read_file_safe("skills/daily-work-summary/helper.py")
    assert rf["error"] is None
    assert "print('hi')" in rf["content"]


# ---------------------------------------------------------------------------
# robustness: scanners never raise even with a missing root
# ---------------------------------------------------------------------------

def test_scanners_never_raise_on_missing_root(claude_home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONSOLE_ROOT", str(claude_home["root"] / "does-not-exist"))
    # Should degrade gracefully, not raise.
    ov = scanner.scan_overview()
    assert ov["exists"] is False or ov["counts"]["skills"] == 0
    assert isinstance(scanner.scan_skills()["items"], list)
    assert isinstance(scanner.scan_plugins()["items"], list)
    assert isinstance(scanner.scan_history()["items"], list)
