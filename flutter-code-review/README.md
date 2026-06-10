# flutter-code-review：多端 CR skill + 方法论溯源分析

把一个用于 AI 自动 Code Review 的 Claude Code skill（`flutter-code-review`），与它的「思想源」——一篇经典 CR 方法论文章《8行代码提出的21个问题》——放在一起收录，并给出二者的逐条对照分析。

## 这是什么

- **`skill/`** —— 一个名为 `flutter-code-review` 的 Claude Code skill 包，用来 review 多端（Flutter / iOS / Android / HarmonyOS）代码。核心是 **5 维度审查（D1 背景 / D2 逻辑 / D3 异常 / D4 规范 / D5 非功能）+ TASK 链 + 假阳性过滤 + 可信度复核**，平台特定规则下沉到 `references/{platform}.md`（目前 Flutter 规则包就绪）。
- **`ANALYSIS.md`** —— 详细分析文档：把这篇文章的方法论（6 维度 / 21 问题 / 三层抽象 / 九宫格）与这个 skill 的工程实现逐条建立映射，论证 **「文章是思想源、skill 是工程化落地」**，并指出 skill 的工程增量与改进方向。

## 核心结论（详见 [`ANALYSIS.md`](./ANALYSIS.md)）

- skill 的 5 维度 ≈ 文章 6 维度（文章「可测性」被并进 D5「非功能点」）。
- 文章在 8 行代码上挖出的 **21 个问题，21/21 都能命中 skill 的某条 checklist** —— 规则库本就是从这类经验长出来的。
- 文章第 3 层呼吁的「**经验沉淀到技术**」「**用 AI 和数据发现未知**」，正是这个 LLM CR Agent 的兑现。
- skill 在文章之上的原创增量，是把人本方法论改造成 Agent 程序时补的工程税：**假阳性过滤、可信度复核、严重度分级、TASK 链、上下文强制产出物**。

## 资产清单

```
flutter-code-review/
├── README.md                     # 本文件
├── ANALYSIS.md                   # 详细对照分析（主交付物）
└── skill/                        # flutter-code-review skill 原始内容
    ├── SKILL.md                  # 纯流程框架：5 维度 + TASK 链 + 双层过滤
    ├── references/
    │   └── flutter.md            # Flutter 硬规范（9 组 checklist）
    └── scripts/
        └── search_deps.py        # 跨 Pods / .pub-cache / gradle 的依赖源码检索
```

## 使用方式

把 `skill/` 整个目录放进 `~/.claude/skills/flutter-code-review/`（或项目级 `.claude/skills/`），在多端工程里让 Claude「review 当前 diff」即可触发。
