# ⌁ Claude Console

一个本地、**只读**、实时刷新的 `~/.claude` 控制台。把 Claude Code 的所有配置域 —— settings、skills、plugins、agents、commands、hooks、MCP servers、projects、sessions、history、plans —— 汇总到一个零依赖的「开发者控制台」式 Web UI，文件一变页面就实时更新。侧边栏可在 **夜间模式（默认）** 与 **正常模式** 之间切换，选择记在 localStorage，首屏渲染前即生效。

> 英文版详细文档见 [`README.en.md`](./README.en.md)；权威构建契约（接口与 JSON 结构的唯一真相源）见 [`CONTRACT.md`](./CONTRACT.md)。

## 功能简介

- **只读**：任何接口都不会写入 / 删除 / 修改 `~/.claude` 下的任何内容，也从不调用 `claude` CLI，只 *读* 文件。
- **仅本地**：服务只绑定 `127.0.0.1`，网络上无法访问。
- **防泄密**：所有出站数据先过一层脱敏 ——
  - secret 类 key（`token` / `secret` / `password` / `api_key` / `authorization` / `cookie` / `client_secret` / `refresh_token` …）的值一律替换为 `<REDACTED>`；
  - *长得像* 凭证的字符串（`sk-…`、`sk-ant-…`、`ghp_…`、GitHub PAT、Slack `xox…`、JWT、AWS `AKIA…`、长 hex/base64）即便挂在无害 key 下也会被脱敏；
  - MCP server 配置**只暴露 env/header 的 KEY 名，绝不暴露值**；
  - history 与 session transcript（含任意粘贴文本）展示前先脱敏。
- **路径安全**：「查看原文件」接口会校验解析后的路径仍在 `~/.claude` 内，限定文本扩展名白名单，拒绝文件名含 `credential` / `creds` / `token` / `.env` 的文件，并限制返回体大小。
- **健壮**：扫描器从不抛异常 —— 文件缺失或损坏会降级成 `{"error": …}` 字段，应用照常可用。

## 环境要求

- **Python 3.10+**
- 运行库：`fastapi`、`uvicorn[standard]`、`watchdog`、`websockets`
- 测试库：`pytest`

> 在 Claude Code 环境里这几个库通常已经装好，多数情况无需额外安装。

## 安装

```bash
# 进入项目目录
cd claude-console

# 如缺依赖再执行（前端零依赖、无构建步骤）
pip install -r requirements.txt
```

## 启动

项目根目录下：

```bash
./run.sh
```

等价于：

```bash
python3 -m claude_console.server
```

然后打开：

> **http://127.0.0.1:8765**

浏览器会自动弹出。常用参数 / 环境变量：

| 参数 / 变量 | 作用 |
| --- | --- |
| `--no-browser` | 不自动打开浏览器 |
| `--port <N>` / `CLAUDE_CONSOLE_PORT` | 改用其他端口（默认 8765 被占用时） |
| `CLAUDE_CONSOLE_ROOT` | 指向非默认的 `~/.claude` 目录（如沙箱） |
| `CLAUDE_CONSOLE_HOME_JSON` | 指向对应的 `~/.claude.json` |
| `CLAUDE_CONSOLE_RUNTIME_ROOT` | 后台任务输出缓冲目录（默认 `/tmp/claude-<uid>`，按 `os.getuid()` 自动解析） |

## 功能域（Domains）

左侧导航对应这些域，每个由 `GET /api/<domain>` 提供数据：

| Domain | 展示内容 |
| --- | --- |
| **Settings** | effort、权限、hook 事件、flags + 脱敏高亮的 `settings.json`；存在时把 `~/.claude/CLAUDE.md` 渲染为 markdown |
| **Skills** | 每个 `skills/*/SKILL.md` —— 名称、描述、refs 徽标；点开卡片读 `SKILL.md` 并浏览其文件 |
| **Plugins** | 已安装插件 + enabled 映射 + marketplaces |
| **Agents** | `agents/*.md` 条目（含空态） |
| **Commands** | `commands/**/*.md` 条目 |
| **Hooks** | `settings.json` 里各事件的 hook 命令（脱敏） |
| **MCP** | `~/.claude.json` 里的 MCP server —— transport、command/url、env **keys** |
| **Projects** | 每个 project 目录解码回真实 cwd；可下钻到 sessions/transcripts |
| **History** | `history.jsonl`，最新在前，可搜索、分页（脱敏）；session 仍存在的行可点击打开 transcript |
| **Plans** | plan 模式文档 `plans/*.md`，最新在前；点行在抽屉里渲染 |

Projects → Sessions 下，带后台任务输出缓冲的 session 会显示绿色 `⚙ N` 角标，数据来自 Claude Code 的运行时 scratch 目录 `/tmp/claude-<uid>/…/tasks/*.output`（第二个 `~/.claude` 之外的只读来源）；点开可看其输出（脱敏，上限 256 KB）。

## 实时更新

[watchdog](https://pypi.org/project/watchdog/) observer 监听 `~/.claude` 下一组精选路径（根、`skills/`、`agents/`、`commands/`、`plugins/`、`projects/`，并忽略高频缓存与海量 transcript 噪声）。相关文件变化时，计算受影响的 UI 域并经 WebSocket（`/ws`）推给浏览器（300ms 防抖 + 域合并）。前端只重拉受影响的域、闪烁对应导航项，并在可折叠的 **Live activity** 条里追加一行。连接点：绿=已连，红=重连中（指数退避自动重连）。

## 运行测试

```bash
python3 -m pytest -q
```

测试会在临时目录里搭一棵合成的迷你 `~/.claude` 树（见 `tests/conftest.py`），断言：secret 被脱敏、MCP 只暴露 env key、`read_file_safe` 拒绝 `../../etc/passwd` 穿越与凭证命名文件、history 最新在前且 `q` 过滤生效、`read_session` 跳过快照/meta 记录、project 目录 key 能解码回真实路径等。仅依赖标准库 + `pytest`。
