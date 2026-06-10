# statusline：Claude Code 自定义状态栏

两个零依赖（除运行时外）的 Claude Code `statusLine` 脚本。Claude Code 每次刷新状态栏时，会把一份会话状态 JSON 通过 **stdin** 喂给配置的命令，脚本把它渲染成终端里那一行（多行）状态显示。

- **`statusline.py`** —— 主推。扁平 **Catppuccin Mocha** 风格，多行卡片，按「身份 / 会话 / 配额 / 活动」四个层级铺开。纯 Python 标准库，无第三方依赖。
- **`statusline-command.sh`** —— 简版备选。单行 **Gruvbox** 风格：`用户 | 目录 | git 分支 | 模型 | 时间`。需要 `jq` + `git`。

> 两者都是从 Claude Code 注入的 status JSON 取数，**不读凭证、不依赖插件**。`statusline.py` 仅在显示公网 IP/地理位置时调用一次 `ipinfo.io`（带 30 分钟缓存，可关）。

## `statusline.py` 显示什么

| 行 | 内容 |
| --- | --- |
| ① 身份 | 模型名、reasoning effort、thinking/fast 标记、当前目录（智能截断）、git 分支 + 脏文件/ahead/behind |
| ② 会话 | context 用量细线 gauge（绿→桃→红）+ 输入/输出 token、会话时长、`cost $`、本次 `diff +/−` 行数 |
| ③ 配额 | 原生 `rate_limits` 的 5h / 7d 用量 gauge + 重置倒计时、公网 IP + 城市/国家、当前时间 |
| ④ 活动 | 本次会话调用过的 tools / skills / mcp servers（取 Top N）、subagents 数、errors 数、todos 完成度 |

某一行/某一项无数据时自动省略。context 在 Claude Code 2.1+ 直接读 `context_window`；旧版回退到从 transcript 末尾估算。

## 依赖

- **`statusline.py`**：Python 3 + 一款 **Nerd Font**（状态栏图标是 Nerd Font 私用区字形，终端字体得是 Nerd Font 才能正常显示，否则是豆腐块）。
- **`statusline-command.sh`**：`bash` + `jq` + `git`。

## 安装

### 主推：`statusline.py`

1. 拷贝脚本到你的 Claude 配置目录：

   ```bash
   cp statusline.py ~/.claude/statusline.py
   ```

2. 在 `~/.claude/settings.json` 里加上 `statusLine` 配置（已有则改 `command`）：

   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "python3 ~/.claude/statusline.py"
     }
   }
   ```

   > 若你的 Claude Code 版本不支持 `~` 展开，改成绝对路径，例如 `python3 /Users/<you>/.claude/statusline.py`。

3. 把终端字体设成任意 **Nerd Font**（如 *JetBrainsMono Nerd Font*、*MesloLGS NF*），确保图标可见。

4. 新开一个 Claude Code 会话（或继续当前会话），状态栏即生效。

### 备选：`statusline-command.sh`

```bash
cp statusline-command.sh ~/.claude/statusline-command.sh
chmod +x ~/.claude/statusline-command.sh
```

`settings.json`：

```json
{
  "statusLine": {
    "type": "command",
    "command": "~/.claude/statusline-command.sh"
  }
}
```

## 使用 / 自测

状态栏由 Claude Code 自动刷新，无需手动运行。想离线验证渲染效果，可以手动喂一段 status JSON：

```bash
echo '{
  "model": {"display_name": "Claude Opus 4.8 (1M context)", "id": "claude-opus-4-8[1m]"},
  "cwd": "'"$HOME"'/code/demo",
  "cost": {"total_cost_usd": 1.23, "total_duration_ms": 845000, "total_lines_added": 42, "total_lines_removed": 7},
  "context_window": {"used_percentage": 38},
  "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": '"$(($(date +%s)+7200))"'}}
}' | python3 statusline.py
```

> 真实运行时，`transcript_path` 由 Claude Code 注入，第④行（tools/skills/mcp）才会有数据；手动喂 JSON 时一般为空，属正常。

## 配置（`statusline.py`）

脚本顶部一段常量即所有可调项：

- `CTX_BAR` / `QUOTA_BAR` —— context、配额 gauge 的长度（单元格数）。
- `ITEM_GAP` / `INDENT` / `CARD_PADX` / `CARD_PADY` / `CARD_HALF` / `ROW_GAP` —— 间距、缩进、卡片内边距与行距。
- 配色直接改 `# Catppuccin Mocha` 区块的 RGB 元组；图标在 `# Nerd Font glyphs` 区块按码点定义（不要直接粘贴字面字符，私用区字形存进源码会丢）。
- **关掉公网 IP/定位**：删掉/注释 `build_rows` 里第③行那段 `ip = get_ipinfo() …` 即可（连带不再请求 `ipinfo.io`）。

## 注意

- 脚本运行时会在 `~/.claude/` 下写两个**缓存**文件：`.statusline-ipinfo.json`（公网 IP/城市，30 分钟 TTL）和 `.statusline-tools.json`（增量解析 transcript 的工具计数偏移）。两者是本机运行态、含本机信息，**已被排除、未纳入本仓库**；你本地首次运行才会生成。
- 图标显示为方块/问号 = 终端字体不是 Nerd Font。
- 第③行的 `rate_limits`、第②行的精确 `context_window` 需要较新的 Claude Code（2.1+）；旧版会自动降级。
