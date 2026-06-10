# AIWorkspace

围绕 AI 编程 / Agent 的研究与工具的合集仓库（monorepo）。每个独立课题或工具放在仓库根下的一个**独立子目录**里，彼此解耦、各带自己的 `README.md`。

## 目录约定

```
AIWorkspace/
├── README.md            # 本文件：仓库总索引 + 布局约定
├── .gitignore
└── <project>/           # 一个课题 / 工具 = 一个顶层目录
    ├── README.md        # 该项目的说明、资产清单、使用方式
    └── ...              # 该项目自己的源码 / 文档 / 资产
```

新增项目时：在根目录下建一个语义化命名的子目录，放一份 `README.md`，再到下面这张「项目清单」里补一行即可。尽量不要把不同项目的文件平铺在仓库根目录。

## 项目清单

| 项目 | 类型 | 简介 |
| --- | --- | --- |
| [`claude-trace/`](./claude-trace) | 研究 / 文档 | 用 claude-trace 抓包，逐层拆解 Claude Code 一次 skill 调用在 HTTP 层做了什么 |

<!-- 后续项目按上表追加，例如 claude-console 等 -->
