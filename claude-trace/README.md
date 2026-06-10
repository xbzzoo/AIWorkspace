# claude-trace：Claude Code 一次 Skill 调用的 HTTP 抓包拆解

用 [claude-trace](https://github.com/badlogic/lemmy/tree/main/apps/claude-trace) 在 Node 进程级别 hook 住 Claude Code CLI 的 HTTPS 栈，把每一对 request/response 落成 JSON Lines，从**流量层**还原"一次 skill 调用"（实验指令：`/fliggy_code_view git diff`）的完整时序。

> 注：claude-trace 当前只兼容 Claude Code 版本 < 2.1.112。

## 核心结论

- 一次 `/fliggy_code_view git diff` 在 HTTP 层留下 **34 条** request/response，其中只有 **11 条** 是 `/v1/messages`，其余 23 条是配置 / 服务发现 / 遥测等会话外围流量。
- 11 条里第 1 条是启动握手的连通性 ping，剩下 **10 条** 是同一次 skill 调用的 Opus 主链（`claude-opus-4-7`、`stream: true`、`max_tokens: 64000`）。
- 这 10 轮共享同一份 ~26K 字符 system prompt 和同一组 10 个 tool schema，每轮原样重发；变化的只有 `messages` 数组长度，按 **1 → 3 → 5 → … → 19** 等差爬升，对应 **9 次中间工具调用 + 1 次以 `stop_reason: "end_turn"` 收尾的最终回答**。这就是 agentic tool-use 循环在 HTTP 层的指纹。
- 请求体被刻意分成"静"（system prompt + tools + 首轮全局上下文，逐字稳定，喂给 prompt cache 的长前缀）与"动"（每轮新追加在 `messages` 尾巴上的一对 `tool_use` / `tool_result`）两层。

## 资产清单

| 文件 | 说明 |
| --- | --- |
| [`claude-trace抓包.md`](./claude-trace抓包.md) | 主文：逐层拆解的完整文章 |
| `detailed-v1-message-report.html` | 抓包导出的详细 trace 报告（单文件，约 7 MB，浏览器打开） |
| `anthropic-agentic-loop-flowchart-cartoon.png` | agentic 循环流程示意 |
| `claude-api-trace-table.png` | API trace 汇总表 |
| `claude-cli-api-path-counts.png` | CLI 各 API path 调用次数分布 |
| `claude-messages-request-body-structure.png` / `...-v2.png` | `/v1/messages` 请求体字段结构 |
| `claude-tools-definitions-toolsearch-gateway.png` | tools 定义与 ToolSearch 网关 |
| `haiku-preflight-check.png` | Haiku 预检请求 |

## 参考

- claude-trace（HTTPS 抓包工具）：<https://github.com/badlogic/lemmy/tree/main/apps/claude-trace>
- cchistory（Claude Code system prompt 版本浏览器）：<https://cchistory.mariozechner.at/>
