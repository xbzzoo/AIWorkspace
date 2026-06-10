"""Pytest fixtures for claude-console (builder D).

Builds a SYNTHETIC mini ``~/.claude`` tree inside ``tmp_path`` and points the
scanner at it via ``CLAUDE_CONSOLE_ROOT`` + ``CLAUDE_CONSOLE_HOME_JSON`` for the
duration of a test, restoring the prior environment afterwards.

The synthetic tree deliberately mirrors the real shapes the scanner parses
(grounded against an actual ``~/.claude`` install) and embeds secrets in a few
places so the redaction tests can prove they never leak out of the API.
"""

import json
import os

import pytest

# ---------------------------------------------------------------------------
# Well-known constants the tests assert against. Keeping them here means a
# single source of truth shared between conftest and the test modules.
# ---------------------------------------------------------------------------

# A secret value stashed under settings.env that MUST be redacted.
SETTINGS_SECRET = "sk-ant-xxxxxxxxxxxxxxxxxxxx"
# A secret pasted into a history line that MUST be redacted.
HISTORY_SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
# A secret pasted into a session prompt that MUST be redacted.
SESSION_SECRET = "sk-ant-api03-ZZZZZZZZZZZZZZZZZZZZZZZZ"
# The token VALUE configured for an MCP server's env — must never appear in API.
MCP_TOKEN_VALUE = "tok-supersecret-0123456789abcdef0123"

# Project key <-> decoded real path used by scan_projects / list_sessions tests.
PROJECT_KEY = "-Users-qianyi-DevWorkspace"
PROJECT_REAL_PATH = "/Users/qianyi/DevWorkspace"
SESSION_ID = "11111111-2222-3333-4444-555555555555"

# Subagent / workflow anchors (live under projects/<key>/<session>/).
WF_RUN_ID = "wf_testrun01"
WF_AGENT_ID = "a1111111111111111"
TASK_AGENT_ID = "a2222222222222222"

# Runtime task-output anchors (live under /tmp/claude-<uid>/<key>/<session>/tasks).
RUNTIME_TASK_BASH_ID = "b0testbash01"
RUNTIME_TASK_FLOW_ID = "wq7testflow1"
# A secret embedded in a task output buffer that MUST be redacted by read.
RUNTIME_TASK_SECRET = "sk-ant-api03-RUNTIMETASKOUTPUTSECRET01"


