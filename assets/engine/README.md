# comsol-mcp

**让 AI agent 直接读懂、修改、计算和查看你的 COMSOL 模型 —— 全程在 COMSOL 桌面里可视化。**
*Let an AI agent understand, modify, solve and post-process your COMSOL Multiphysics models — with the COMSOL desktop visible the whole time.*

[![CI](https://github.com/your-name/comsol-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/your-name/comsol-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 中文

### 这是什么

一个 [MCP](https://modelcontextprotocol.io) 服务器，把 COMSOL Multiphysics 接到你的 AI agent（ZCode、Claude Code、Cursor、Codex、Gemini CLI、VS Code Copilot…）上。它通过 **COMSOL Multiphysics 服务器**工作：模型在服务器内存里，COMSOL 桌面窗口同时连着同一个服务器，于是

- agent 改参数、改设置、划网格、求解，**每一步都在 COMSOL 桌面里实时可见**；
- 求解进度会实时转发到对话里（并且可以切回 COMSOL 自带的进度窗口）；
- 算完后结果就在桌面上，你可以直接查看、导出、继续手动操作。

### 能做什么

| 你想说 | agent 会做 |
|---|---|
| "读一下这个模型，讲讲它是什么" | 结构化摘要（参数/材料/物理场/网格/研究/结果）+ 导出 Java 源码深读 |
| "把参数 R 改成 5cm，网格调细一点，重新算" | 改参数 → 重建网格 → 求解（带进度）→ 快照备份 |
| "把温度分布图导出来我看看" | 渲染绘图组为 PNG，agent 自己也能看图并解释 |
| "这个模型的网格质量怎么样？" | 网格统计（单元数、最小质量等） |
| "算一下出口的平均流速" | 表达式求值（大规模结果落盘 CSV） |

### 环境要求

- COMSOL Multiphysics **6.0 – 6.3**（已在 **6.2** 上完整实测），本地安装 + 有效许可证
- Python **3.10+**（由 MCP 服务器自动使用 `MPh` 连接 COMSOL，不需要装 Java）
- Windows 已完整验证；Linux/macOS 理论上可用（`comsolmphserver` / `comsolmphclient` 路径由 MPh 自动发现）

### 快速开始

1. **安装**（任选其一）

   ```bash
   # 从 PyPI（发布后）
   pip install comsol-mcp

   # 或从源码
   git clone https://github.com/your-name/comsol-mcp
   cd comsol-mcp && pip install -e .
   ```

2. **配置到你的 agent** —— 直接对你的 agent 说：

   > 帮我配置 comsol-mcp。先问我：是只在当前项目用，还是全局所有项目都能用。

   agent 会读取本仓库的 [AGENTS.md](AGENTS.md)，问你"项目 / 全局"，然后自动写入配置（各 agent 的路径见 [docs/agents.md](docs/agents.md)）。

   也可以手动一条命令：

   ```bash
   comsol-mcp install --agent zcode --scope project      # 或 --scope global
   comsol-mcp install --agent cursor --scope global --dry-run   # 先看会改什么
   ```

3. **开始用**（重开一个对话后）：

   > 启动会话并打开 `E:\models\busbar.mph`，告诉我模型结构；然后把铜的电导率改成 5.8e7，重建网格并求解，导出电势分布图。

### 工具一览（31 个）

| 分组 | 工具 |
|---|---|
| 会话 | `comsol_detect`, `comsol_start_session`, `comsol_attach_session`, `comsol_session_status`, `comsol_end_session` |
| 桌面窗口 | `comsol_focus_desktop`（把 COMSOL 窗口带到前台）, `comsol_close_desktop`（优雅关窗，自动应答"不保存"） |
| 模型文件 | `comsol_list_models`, `comsol_create_model`, `comsol_load_model`, `comsol_save_model`, `comsol_snapshot`, `comsol_snapshot_list`, `comsol_set_progress_mode` |
| 理解模型 | `comsol_describe_model`, `comsol_node_tree`, `comsol_get_property`, `comsol_export_source` |
| 修改模型 | `comsol_get_parameters`, `comsol_set_parameters`, `comsol_set_property`, `comsol_run_feature`, `comsol_mesh` |
| 计算 | `comsol_solve`（阻塞式，进度实时进对话）；`comsol_solve_start` / `comsol_solve_status`（后台求解 + 轮询，适合长计算） |
| 后处理 | `comsol_evaluate`, `comsol_export_output`, `comsol_export_plot_image`, `comsol_show_results`, `comsol_run_python`（高级，需显式开启） |

### 安全设计

- **任何修改或求解之前自动快照**到 `<工作区>/.comsol-mcp/history/`（可用 `COMSOL_MCP_AUTOSNAPSHOT=0` 关闭）；
- `comsol_save_model` **默认绝不覆盖原文件**，除非显式 `allow_overwrite=True`；
- 不依赖"暴力杀进程"：COMSOL 窗口由用户正常关闭（强制杀会破坏服务器会话，见排障文档）；
- `comsol_run_python`（任意代码执行）默认关闭，需设置 `COMSOL_MCP_ALLOW_CODE=1`。

### 工作原理

```
AI agent ──MCP(stdio)──> comsol-mcp ──MPh/COMSOL API──> COMSOL Multiphysics Server（模型在内存）
                                                              ▲
                                         COMSOL 桌面客户端 ─────┘（同一服务器，实时看到每一步）
```

详见 [docs/architecture.md](docs/architecture.md)，实测记录见 [docs/findings-m0.md](docs/findings-m0.md)。

---

## English

### What it is

An [MCP](https://modelcontextprotocol.io) server that connects COMSOL Multiphysics to your AI agent
(ZCode, Claude Code, Cursor, Codex, Gemini CLI, VS Code Copilot, ...). It drives a **COMSOL
Multiphysics server**, while a COMSOL desktop client is attached to the *same* server — so every
change the agent makes is visible live in the COMSOL window, solving streams progress into the
conversation, and the results are waiting for you in the COMSOL UI when the agent is done.

### What you can ask for

- *"Read this model and explain it."* → structured summary + full Java source for deep reading.
- *"Set R to 5 cm, refine the mesh, re-solve."* → parameters, mesh rebuild, solve with live progress.
- *"Export the temperature plot."* → renders a plot group to PNG (the agent can look at it).
- *"How good is this mesh?"* → element counts and quality statistics.
- *"What's the average velocity at the outlet?"* → expression evaluation (large results to CSV).

### Requirements

- COMSOL Multiphysics **6.0 – 6.3** (fully tested on **6.2**), local install with a valid license.
- Python **3.10+** — the server uses `MPh`; no separate Java setup needed.
- Windows is fully verified; Linux/macOS should work (executables are discovered by MPh).

### Quick start

```bash
pip install comsol-mcp                       # or: git clone ... && pip install -e .
comsol-mcp install --agent zcode --scope project   # or --scope global
```

Then tell your agent: *"Open `busbar.mph`, summarize it, set the copper conductivity to 5.8e7,
rebuild the mesh, solve, and export the potential plot."*

Agents that read [AGENTS.md](AGENTS.md) (this repo) will ask you "project or global scope?"
and configure themselves; per-agent paths are in [docs/agents.md](docs/agents.md).

### Safety

Automatic snapshots before every modification, no overwriting of your original `.mph` unless you
explicitly allow it, and the COMSOL desktop window is never force-killed.

### License

MIT — see [LICENSE](LICENSE). COMSOL is a trademark of COMSOL AB; this project is not affiliated
with or endorsed by COMSOL AB.