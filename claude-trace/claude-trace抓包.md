# Claude Code 一次 Skill 调用做了什么 —— 基于 claude-trace 抓包的逐层拆解

Claude Code CLI 本质上是一个驻留在终端里的 Anthropic Messages API 客户端。当用户在 REPL 里敲下一行自然语言指令，或者一个 `/<skill-name>` 斜杠命令时，CLI 会把当前会话的全部上下文打包成一次 `POST /v1/messages` 请求发给 `api.anthropic.com`，模型走 Server-Sent Events 流式回包。当流里出现 `tool_use` 内容块，CLI 就在本地把对应的工具（Bash、Read、Edit、Glob、Grep 等）真的执行掉，再把执行结果包成下一轮请求里 user 消息的 `tool_result` 块。这个循环一直转，直到模型返回 `stop_reason: "end_turn"`，或者用户按 Ctrl-C 打断。

本文用 [claude-trace](https://github.com/badlogic/lemmy/tree/main/apps/claude-trace) 在 Node 进程级别 hook 住 Claude Code CLI 走的 HTTPS 栈，把每一对 request/response 写成一行 JSON Lines，从流量层把"一次 skill 调用"的完整时序剖开来看。

> 注：claude-trace 当前只兼容 Claude Code 版本小于 2.1.112。

本次实验在 Claude Code REPL 里输入的指令是一行：

```
/fliggy_code_view git diff
```

字面含义是：调用名为 `fliggy_code_view` 的 skill，把字符串 `git diff` 当作参数传给它，让它对当前工作区的未提交改动做一次代码评审。整理后的 HTTP trace 可以在线浏览：[Claude Code HTTP trace 页面](https://cdn.fliggy.com/normal/ol3nr4yu83vo.html#interface-requests)。

---

## 摘要

- **流量总量**：这一次 `/fliggy_code_view git diff` 触发的会话，在 HTTP 层一共留下了 **34 条** request/response 对，目的地全部是 `api.anthropic.com`。
- **核心请求**：34 条里只有 **11 条** 是 `/v1/messages`。剩下 23 条属于"会话外围"流量 —— 配置面、服务发现面、遥测面，跟模型推理本身解耦。
- **握手 + 主链**：11 条 `/v1/messages` 里，第 1 条是 CLI 启动握手时的连通性 ping；后续 **10 条** 是同一次 skill 调用的 Opus 主链，模型字段固定为 `claude-opus-4-7`，全部 `stream: true`、`max_tokens: 64000`。
- **静态前缀，动态尾巴**：这 10 次 Opus 调用共享同一份 **约 26,228 字符** 的 system prompt 和同一组 **10 个** tool schema，每一轮原样重发；真正在变的只有 `messages` 数组的长度，严格按 **1 → 3 → 5 → 7 → 9 → 11 → 13 → 15 → 17 → 19** 等差爬升。
- **9 次工具调用**：每一次工具往返会让下一轮请求的 `messages` 多出两条（一条带 `tool_use` 的 assistant 消息、一条带 `tool_result` 的 user 消息），所以 +2 等差等价于"9 次中间工具调用 + 1 次以 `stop_reason: "end_turn"` 收尾的最终回答"。这就是 agentic tool-use 循环在 HTTP 层的指纹。

---

## 1. 34 条 HTTP 请求的方法与状态码分布

![方法和状态码分布](https://oss-ata.alibaba.com/article/2026/05/3bab46c0-8c22-4f96-9eec-c343386c16aa.png)

按方法和状态码切一刀：

- **HTTP 方法**：24 个 POST、9 个 GET、1 个 HEAD。
- **状态码**：33 个 200、1 个 404。
- 那个孤零零的 404 来自 CLI 对根路径 `/` 的 HEAD 探活 —— Anthropic 网关对根路径的 HEAD 本就回 404，这是预期行为，CLI 会把它当成"端点可达"的信号吞掉，不抛错也不进入用户视野。

结论很直白：把 23 条配置 / 发现 / 遥测请求剔掉之后，真正决定这次 skill 推理走向的，只剩下那 11 条 `/v1/messages`。下文只看这 11 条。

---

## 2. 11 条 `/v1/messages` 的全貌

下表是这 11 条核心请求的概览（`line` 是该请求在 JSONL 文件里的 1-based 行号，`stream` 对应请求体的 `stream` 字段，`max_tok` 对应 `max_tokens`）：

![/v1/messages 11 条请求一览](https://oss-ata.alibaba.com/article/2026/05/effde8f8-2596-42ef-8477-fbd754765f76.png)

### 2.1 第 1 条：连通性 ping

`line 9` 那一条是 Anthropic 官方 CLI 的标准连通性 ping —— 在 REPL 启动握手阶段就发出来，请求体极短，目的就是确认凭证、网络通路、网关就绪，跟本次 skill 的语义没有关系。

![连通性 ping 请求](https://oss-ata.alibaba.com/article/2026/05/a9d78293-df5a-47bf-ae42-62653da44d7d.png)

### 2.2 剩下 10 条：同一次 skill 调用的 Opus 主链

后续 10 条 `/v1/messages` 才是这一次 skill 调用真正承载推理的 Opus 主链。它们的"静态部分"在 10 轮之间逐字相同：

| 字段 | 取值 |
| --- | --- |
| `model` | `claude-opus-4-7` |
| `stream` | `true` |
| `max_tokens` | `64000` |
| `tools` | 同一组 10 个 tool schema，每轮原样重发 |
| `system` | 同一份 ~26,228 字符的 system prompt，每轮原样重发 |
| `messages.length` | 第 _k_ 次调用为 `2k − 1`，即 1, 3, 5, 7, 9, 11, 13, 15, 17, 19 |

换句话说，**请求体的"静态前缀"在 10 次调用之间是完全相同的**，所有变化都落在 `messages` 这条对话历史的"尾巴"上。Anthropic Messages API 的 prompt cache 正好吃这种"长前缀稳定 + 尾巴小幅增长"的形状 —— 长 system prompt 和工具描述的 token 成本因此可以在多轮之间被摊薄，而不是每一次 Opus 调用都按全长计费。

---

## 3. 为什么恰好是 10 次：agentic tool-use 循环的指纹

`messages.length` 那条 1, 3, 5, …, 19 的等差数列，就是 agentic tool-use 循环在流量层留下的标准指纹。

![agentic tool-use 状态机示意](https://oss-ata.alibaba.com/article/2026/05/d90551db-25ba-4459-ac0b-8ea0b2a4539a.png)

一次工具往返的完整 5 步：

1. CLI 把当前对话状态打包成一次 `POST /v1/messages` 发给 Anthropic 网关。
2. 模型以 SSE 流式回内容，assistant 消息的 `content` 数组里出现一个或多个 `tool_use` 块。
3. CLI 在本地真正执行那一组工具（Bash / Read / Edit / Glob / Grep / WebFetch / 用户自定义工具 ……）。
4. CLI 把每一个工具的执行输出包成对应的 `tool_result` 内容块，塞进下一轮请求里 user 消息的 `content` 数组中，并用 `tool_use_id` 跟上一轮的 `tool_use` 一一对应起来。
5. 进入下一轮 `POST /v1/messages`，这一轮的 `messages` 数组就比上一轮多了两条消息 —— 一条带 `tool_use` 的 assistant，一条带 `tool_result` 的 user。

起点是 1（也就是最初那条用户输入消息），之后每完成一轮工具往返就 +2，所以序列正好是：

```
1, 3, 5, 7, 9, 11, 13, 15, 17, 19
```

落回本次 trace 上：

- `line 14` 是 **第 1 次** Opus 主调用。
- `line 32` 是 **第 10 次** 也是最后一次 Opus 调用 —— 这一轮模型不再发起新的 `tool_use`，而是直接把最终的代码评审正文流式吐回终端，以 `stop_reason: "end_turn"` 结束整个循环。
- 中间 8 轮各对应一次中间工具往返，加上 `line 32` 之前的最后一次工具往返，总共是 **9 次工具调用 + 1 次最终自然语言回答 = 10 次 Opus 调用**。

要把这 9 次工具调用的具体工具名一一抠出来，需要逐条解析 Opus response 的 `body_raw` 中的 SSE 字节流，找到所有 `event: content_block_start` 行，从中筛出 `content_block.type == "tool_use"` 的块，再读 `content_block.name` 字段。完整响应体在线视图：[v1/messages Response body](https://cdn.fliggy.com/normal/ol3nr4yu83vo.html#interface-requests)。

---

## 4. 一条 `/v1/messages` 请求体的字段图谱

把镜头拉近，取 trace 文件里时序居中的那条 Opus 调用 —— 也就是 `line 22`。它对应的会话状态是"tool-use 循环已经走过 4 轮"，因此 `messages.length == 9`（初始 1 + 每轮 +2 × 4 = 9）。请求地址是：

```
POST https://api.anthropic.com/v1/messages?beta=true
```

URL 末尾的 `?beta=true` 是网关侧开 beta 路由通道的开关，需要跟请求头里的 `anthropic-beta` 字段配套使用 —— 只带 query 不带 header、或者只带 header 不带 query，都不能完整启用对应的 beta 能力。

请求体的顶层字段树长这样：

![Request body 顶层字段结构](https://oss-ata.alibaba.com/article/2026/05/274faa21-a962-4dec-9dde-97b1941382da.png)

完整 JSON 在 trace 页面里：[Request body 详细内容](https://cdn.fliggy.com/normal/ol3nr4yu83vo.html#interface-requests)。

下面挑三块最值得看的字段拆开：`system`、`messages`、`tools`。

### 4.1 `system`：四段拼接而成的稳定头部

`system` 字段是一个 `content` 数组，在本次 trace 里由四段拼接而成、顺序固定、每一轮原样重发：

- **`system[0]` —— CLI 版本与运行环境信源。** 一段固定文本，标注当前 Claude Code CLI 的版本号、模型 ID、操作系统、shell 类型、工作目录这些会话级"机器铭牌"信息。

- **`system[1]` —— 角色定义。** 一段短文本告诉模型它扮演的角色是"Claude Code，一个跑在终端里、可以执行工具的工程助手"，对应传统 prompt engineering 里的 role 设定段。

- **`system[2]` —— harness 行为规约。** 这是 Claude Code CLI 自带的长篇内置 system prompt 主体，几乎不随用户、不随仓库变，是整份 prompt 里**最适合命中 prompt cache 的那一块**。它的常见小节包括：

  - `# System` —— 工具结果格式、hooks 机制、上下文压缩策略等系统级约定。
  - `# Doing tasks` —— 任务编排、TaskCreate 的用法、风险动作之前的人工确认要求。
  - `# Tone and style` —— 输出长度、Markdown 渲染范围、是否可以暴露内部推理。
  - `# Environment` —— 工作目录、操作系统、shell、当前模型 ID 等运行时占位。
  - 工具使用约定、安全边界、"refuse vs. comply"的工程纪律段落。

  整段内容的逐版本 diff，可以在 mariozechner 维护的 cchistory 项目里查看 —— 在线浏览器 <https://cchistory.mariozechner.at/?from=2.1.112&to=2.1.112>，配套博文 <https://mariozechner.at/posts/2025-08-03-cchistory/>。

- **`system[3]` —— 协作、输出与记忆规则。** 比 `system[2]` 更贴近"当前这台机器、这个用户、这一次会话"那一层的规则，主要含三块。

  *文本输出规范* —— 每一次调用工具之前先用一句话说明"接下来要做什么"；过程中只在关键节点（找到了什么、改变方向、遇到阻塞）更新一句；不输出内部思考过程；最终的收尾总结控制在一到两句。

  *协作与工具使用* —— 用户需要自己在终端跑命令时，提示对方在前面加 `! <command>` 前缀，由 CLI 把命令的标准输出接管到对话里；跨多次搜索的发散探索，可以委托给专用 Agent 来跑，但要避免和主线工作的搜索互相重复；用户敲 `/<skill-name>` 时，只允许调用当前已经在 skill 清单里列出的 skill，不能凭训练记忆瞎猜一个不存在的 skill 名。

  *记忆系统* —— Claude Code 维护一份按文件落盘的长期记忆库，分四类标签：`user`（用户画像）、`feedback`（显式纠偏）、`project`（项目状态）、`reference`（外部资源指针）。当用户明确说"记住 X"或"忘掉 Y"时，CLI 会去对应的目录里写入或者删除一份 markdown。这个目录的标准路径是：

  > `~/.claude/projects/<编码后的工作目录>/memory/`

  其中 `<编码后的工作目录>` 是当前工作目录的绝对路径，把每一个 `/` 替换成 `-` 之后得到的字符串。举个例子：工作目录 `/Users/qianyi/DevWorkspace/Scratch` 对应的子目录名就是 `-Users-qianyi-DevWorkspace-Scratch`，记忆文件全部落在它下面的 `memory/` 子目录里。这个目录里的 `MEMORY.md` 是这一份工作目录的记忆索引，一行一条 markdown 链接指向同目录下的具体记忆文件；每一份具体记忆文件本身都带一段 YAML frontmatter，记录自己的 `name` / `type` / `description`。`MEMORY.md` 的内容会在每一次 Claude Code 会话启动时被自动读进当前轮的上下文，被索引指过去的具体记忆 markdown 则是惰性加载的 —— 模型问到了哪一条、CLI 才会把它的正文拼进 user 消息。

  *记忆边界* —— 系统提示里写得很清楚：**凡是能从仓库本身重新推导出来的事实，都不应该写进记忆**。代码结构、文件路径、git 历史、当前任务的临时状态、某个 bug 的具体修复配方，这些事实的真正出处是源代码、`git log`、当前的对话上下文，不属于"跨会话"的长期记忆。`MEMORY.md` 装的只应该是"读代码读不出来的东西" —— 用户的角色和偏好、长期协作约定、外部系统的位置和入口，以及那些和具体 ticket 无关的工程纪律。

### 4.2 `messages`：user / assistant 严格交替的对话流

`messages` 是一个数组，元素的 `role` 严格交替出现：

```
user → assistant → user → assistant → user → assistant → ……
```

这种"一应一答"的形状，正是第 3 节那个 tool-use 循环在数据层的投影。

每一条 user 消息的 `content` 自己也是一个数组 —— 也就是说一条 user 消息可以塞**多个**内容块，不止"一段文本"那么简单。以本次 trace 中**最开头那一条 user 消息**为例（它装的是会话冷启动时 CLI 拼出来的"第零轮全局上下文"），它的 content 数组按顺序大致包含以下几块：

- **`content[0]` —— 当前可用工具的人话描述。** 一段告诉模型"这一次会话里挂了哪些工具、每一个工具大概是干什么用的、什么场合该选哪一个"的自然语言卡片。它和顶层 `tools` 字段是两份不同的东西：顶层 `tools` 是给网关用来校验 `tool_use` 调用合法性的 JSONSchema 协议层定义，而这里的 `content[0]` 是写在 user 消息正文里、给模型"读"的人类语言版工具卡片。这份卡片由 CLI 在会话启动时根据当前实际启用的工具集动态拼装出来，不是一份预先写死的固定文本。

- **`content[1]` —— 当前会话挂载的 MCP server 描述。** 列出在 CLI 启动时被 attach 进来的全部 MCP server 名字、每一个 server 自己的 instruction 文本段、以及它们暴露出的 tool 名字清单。本次 trace 里这一块只挂载了 **一个** MCP server —— `context7`（它的作用是为各种库和框架的官方文档做检索增强），为什么这一次会话恰好只挂了它一个、还在追因。这里能看出的一件事是：**MCP server 列表是按会话动态注入的，不是 CLI 启动时绑死的一组固定 server**。

- **`content[2]` —— 当前会话可用的 skill 清单。** 一份"name + 一行 description"的列表，告诉模型当用户敲出 `/<name>` 时各个 name 分别对应什么能力。skill 的来源通常有三层：用户级 skill 目录、项目级 skill 目录、Claude Code 内置自带的默认 skill 集。这份清单决定了 `/<skill-name>` 这种斜杠触发能不能落到正确的 skill 上。

- **`content[3]` —— 仓库上下文，也就是 CLAUDE.md / MEMORY.md 的合并文本。** 它把以下这一组 markdown 文件按顺序拼成一长串塞进来：

  - 当前工作目录里的 `CLAUDE.md`（如果存在的话）。
  - 从家目录开始一路向当前工作目录走的、每一级父目录里的 `CLAUDE.md`，按"父级在前、当前目录在后"的顺序链式合并。
  - 工作目录下 `.claude/rules/*.md` 这一类被规则目录约定自动捞起来的 markdown 文件。
  - 当前工作目录对应的那份 `MEMORY.md` 索引正文（具体的记忆条目仍然按 §4.1 描述的那样、单独存在 `memory/` 目录里，索引行只是"指过去"的链接）。

  有两个细节值得记住：第一，`.claude/rules/` 下的 markdown 是 Claude Code 自动加载的，不需要你在 `CLAUDE.md` 里显式声明引用关系；第二，`CLAUDE.md` 正文里如果用 `@some-file.md` 这种语法显式引用了别的一份 markdown，被引用的文件会在拼 `content[3]` 时**当场以整段纯文本的形式**被拼进来，而不是等到模型问起来再去 lazy-load —— 也就是说，在 `CLAUDE.md` 里乱写 `@xxx.md` 这种引用，是要每一轮请求都按完整文件长度付 token 成本的。

- **`content[4]` / `content[5]` —— 这一轮真正要给模型"看的"输入。** 一块是用户在 REPL 里输入的那一行原始命令文本（本次实验里就是 `/fliggy_code_view git diff` 那一行）；另一块是被命中的那一个 skill 自己附带的指令片段 —— skill 在文件系统上是一份带 YAML frontmatter 的 markdown，被触发时 Claude Code 会把它的正文（步骤说明、约束、对输出格式的要求）作为一个独立的 user 内容块塞进当轮请求，模型当轮就能直接看到这个 skill 的具体执行指南。

从第 2 轮开始往后，每一轮的 user 消息内容会简单很多 —— 它通常只剩一个 `tool_result` 块（把上一轮 CLI 本地工具执行的输出回灌给模型），有时再加一段 CLI 自己注入的 `<system-reminder>` 性质的元文本（提醒模型某些当前状态）。每一轮的 assistant 消息则是一条 SSE 流回包的产物：它的 `content` 里要么继续出现一个或多个 `tool_use` 块（循环还要继续），要么是最终的自然语言答复文本（循环结束，`stop_reason: "end_turn"`）。

### 4.3 `tools`：能力声明，而不是执行入口

顶层 `tools` 字段是一份 JSONSchema 数组，描述本次请求里**模型被允许调用**的那一组工具 —— 每一项至少包含 `name`、`description`、`input_schema`，有些工具条目还会带额外的元信息字段（比如是否标注为有副作用、是否需要在 CLI 侧弹一个人工确认提示）。

![tools 字段示意](https://oss-ata.alibaba.com/article/2026/05/0aac07f0-f71f-4ec9-b28c-e1f009aeb42a.png)

要强调的核心认知是：**`tools` 字段只是把"可调用的工具能力"作为协议级声明告诉网关和模型，工具本身不会因为出现在这个数组里就被自动执行**。一次完整的工具调用链路始终是这五步：

1. 模型在响应的 SSE 流里输出一个 `tool_use` 内容块，块里带着 `name` 和实际的参数。
2. Anthropic 网关把这个 `tool_use` 块**原样**转回给 CLI（网关从不替 CLI 跑工具，它只是消息总线）。
3. CLI 在用户机器本地把对应的工具真的执行掉，拿到 stdout / 返回值 / 错误。
4. CLI 把执行的产出包装成一个 `tool_result` 内容块，塞进**下一轮** `/v1/messages` 请求的 user 消息里，用 `tool_use_id` 跟它对应的那个 `tool_use` 关联起来。
5. 模型在下一轮请求里看到这个 `tool_result`，就知道上一次它发起的工具调用已经返回了什么观察结果，然后基于这个新观察决定下一步是再发一个 `tool_use` 还是收尾输出最终答复。

本次 trace 中的 10 条 Opus 调用共享的是同一份"10 工具"的清单（典型成员是 Claude Code 内置的那一组标准工具，逐条名字可以在 trace 文件里 `tools[*].name` 字段下读出来）。

---

## 5. Claude Code 的两个 prompt 缓存细节

本次 trace 里还能看到两处 Claude Code 给"长上下文 + 多轮工具调用"这一类组合做开销节流的设计细节，它们都体现在请求体里的 `cache_control` 标记上 —— 一个 `cache_control` 钉死在 `system` 头部不变的尾端，另一个 `cache_control` 跟着对话历史的最末端、在每一轮请求之间持续向后滑动。这两个标记合在一起，就构成了 Anthropic prompt-caching 机制下、多轮 agentic 工具调用循环里常用的"双断点"缓存布局。

### 5.1 长前缀：钉在 `system[2]` 末尾的缓存断点

这 10 轮 Opus 调用的 `system` 段使用的都是同一份 26,228 字符的内容数组，由 §4.1 拆过的那四段（CLI 版本信源、角色定义、harness 行为规约本体、协作 / 输出 / 记忆规则）顺序拼成。其中 **`system[2]`** —— 也就是中间那段大约 9,937 字符长的 Claude Code 内置 harness 行为规约本体 —— 在它的末尾挂着一个 `cache_control` 标记：

```json
{
  "cache_control": {
    "type": "ephemeral",
    "ttl": "1h",
    "scope": "global"
  }
}
```

这个标记的语义是告诉 Anthropic 服务端：把请求体从最开头一直到这个标记所在位置之间的整段内容（也就是 `system[0]`、`system[1]`、`system[2]` 这三段在 10 轮 Opus 调用之间逐字相同的"长前缀"）作为一条 `ephemeral` 类型的 prompt-cache 条目缓存起来，**有效期 1 小时**。在这一小时窗口里，任何后续 `/v1/messages` 请求，只要它的请求体前缀和这条被缓存的前缀**逐字节相同**，那一段前缀对应的输入 token 在网关侧就按 cache-read 的折扣价计费，而不是按"完整输入长度"再按原价重新算一次。

字段里那个 **`scope: "global"`** 来自 Anthropic 的一个扩展 beta 通道 —— 在请求头里能看到 `anthropic-beta: prompt-caching-scope-2026-01-05` 这个能力标识。它的作用是把这条缓存键的可见范围，从默认的"同一账号、几分钟级的短窗口"放宽成"**在 `ttl` 字段限定的有效期内全局共享**"，也就是说有效期不再由那个隐含的短窗口决定，而是直接由 `ttl: "1h"` 决定。落到 Claude Code 这种"一个 CLI 长挂在终端里、对话之间隔几分钟到几十分钟很正常、还可能并发开多个会话"的客户端形态上，这个 `global` scope 加上 1 小时 TTL 的组合显著拉高了 26K 字符长 system prompt 的 cache hit 率 —— 它不会因为会话之间的空窗或者短窗口超时被打断，只要离上一次发同样前缀不超过一小时，下一次还能继续命中前一次留下来的那个缓存槽位。

### 5.2 滑动尾巴：每一轮 `tool_result` 末端的缓存断点

除了 §5.1 那个固定钉在 `system[2]` 末尾的"前缀缓存断点"，**每一轮请求**里 `messages` 数组的**最末一条消息**（按 §4.2 那条"user / assistant 严格交替"的规则，这一条的 `role` 必然是 `user`，因为下一回合该轮到 assistant 说话）—— 它的 `content` 数组里**最后一个 `tool_result` 内容块**上 —— **还**带着一个等价形状的 `cache_control` 标记（这一处没有显式给 `scope` 字段，因此回退到默认 scope）：

```json
{
  "cache_control": {
    "type": "ephemeral",
    "ttl": "1h"
  }
}
```

这一个标记的语义，是把"可缓存前缀的右边界"从 §5.1 钉死在 `system[2]` 末尾的位置**继续向右推**，一直推到当前这一轮对话历史的最末端 —— 也就是上一轮工具往返刚刚闭合的那一对 `tool_use` / `tool_result` 块的最后一个字节。等到**下一轮**请求，CLI 在 `messages` 数组尾巴上再追加一对新的"assistant 带新 `tool_use` + user 带新 `tool_result`"消息发出去时，**前面那一长段已经稳定下来的对话历史**（从最初的那条用户输入开始、一路到上一轮闭合的那次工具往返为止的所有 user / assistant 消息）整体都能命中上一轮请求埋下的这个新缓存断点 —— CLI 不需要为"反复重发同一段已经发送过的对话历史"再付一次原价输入 token 的钱。

换一种说法：每完成一次工具往返，CLI 就把这个 `cache_control` 标记**跟着对话历史的尾端向右滑动一格**，把"上一轮已经稳定下来的历史"和"这一轮才新增的尾巴"分别落到缓存命中和缓存未命中两侧，新增的输入 token 量始终只是"最新这一对 `tool_use` / `tool_result`"的长度，跟整条对话已经长到多少没关系。

想验证这套机制每一轮实际命中了多少 token，看的是该轮 SSE 回包里最早那一条事件 `message_start` 的 `usage` 对象 —— 它里面的 **`cache_read_input_tokens`** 字段给出的就是本轮请求实际从缓存中读到、按折扣价计费的输入 token 数（这个字段名以及它在 `message_start.usage` 路径下的位置，都是 Anthropic Messages API 流式协议里公开的标准字段）。把 10 条 Opus 主调用的 `cache_read_input_tokens` 累加起来，再跟它们各自 `input_tokens` 字段的总和做一次比，就能量化整段 agentic tool-use 循环靠这两个 `cache_control` 断点节省下来的输入开销在"理论全量输入"里所占的比例。

**"前缀钉死 + 尾巴滑动"** 这种"一头一个标记"的双断点布局，正是 Anthropic 官方 prompt-caching 文档里给"多轮工具调用 agentic 应用"这一类工作流推荐的标准姿势：它把一次长上下文请求自然地切成三段 —— 完全不变的 system 长前缀、上一轮已经收敛下来的对话中段、本轮才新追加的那一对 `tool_use` / `tool_result` 尾巴 —— 前两段全部走 cache-read 折扣价，只有最后这一对新消息按原价输入计费。本次 trace 里 Claude Code CLI 展示出来的，就是这一套布局的具体落地实现。

---

## 6. 总结

这一次 `/fliggy_code_view git diff` 的抓包，把 Claude Code 在 HTTP 层的工作模式拍得非常清楚：

- **它不是一次请求做完全部任务的"问答机"**，而是绕着 `POST /v1/messages` 这一个端点构造出来的**多轮 agentic tool-use 循环** —— 模型每一轮基于已经看到的上下文决定下一步动作，CLI 在本地把动作真正执行掉，再把结果回喂给模型，直到模型说"够了，出最终答案"，以 `stop_reason: "end_turn"` 收尾。

- **流量上的核心高度集中**：34 条 HTTP 请求里只有 11 条 `/v1/messages` 真正影响模型行为，其中第 1 条是握手 ping，剩下 10 条是同一次 skill 调用的连续 Opus 推理。`messages.length` 沿 1, 3, 5, …, 19 这一条等差数列单调爬升，这条数列本身就把"9 次中间工具调用 + 1 次最终自然语言回答"的内部结构裸露在外面了。

- **请求体的"静"与"动"被刻意分层**。"静"的那一层在 10 轮 Opus 调用之间逐字相同 —— Claude Code 内置的 26K 字符 system prompt（行为规约、角色定义、协作和记忆规则）、10 个工具的 JSONSchema 数组、首轮 user 消息里那一大坨 CLAUDE.md / MEMORY.md / MCP 描述符 / skill 清单凑出来的"全局会话上下文"。这一层的稳定，让 Anthropic Messages API 的 prompt cache 有一个长而稳定的、可被命中的前缀，把"长 system prompt × 多轮工具调用"组合在一起的 token 成本压到工程上可承受的量级。"动"的那一层是每一轮**新追加在 `messages` 尾巴上**的那一对 `tool_use` / `tool_result`，这两条新消息就是这一轮的全部新信息量。

- **读懂任意一段 Claude Code trace 的关键，不是只盯着最后一条 SSE 流里吐出的最终文本**，而是顺着 `/v1/messages` 的请求序列走一遍：把第 _k_ 轮 assistant 流里冒出来的每一个 `tool_use` 块、和第 _k+1_ 轮 user 消息里对应的那一个 `tool_result` 块串起来 —— 整条会话就会还原成一台"在自己的上下文里、一步一步往前推进的有限状态机"，这台状态机的转移函数就是模型本身。

---

## 附录：工具与参考链接

- claude-trace（HTTPS 抓包工具）：<https://github.com/badlogic/lemmy/tree/main/apps/claude-trace>
- cchistory（Claude Code 内置 system prompt 的版本浏览器）：<https://cchistory.mariozechner.at/?from=2.1.112&to=2.1.112>
- cchistory 介绍博文：<https://mariozechner.at/posts/2025-08-03-cchistory/>
- 本次实验对应的渲染后的 trace 页面：<https://cdn.fliggy.com/normal/ol3nr4yu83vo.html#interface-requests>