def _write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_tree(root):
    """Materialize the synthetic ~/.claude tree under *root* (a pathlib.Path).

    Returns the path to the sibling ``.claude.json`` (home json) so the fixture
    can export it. ``root`` itself stands in for ``~/.claude``.
    """
    root.mkdir(parents=True, exist_ok=True)

    # -- settings.json: secret env, hooks, enabledPlugins, permissions, flags --
    settings = {
        "env": {
            "API_TOKEN": SETTINGS_SECRET,
            "EDITOR": "vim",  # innocuous, must survive redaction
        },
        "permissions": {
            "allow": ["Bash(ls:*)", "Read(*)"],
            "deny": ["Bash(rm:*)"],
            "ask": ["Bash(git push:*)"],
        },
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/Users/qianyi/.coding-monitor/hook session_start",
                        }
                    ]
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "node /Users/qianyi/.claude/plugins/audit-hook.mjs",
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "/Users/qianyi/.coding-monitor/hook stop",
                        }
                    ]
                }
            ],
        },
        "enabledPlugins": {
            "code-review@claude-plugins-official": True,
            "context7@claude-plugins-official": True,
            "disabled-thing@somewhere": False,
        },
        "skipDangerousModePermissionPrompt": True,
        "skipAutoPermissionPrompt": False,
        "effortLevel": "xhigh",
    }
    _write_json(root / "settings.json", settings)

    # A local override + a backup file (to exercise file listing/backup flag).
    _write_json(
        root / "settings.local.json",
        {"effortLevel": "high", "env": {"LOCAL": "1"}},
    )
    (root / "settings.json.bak").write_text(
        json.dumps(settings, ensure_ascii=False), encoding="utf-8"
    )

    # -- skills/<name>/SKILL.md with folded frontmatter + references/ ----------
    skill_dir = root / "skills" / "daily-work-summary"
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    skill_md = (
        "---\n"
        "name: daily-work-summary\n"
        "description: >-\n"
        "  Summarize today's Claude Code work and append it to a DingTalk doc.\n"
        "  Triggers include daily report and end-of-day report phrases.\n"
        "allowed-tools: Read, Bash\n"
        "---\n\n"
        "# Daily Work Summary\n\n"
        "Body text that should NOT bleed into the description field.\n"
    )
    (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (skill_dir / "references" / "jsonl_schema.md").write_text(
        "# schema notes\n", encoding="utf-8"
    )

    # A second skill with NO frontmatter, to prove tolerance.
    skill2 = root / "skills" / "bare-skill"
    skill2.mkdir(parents=True, exist_ok=True)
    (skill2 / "SKILL.md").write_text(
        "# Bare Skill\n\nNo frontmatter here at all.\n", encoding="utf-8"
    )

    # -- plugins/installed_plugins.json + known_marketplaces.json --------------
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        plugins_dir / "installed_plugins.json",
        {
            "version": 2,
            "plugins": {
                "code-review@claude-plugins-official": [
                    {
                        "scope": "user",
                        "installPath": "/Users/qianyi/.claude/plugins/cache/code-review/1.0.0",
                        "version": "1.0.0",
                        "installedAt": "2026-01-28T03:22:09.978Z",
                        "lastUpdated": "2026-01-28T03:22:09.978Z",
                        "gitCommitSha": "e30768372b4150ca1bc0839d93283e8edc30d60d",
                    }
                ],
                # Installed but NOT in enabledPlugins -> enabled should default false.
                "lonely-plugin@claude-plugins-official": [
                    {
                        "scope": "user",
                        "installPath": "/Users/qianyi/.claude/plugins/cache/lonely/2.0.0",
                        "version": "2.0.0",
                        "installedAt": "2026-02-01T00:00:00.000Z",
                        "lastUpdated": "2026-02-01T00:00:00.000Z",
                    }
                ],
            },
        },
    )
    _write_json(
        plugins_dir / "known_marketplaces.json",
        {
            "claude-plugins-official": {
                "source": {"source": "github", "repo": "anthropics/claude-plugins-official"},
                "installLocation": "/Users/qianyi/.claude/plugins/marketplaces/claude-plugins-official",
                "lastUpdated": "2026-03-16T13:17:08.102Z",
            }
        },
    )

    # -- agents/ (present but EMPTY) -------------------------------------------
    (root / "agents").mkdir(parents=True, exist_ok=True)

    # -- commands/ with one markdown command -----------------------------------
    commands_dir = root / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "deploy.md").write_text(
        "---\nname: deploy\ndescription: Deploy the app\n---\n\nRun the deploy.\n",
        encoding="utf-8",
    )

    # -- history.jsonl (oldest-first; one line carries a pasted secret) --------
    history_lines = [
        {
            "display": "first thing I did",
            "pastedContents": {},
            "timestamp": 1780000000000,
            "project": PROJECT_REAL_PATH,
            "sessionId": SESSION_ID,
        },
        {
            "display": f"please use my token {HISTORY_SECRET} now",
            "pastedContents": {},
            "timestamp": 1780000100000,
            "project": PROJECT_REAL_PATH,
            "sessionId": SESSION_ID,
        },
        {
            "display": "refactor the OVERVIEW renderer",
            "pastedContents": {},
            "timestamp": 1780000200000,
            "project": "/some/other/project",
            "sessionId": "other-session",
        },
    ]
    (root / "history.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in history_lines) + "\n",
        encoding="utf-8",
    )

    # -- projects/<key>/<uuid>.jsonl (mixed record types) + sessions-index -----
    proj = root / "projects" / PROJECT_KEY
    proj.mkdir(parents=True, exist_ok=True)
    records = [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": SESSION_ID},
        # snapshot record -> MUST be skipped by read_session
        {
            "type": "file-history-snapshot",
            "isSnapshotUpdate": True,
            "messageId": "abc",
            "snapshot": {"big": "blob" * 50},
        },
        # user with string content (carries a secret to prove redaction)
        {
            "type": "user",
            "sessionId": SESSION_ID,
            "timestamp": "2026-05-09T06:51:57.692Z",
            "message": {"role": "user", "content": f"deploy with {SESSION_SECRET} please"},
        },
        # isMeta user -> MUST be skipped
        {
            "type": "user",
            "isMeta": True,
            "sessionId": SESSION_ID,
            "timestamp": "2026-05-09T06:52:00.000Z",
            "message": {"role": "user", "content": "<meta noise>"},
        },
        # slash-command machinery -> kept but is_prompt MUST be False
        {
            "type": "user",
            "sessionId": SESSION_ID,
            "timestamp": "2026-05-09T06:52:01.000Z",
            "message": {"role": "user", "content": "<command-name>/effort</command-name>"},
        },
        # tool result riding the user role -> is_prompt MUST be False
        {
            "type": "user",
            "sessionId": SESSION_ID,
            "timestamp": "2026-05-09T06:52:02.000Z",
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "content": "ok done"}]},
        },
        # assistant with list-of-blocks content (text + tool_use) + usage tokens
        {
            "type": "assistant",
            "sessionId": SESSION_ID,
            "timestamp": "2026-05-09T06:52:05.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [
                    {"type": "thinking", "thinking": "internal"},
                    {"type": "text", "text": "Sure, running the deploy now."},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 1000,
                    # nested breakdown that MUST NOT be double-counted
                    "iterations": [{"input_tokens": 100, "output_tokens": 20}],
                },
            },
        },
        # second assistant turn with usage -> proves per-session summation
        {
            "type": "assistant",
            "sessionId": SESSION_ID,
            "timestamp": "2026-05-09T06:52:09.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-7",
                "content": [{"type": "text", "text": "Done."}],
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 30,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    ]
    (proj / f"{SESSION_ID}.jsonl").write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in records) + "\n",
        encoding="utf-8",
    )
    # A second session file so session_count == 2.
    (proj / "99999999-0000-0000-0000-000000000000.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-05-10T00:00:00.000Z",
                "message": {"role": "user", "content": "second session start"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_json(
        proj / "sessions-index.json",
        {
            "version": 1,
            "entries": [
                {
                    "sessionId": SESSION_ID,
                    "fullPath": str(proj / f"{SESSION_ID}.jsonl"),
                    "fileMtime": 1780000300000,
                    "firstPrompt": "deploy with <secret> please",
                    "summary": "Deploy session",
                }
            ],
        },
    )

    # -- a noise subdir with bulk size, to exercise overview's is_noise flag ---
    noise = root / "shell-snapshots"
    noise.mkdir(parents=True, exist_ok=True)
    (noise / "snap.sh").write_text("x" * 4096, encoding="utf-8")

    # -- sibling ~/.claude.json with mcpServers (one stdio+env, one http+url) --
    home_json = root.parent / ".claude.json"
    _write_json(
        home_json,
        {
            "mcpServers": {
                "yuque": {
                    "command": "npx",
                    "args": ["-y", "@yuque/mcp"],
                    "env": {"TOKEN": MCP_TOKEN_VALUE},
                },
                "remote-thing": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer " + MCP_TOKEN_VALUE},
                },
            }
        },
    )

    # mcp-needs-auth-cache.json drives needs_auth flags.
    _write_json(
        root / "mcp-needs-auth-cache.json",
        {"remote-thing": {"timestamp": 1780391863241, "id": "mcpsrv_x"}},
    )

    # -- project memory store: projects/<key>/memory/ --------------------------
    mem = proj / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text(
        "# Memory index\n\n- [User prefs](user_prefs.md) — prefers tabs\n",
        encoding="utf-8",
    )
    # nested-metadata frontmatter (one real-world layout)
    (mem / "user_prefs.md").write_text(
        "---\nname: user-prefs\n"
        "description: Prefers tabs over spaces\n"
        "metadata:\n  node_type: memory\n  type: user\n---\n\n"
        "The user prefers tabs.\n",
        encoding="utf-8",
    )
    # flat frontmatter (the OTHER real-world layout: top-level `type:`)
    (mem / "flat_note.md").write_text(
        "---\nname: flat-note\n"
        "description: A flat-frontmatter memory\n"
        "type: reference\noriginSessionId: x\n---\n\n"
        "Body.\n",
        encoding="utf-8",
    )
    # folded `description: >-` block must be gathered across indented lines
    (mem / "folded_note.md").write_text(
        "---\nname: folded-note\n"
        "description: >-\n  first part of the\n  folded description\n"
        "metadata:\n  type: feedback\n---\n\nBody.\n",
        encoding="utf-8",
    )

    # -- subagents: a workflow run (json + agent transcript) + a direct Task
    #    agent, under projects/<key>/<session>/ (a DIR beside the .jsonl) -------
    sdir = proj / SESSION_ID
    (sdir / "workflows").mkdir(parents=True, exist_ok=True)
    _write_json(sdir / "workflows" / (WF_RUN_ID + ".json"), {
        "runId": WF_RUN_ID,
        "workflowName": "demo-workflow",
        "summary": "demo run",
        "status": "completed",
        "agentCount": 1,
        "durationMs": 12345,
        "startTime": 1780402610420,
        "defaultModel": "claude-opus-4-8",
        "phases": [{"title": "Build"}],
        "workflowProgress": [
            {"type": "workflow_phase", "index": 1, "title": "Build"},
            {"type": "workflow_agent", "index": 1, "label": "builder",
             "phaseTitle": "Build", "agentId": WF_AGENT_ID},
        ],
    })
    # an authored workflow script under workflows/scripts/
    (sdir / "workflows" / "scripts").mkdir(parents=True, exist_ok=True)
    (sdir / "workflows" / "scripts" / ("demo-workflow-" + WF_RUN_ID + ".js")) \
        .write_text("export const meta = { name: 'demo-workflow' }\n", encoding="utf-8")
    wf_agent_dir = sdir / "subagents" / "workflows" / WF_RUN_ID
    wf_agent_dir.mkdir(parents=True, exist_ok=True)
    (wf_agent_dir / ("agent-" + WF_AGENT_ID + ".jsonl")).write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in [
            {"type": "user", "timestamp": "2026-06-02T12:16:50.440Z",
             "message": {"role": "user", "content": "do the build"}},
            {"type": "assistant", "timestamp": "2026-06-02T12:17:00.000Z",
             "message": {"role": "assistant", "model": "claude-opus-4-8",
                         "content": [{"type": "text", "text": "built"}],
                         "usage": {"input_tokens": 10, "output_tokens": 5,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}}},
        ]) + "\n", encoding="utf-8",
    )
    # direct Task agent (transcript NOT under workflows/)
    (sdir / "subagents").mkdir(parents=True, exist_ok=True)
    (sdir / "subagents" / ("agent-" + TASK_AGENT_ID + ".jsonl")).write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in [
            {"type": "user", "timestamp": "2026-06-02T10:00:00.000Z",
             "message": {"role": "user", "content": "Review the codebase"}},
            {"type": "assistant", "timestamp": "2026-06-02T10:00:05.000Z",
             "message": {"role": "assistant", "model": "claude-sonnet-4-6",
                         "content": [{"type": "text", "text": "done"}],
                         "usage": {"input_tokens": 7, "output_tokens": 3,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}}},
        ]) + "\n", encoding="utf-8",
    )

    # -- plans/*.md: plan-mode documents (plain markdown, NO frontmatter) ------
    plans = root / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / "lively-soaring-moon.md").write_text(
        "# Plan — Build the thing\n\n## Context\n\nDo X then Y.\n",
        encoding="utf-8",
    )
    # a plan with no heading -> title must fall back to the file stem
    (plans / "no-heading-plan.md").write_text(
        "Just some body text, no markdown heading here.\n", encoding="utf-8",
    )

    # -- session runtime task outputs: /tmp/claude-<uid>/<key>/<session>/tasks/ -
    #    a sibling of ~/.claude (NOT under it). One bg-Bash output carrying a
    #    secret (proves redaction), one bg-workflow output, and a non-.output
    #    file that MUST be ignored by the lister.
    runtime = root.parent / "claude-runtime"
    tasks = runtime / PROJECT_KEY / SESSION_ID / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / (RUNTIME_TASK_BASH_ID + ".output")).write_text(
        "+ deploy used key " + RUNTIME_TASK_SECRET + " here\n",
        encoding="utf-8",
    )
    (tasks / (RUNTIME_TASK_FLOW_ID + ".output")).write_text(
        json.dumps({"summary": "demo flow", "agentCount": 3, "result": {}}) + "\n",
        encoding="utf-8",
    )
    (tasks / "scratch.tmp").write_text("ignore me\n", encoding="utf-8")

    return home_json, runtime


@pytest.fixture()
def claude_home(tmp_path):
    """Build the synthetic tree and point config at it via env, then restore.

    Yields a dict of the well-known anchors the tests assert against.
    """
    root = tmp_path / ".claude"
    home_json, runtime = _build_tree(root)

    saved = {
        "CLAUDE_CONSOLE_ROOT": os.environ.get("CLAUDE_CONSOLE_ROOT"),
        "CLAUDE_CONSOLE_HOME_JSON": os.environ.get("CLAUDE_CONSOLE_HOME_JSON"),
        "CLAUDE_CONSOLE_RUNTIME_ROOT": os.environ.get("CLAUDE_CONSOLE_RUNTIME_ROOT"),
    }
    os.environ["CLAUDE_CONSOLE_ROOT"] = str(root)
    os.environ["CLAUDE_CONSOLE_HOME_JSON"] = str(home_json)
    os.environ["CLAUDE_CONSOLE_RUNTIME_ROOT"] = str(runtime)
    try:
        yield {
            "root": root,
            "home_json": home_json,
            "runtime_root": runtime,
            "settings_secret": SETTINGS_SECRET,
            "history_secret": HISTORY_SECRET,
            "session_secret": SESSION_SECRET,
            "mcp_token_value": MCP_TOKEN_VALUE,
            "project_key": PROJECT_KEY,
            "project_real_path": PROJECT_REAL_PATH,
            "session_id": SESSION_ID,
            "wf_run_id": WF_RUN_ID,
            "wf_agent_id": WF_AGENT_ID,
            "task_agent_id": TASK_AGENT_ID,
            "task_bash_id": RUNTIME_TASK_BASH_ID,
            "task_flow_id": RUNTIME_TASK_FLOW_ID,
            "task_secret": RUNTIME_TASK_SECRET,
        }
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
