[English](README.md) | [简体中文](README.zh-CN.md)

# Harness Harbor

**把本地 AI 编程 CLI 变成 AI Supervisor 可调度的 worker pool。**

Harness Harbor 是一个面向 AI 编程 harness 的轻量级本地执行代理层。

它通过统一的 MCP 控制面暴露本地安装的 **Codex**、**AGY**、**MiniMax** 等工具，让上游 AI Supervisor 可以分发边界明确的编程任务、追踪长时间运行的 job、管理工作区访问、恢复执行状态，并持续推进多阶段开发工作流。

一种很实用的使用方式是：

```text
ChatGPT Chat
     │
     │ 推理 · 规划 · 审查
     │ 验收 · 定时唤醒
     │
     │ MCP
     ▼
OpenAI Secure MCP Tunnel
     │
     ▼
Harness Harbor
     │
     ├── Codex
     ├── AGY
     └── MiniMax
          │
          ▼
      本地 worktree
```

在这套架构中，**ChatGPT 仍然是 Supervisor**。

Harbor 并不是用另一个本地 autonomous agent 替代 ChatGPT，而是给 ChatGPT 对话提供一个可靠的本地执行层。

ChatGPT 可以把一个较大的项目拆成多个边界清晰的阶段，每次只向 coding harness 下发一个聚焦任务，随后检查结果并决定下一步做什么。

如果再配合 **ChatGPT Scheduled Tasks**，Supervisor 还可以在之后自动回来，检查已经完成的 Codex job，决定通过或驳回，然后继续派发下一阶段任务。

这样，一个开发工作流就不必局限在单次交互式聊天里。

> **平台状态：** Windows 1.1.0 CI 和 ZIP 构建已通过；真实安装与系统集成验收仍未完成。<br>
> macOS 已有 Apple Silicon 开发实现：原生 SwiftUI Lighthouse 和内置 Python Runtime。提供 Apple Silicon DMG 预览版；正式发布验收尚未完成，Intel 未验证，详见 [macOS 说明](MACOS.md)。

## 下载与平台版本

- **macOS 1.1.0 预览版（Apple Silicon）**：[下载 DMG 与 SHA256 校验文件](https://github.com/JL066/harness-harbor/releases/tag/macos-v1.1.0-preview.1)。仅 ad-hoc 签名、未公证，Gatekeeper 可能阻止打开；尚非正式稳定版。
- **Windows 1.1.0**：Windows Python 3.11／3.12 CI 和 ZIP 构建已通过；真机 launcher／托盘、Credential Manager、Task Scheduler 和已安装 harness 验收尚未完成。当前仅提供源码，不发布 Windows 二进制。
- 两个平台独立发布：macOS 标签 `macos-v<版本>`，Windows 标签 `windows-v<版本>`。预览版追加 `-preview.N`，不混用 Release 或安装包。共享源码仍在同一仓库维护。

测试范围见 [测试矩阵](docs/convergence/TEST_MATRIX.md)。


### macOS 密钥存储

当前 **preview.1** 使用本地独立文件保存 Harbor 密钥，不再访问钥匙串，并包含 macOS AGY 写入权限和失败终态修复。本预览版替代已撤回的早期测试包，请用 SHA256 校验和区分安装包。

文件位置：`~/Library/Application Support/Harness Harbor/credentials/`

| 文件 | 用途 |
| --- | --- |
| `tunnel-runtime-key.txt` | 已配置的 Tunnel Runtime Key |
| `codex-custom-api-key.txt` | 可选的自定义 Provider API Key |

只有明确保存某项密钥时，才创建对应文件。只使用官方 Codex 时，不创建或加载自定义密钥文件；启用对应 Provider 或路由时才加载，替换或清除时会读取旧值用于失败回滚。关闭自定义 Provider 会保留已保存的密钥。

目录权限为 `0700`，文件权限为 `0600`。文件是当前账户可读取的明文，与 `settings.json` 分开，请勿分享。Git 忽略这些文件，诊断导出不包含其内容。设置 `HARBOR_USER_SETTINGS_DIR` 可调整设置和密钥目录的位置。

旧钥匙串条目不会被读取、自动导入或删除；升级后需重新填写一次已保存的密钥。Windows 继续使用 Credential Manager。


---

## 为什么需要 Harness Harbor？

AI coding tools 越来越强，但它们通常分别存在于不同 CLI 后面，各自拥有独立的认证方式、模型目录、配额行为、进程语义和执行状态。

如果没有一个本地 broker，上游 Supervisor 往往需要自己处理每个 worker 的专用胶水代码：

```text
Supervisor
   ├── Codex integration
   ├── AGY integration
   ├── MiniMax integration
   ├── job tracking
   ├── concurrency handling
   ├── workspace locking
   └── crash recovery
```

Harness Harbor 把这些能力收敛成一个统一的执行面：

```text
Supervisor
    │
   MCP
    │
    ▼
Harness Harbor
    │
    ├── Codex
    ├── AGY
    └── MiniMax
```

Supervisor 保留推理与编排循环。

Harbor 负责本地执行循环。

---

## ChatGPT 可以直接作为 Supervisor

Harness Harbor **不要求你再部署一个长期运行的本地 AI agent runtime**。

一个普通的 ChatGPT 对话就可以继续承担这些工作：

- 讨论需求；
- 推理系统架构；
- 把项目拆成多个阶段；
- 决定每个阶段交给哪个 worker；
- 审查实现结果；
- 驳回失败工作或重新定向；
- 判断项目何时可以继续推进。

Harbor 位于这个对话层之下。

```text
┌───────────────────────────────────────┐
│             ChatGPT Chat              │
│                                       │
│ 理解 · 推理 · 规划                    │
│ 审查 · 验收 · 重试 · 定时            │
└───────────────────┬───────────────────┘
                    │
                 MCP tools
                    │
                    ▼
          OpenAI Secure MCP Tunnel
                    │
                    ▼
┌───────────────────────────────────────┐
│           Harness Harbor              │
│                                       │
│ Probe · Route · Queue · Lease         │
│ Run · Track · Recover                 │
└───────────┬──────────┬────────────────┘
            │          │
            ▼          ▼
          Codex       AGY       MiniMax
            │          │          │
            └──────────┴──────────┘
                       │
                       ▼
                  本地 worktree
```

各层职责刻意保持分离。

### ChatGPT / 上游 Supervisor

Supervisor 负责：

- 理解用户意图；
- 规划工作；
- 把大型目标拆成多个阶段；
- 选择合适的 coding harness；
- 为每个 worker 提供聚焦且边界清晰的任务；
- 评估返回结果；
- 判断当前阶段是否通过；
- 决定重试、修正还是继续；
- 必要时安排之后的检查。

### Harness Harbor

Harbor 负责：

- 通过 MCP 暴露本地 worker pool；
- 探测 harness 可用性；
- 排队任务；
- 控制并发；
- 租约管理工作区；
- 启动本地 coding job；
- 持久化 job 状态；
- 返回结果；
- 恢复过期 reservation 和执行状态。

### Coding harness

Codex、AGY、MiniMax 或其他受支持 worker 负责真正的实现工作，例如：

- 读取仓库；
- 编辑代码；
- 执行命令；
- 运行测试；
- 检查结果；
- 完成 Supervisor 分配的有边界任务。

Coding harness 是 **worker**，而不是长期运行的项目 Supervisor。

---

## 长期 Supervisor，短任务 worker

这套工作流背后的一个核心设计原则很简单：

> **让 Supervisor 保存项目连续性，让每个 coding job 保持聚焦且有边界。**

与其让一个 coding agent 在同一个不断增长的 session 里从头跑完整个项目，ChatGPT 可以把项目拆成更小的阶段。

例如：

```text
项目目标
    │
    ▼
阶段 1：审查架构
    │
    ▼
Codex job
    │
    ▼
Supervisor 验收
    │
    ▼
阶段 2：实现后端修改
    │
    ▼
Codex job
    │
    ▼
Supervisor 验收
    │
    ▼
阶段 3：补充测试
    │
    ▼
AGY job
    │
    ▼
Supervisor 验收
    │
    ▼
阶段 4：发布审计
```

每个 worker 都只解决 **一个具体问题，并带有明确边界与验收标准**。

Supervisor 则保留整个项目的大图景。

这种分层方式有助于减少超长 coding-agent session 中常见的一些问题：

- **上下文漂移（context drift）** —— 随着执行历史不断增长，早期需求逐渐失去显著性；
- **范围膨胀（scope creep）** —— worker 开始修改原任务之外的内容；
- **指令稀释（instruction dilution）** —— 重要约束被淹没在越来越大的累计上下文中；
- **过期假设（stale assumptions）** —— 很早以前形成的结论在仓库状态已经变化后仍继续影响后续工作；
- **审查疲劳（review fatigue）** —— 实现和验收混在一起，而不是在清晰的阶段边界上进行。

Harbor 并不声称能让 context window 的限制消失。

它做的是让另一种工作方式变得实际可用：

```text
大型项目上下文
        │
        ▼
    Supervisor
        │
        ├── 聚焦任务 A ──► worker
        │                   │
        │◄────── 结果 ──────┘
        │
        ├── 审查 / 更新项目状态
        │
        ├── 聚焦任务 B ──► worker
        │                   │
        │◄────── 结果 ──────┘
        │
        └── 审查 / 继续
```

长期运行的推理循环和实际 coding execution 不再必须处于同一个 session 中。

这样更容易让每个实现阶段保持狭窄、可测试，并且可以被独立验收。

---

## 配合 ChatGPT Scheduled Tasks 的持续工作流

Harbor 的持久化 job 在与 ChatGPT 自带的定时能力结合时尤其有用。

一个 coding job 可能运行几分钟，也可能更久。

用户不应该每次都需要手动回来问：

> Codex 做完了吗？

更自然的工作流可以是：

```text
用户请求
     │
     ▼
ChatGPT 规划阶段 1
     │
     ▼
task_start(...)
     │
     ▼
Harbor 返回 job_id
     │
     ▼
ChatGPT 安排未来检查
     │
     │
     │     Codex 在本地继续工作
     │
     ▼
Scheduled Task 唤醒 ChatGPT
     │
     ▼
task_poll(job_id)
     │
     ▼
ChatGPT 检查结果
     │
     ├── PASS
     │     │
     │     ▼
     │   规划阶段 2
     │     │
     │     ▼
     │   task_start(...)
     │     │
     │     ▼
     │   安排下一次验收
     │
     ├── NEEDS FIX
     │     │
     │     ▼
     │   派发修正任务
     │
     └── COMPLETE
           │
           ▼
        汇报最终结果
```

关键在于：Scheduled Task 的唤醒不只是一个提醒。

Supervisor 可以利用后续运行 **真正继续监督循环**：

```text
Plan
  ↓
Dispatch
  ↓
Wait
  ↓
Wake
  ↓
Inspect
  ↓
Accept / Reject
  ↓
Plan next bounded stage
  ↓
Dispatch
  └──────────────↺
```

例如，ChatGPT 可以：

1. 让 Codex 实现一个后端修改；
2. 保存 Harbor 返回的 `job_id`；
3. 安排稍后的检查；
4. 之后自动回来并轮询该 job；
5. 检查修改文件和测试结果；
6. 如果不满足验收标准，则驳回实现；
7. 再派一个小型修正任务；
8. 验收修正结果；
9. 单独派发测试编写任务；
10. 最后再执行一个独立的发布审计阶段。

在这些阶段被持续监督时，用户不需要一直守着工作流。

这使一个普通 ChatGPT 对话可以成为实用的长期开发 Supervisor，而不需要把推理层迁移进另一个本地 agent runtime。

Scheduled Tasks、MCP integration、持续权限和无人值守 action 的具体可用性取决于用户的 ChatGPT plan 和 workspace 配置。

Harbor 自身并不实现 scheduler。

它提供的是让这种监督模式成为可能的 **持久化执行状态**。

---

## 为什么不直接把整个项目都交给一个 agent？

当然可以。

Harbor 并不会阻止这种使用方式。

但对于较大的项目，还存在另一种很实用的模式：

```text
一个超长 agent session

需求
   ↓
架构
   ↓
实现
   ↓
更多实现
   ↓
测试
   ↓
调试
   ↓
更多调试
   ↓
发布
```

随着执行历史持续增长，worker 需要携带越来越多的累计上下文和历史决策。

Harbor 让阶段化替代方案变得更容易实现：

```text
              Supervisor
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
      阶段 1    阶段 2    阶段 3
        │         │         │
      worker    worker    worker
        │         │         │
        └────── 结果 ───────┘
                  │
                  ▼
             Supervisor 验收
```

每个任务都可以拥有：

- 一个狭窄目标；
- 一个明确工作区；
- 显式约束；
- 验收标准；
- 清晰的停止点。

Supervisor 再根据仓库的真实最新状态，决定下一项任务应该是什么。

这种架构可以减少上下文漂移和范围膨胀，同时让中间验收更容易进行。

它也允许不同 coding harness 分别处理不同阶段，而不需要改变上层工作流。

---

## Harbor 提供什么？

### 一个 MCP 控制面

Supervisor 与 Harbor 对话，而不需要为每一个 coding CLI 单独实现一套完整的执行集成。

### 持久化 jobs

Job 会持久化到 `.jobs/`。

Supervisor 可以现在提交任务，保存稳定的 `job_id`，之后再回来检查同一个 job。

这次检查可以发生在另一个监督回合中，而不是必须紧跟在任务提交之后。

### Harness 探测

Harbor 可以检查受支持的本地 harness integration，并暴露其已验证的可用性和能力。

### Workspace lease

Harbor 可以避免互相冲突的 worker 同时占用同一个项目工作区。

### 按 harness 控制并发

队列中的任务会根据每个 harness 的并发限制进行 lease。

### 受监管的本地子进程执行

Worker 以受控本地进程运行，并具有有边界的执行行为和持久化状态。

### Recovery

在 worker 或 daemon 故障、重启后，Harbor 可以回收 stale reservation 和 workspace lease。

### Codex execution routing

对于 Codex，Supervisor 可以显式选择多个执行 route，包括在官方 provider 与通用 custom Responses API provider 之间进行受控 fallback。

### 权威遥测与配额

Harbor 提供统一的只读遥测模型（`harness_telemetry`），涵盖各 harness 状态、进程活动与 AGY 模型列表。仅在存在权威 provider 来源时报告配额；否则明确报告不可用，绝不擅自猜测。

### 安全 Git 交付

Harbor 暴露受限的 `git_ls_remote`、`git_push_dry_run` 和 `git_push_ref` 工具，用于安全分支交付，不暴露通用 shell 访问，也不接受凭据注入。

---

## Harbor 刻意不是一个 agent framework

Harness Harbor 的目标不是变成另一个 autonomous-agent runtime。

三层职责分别是：

```text
Supervisor / ChatGPT
────────────────────────────────────
理解
推理
规划
拆分任务
选择 worker
审查结果
接受 / 驳回
安排未来工作

                │
               MCP
                │
                ▼

Harness Harbor
────────────────────────────────────
Probe
Route
Queue
Lease
Run
Persist
Track
Recover

                │
                ▼

Coding harness
────────────────────────────────────
检查仓库
编辑代码
执行命令
运行测试
完成被分配的任务
```

Harbor 不提供：

- 通用推理；
- 项目规划；
- 结果验收；
- 对话记忆；
- 人格；
- 替代性的聊天界面；
- 长期 autonomous reasoning loop；
- 定时唤醒能力。

Supervisor 仍然是大脑。

Coding harness 仍然是 worker。

Harbor 负责连接两者。

---

## 支持的 harness

当前集成包括：

| Harness | 状态 | 说明 |
| --- | --- | --- |
| Codex | 已支持 | 包含可配置 execution routes |
| Antigravity / AGY | 已支持 | 使用本地已安装并配置好的 CLI |
| MiniMax | 已支持 | 使用本地已安装并配置好的 CLI |

每个 CLI 都必须已经在本机安装并完成配置。

Harbor 不捆绑：

- coding CLI；
- 模型；
- 凭据；
- 订阅；
- API 账户；
- provider 访问权限。

可以使用 MCP 工具 `harness_list` 和 `harness_status` 检查当前机器上实际可用的 integration。

MCP 服务端还暴露 `harness_telemetry`，用于统一、权威地检查安装状态、进程活动与 AGY 模型列表。仅在存在权威 provider 来源时显示配额；否则报告不可用，绝不擅自猜测。Lighthouse 直接在界面中消费该权威遥测数据。

---

## 快速开始

Windows 入口使用 **Windows PowerShell**。当前 1.1.0 更新已通过 Windows Python 3.11/3.12 CI；真机验收仍待完成。

### 1. 克隆仓库

```powershell
git clone https://github.com/JL066/harness-harbor.git
cd harness-harbor
```

### 2. 创建虚拟环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 4. 配置本地 coding harness

安装并登录你准备使用的 coding CLI。

Harbor 可以使用已经位于 `PATH` 中的 executable，也可以使用 [.env.example](.env.example) 中描述的对应 `HARBOR_*` 环境变量。

Harbor 不会自动加载 `.env.example`。

### 5. 启动完整 MCP 服务端

```powershell
python server_legacy.py
```

`server_legacy.py` 当前是完整 MCP server。

这个文件名为了源码兼容而保留。

`server.py` 是体积更小的 Codex-only compatibility server。

### 6. 可选：启动 job daemon

如需后台分发队列任务，在另一个 PowerShell 窗口运行：

```powershell
.\start-codex-job-daemon.ps1
```

除非通过 `HARBOR_PYTHON` 指定其他解释器，否则 launcher 使用 `python`。

### 7. 可选：启动 Lighthouse 图形界面或打包独立可执行文件

Harness Harbor 包含 **Lighthouse**，一个独立的 Windows 图形化启动器，提供设置/向导界面、服务生命周期管理、系统托盘、日志查看、诊断以及后台健康监测。

使用 Python 启动 Lighthouse：

```powershell
python run_launcher.py
```

或使用 PowerShell 启动脚本：

```powershell
.\run-launcher.ps1
```

如需将 Lighthouse 打包为独立的 Windows 可执行文件：

```powershell
python build_exe.py
```

打包使用 `harbor_launcher.spec`，并捆绑来自 `launcher/assets/` 的品牌图标资源。

---

## Core 与 Lighthouse Launcher

此 Public RC 包含 Python Core 与 Lighthouse（独立 Windows 图形启动器）。Lighthouse 提供设置/向导界面、安全的 CredentialStore 集成、托管隧道配置与生命周期、启动/停止/重启控制、系统托盘、日志查看、诊断以及后台健康监测。当前本地开发版本为 `1.1.0`；可通过 `run_launcher.py` 或 `run-launcher.ps1` 启动，并可通过 `build_exe.py` 或 `harbor_launcher.spec` 打包。Harbor/Lighthouse 图标及运行时打包资源位于 `launcher/assets/`。

向导仅持久化非敏感配置。隧道运行时密钥和自定义 provider 密钥存储于 Windows Credential Manager，仅传递给相关子进程环境变量；严禁明文回退。托管隧道设置采用操作者提供的通用 HTTPS 控制配置与本地路径，不捆绑私有端点或账户绑定。

Lighthouse 消费 Core 对 Codex、MiniMax 和 Antigravity/AGY 的权威遥测数据。它显示已验证的安装/能力状态、活动状态与 AGY 模型列表。仅在存在权威 provider 来源时显示配额；否则报告不可用，绝不擅自猜测。

---

## 将 ChatGPT 连接到 Harbor

运行在私有开发机上的 Harbor MCP server 通常无法被 ChatGPT 直接访问。

使用 ChatGPT 作为 Supervisor 时，推荐的连接架构是：

```text
ChatGPT Chat
     │
     │ MCP
     ▼
OpenAI Secure MCP Tunnel
     │
     ▼
Harness Harbor
     │
     ▼
本地 coding harnesses
```

OpenAI Secure MCP Tunnel 可以让受支持的 OpenAI 环境连接到开发机或私有网络中的 MCP server，而不需要把这个 MCP server 直接公开暴露到互联网。

当 Harbor MCP integration 在 ChatGPT 环境中可用之后，对话就可以像调用其他被允许的 MCP integration 一样调用 Harbor tools。

Tunnel client、OpenAI 侧 MCP 配置、认证和权限均位于本仓库之外。

Harbor 不捆绑 tunnel 凭据，也不包含 OpenAI 账户配置。

对于使用托管隧道的环境，Lighthouse 直接在启动器界面中提供托管隧道 profile 配置与生命周期控制，运行时凭据安全存储于 Windows Credential Manager 中，杜绝明文回退。所有托管隧道设置均采用操作者提供的通用 HTTPS 控制端点与本地路径，不捆绑私有端点或账户绑定。

随附的 `start-tunnel.ps1` 是面向无头或命令行环境的可选便利工具。它在通过环境变量提供可执行文件和 profile 设置之前会拒绝启动，且不包含任何捆绑的可执行文件、profile 或凭据。

如果上游 MCP client 本身已经能够直接访问 Harbor，或者 Harbor 通过合适 transport 在本地使用，则不需要 tunnel。

---

## 一个典型的 Harbor job

上游 Supervisor 可以先检查可用 worker：

```text
harness_list()
```

或者查询某个 integration：

```text
harness_status(...)
```

随后可以启动一个聚焦阶段：

```text
task_start(...)
```

Harbor 返回：

```text
job_id
```

Supervisor 保存这个标识符。

它 **不需要阻塞整个对话等待任务完成**。

之后可以再次检查同一个 job：

```text
task_poll(job_id)
```

然后由 Supervisor 做真正重要的决定：

```text
result
  │
  ├── accepted
  │      │
  │      ▼
  │   next stage
  │
  ├── rejected
  │      │
  │      ▼
  │   corrective task
  │
  └── project complete
```

**执行** 与 **验收** 的分离，是 Harbor 预期工作流中的核心原则。

Worker 负责写代码。

Supervisor 决定代码是否达到要求。

---

## 两层路由

Harbor 把路由拆成两个独立决策。

### 第一层：选择 harness

Supervisor 选择：

```text
codex
agy
minimax
```

### 第二层：选择 Codex route

当选中的 harness 是 Codex 时，Supervisor 还可以进一步选择：

```text
current
official
custom
official_then_custom
```

MiniMax 和 AGY 当前只接受 `current`。

### `current`

使用用户当前的 Codex CLI 配置，不应用 Harbor provider override。

### `official`

添加进程本地 Codex override：

```text
-c model_provider="openai"
```

### `custom`

使用名为 `harbor_custom` 的通用进程本地 provider，并采用 Responses API wire mode。

通过以下环境变量配置：

```text
HARBOR_CODEX_CUSTOM_BASE_URL
HARBOR_CODEX_CUSTOM_API_KEY
```

可选：

```text
HARBOR_CODEX_CUSTOM_MODEL
```

Harbor 只通过 provider 的 `env_key` 把 API key 放入复制后的子进程环境。

它不会把 key 写入用户的 Codex 配置，也不会把 key 放入命令行参数。

自定义路由是可配置的通用兼容 OpenAI provider。此 Public RC 不包含个人路由、私有 provider、账户绑定或特定 provider 的回退配置。

### `official_then_custom`

该 route 首先尝试 subscription-backed 的 official provider。

只有当第一次尝试明确识别为 OpenAI/Codex 订阅或 usage quota 耗尽时，才会尝试 custom route。

它不会因为以下情况自动 fallback：

- 认证失败；
- 普通 rate limit；
- 泛化 HTTP 429；
- 未知失败；
- coding failure；
- task failure；
- prompt failure；
- parser failure；
- test failure。

Harbor 只记录请求 route、实际使用 route、脱敏后的 classification 信息以及有边界的 attempt summary。

结果是否可接受，仍然由 Supervisor 决定。

---

## 安全 Git 交付

完整 MCP 服务端暴露受限的 `git_ls_remote`、`git_push_dry_run` 和 `git_push_ref` 工具，用于安全的分支交付：

- 仅接受已配置的远程名称；
- 要求无凭据的 HTTPS push URL 和精确的 `refs/heads/*` 分支引用；
- 拒绝 tag、分支删除、强制推送（force push）和任意 refspec；
- 在推送前校验预期的远程 HEAD；
- 执行单次非强制的快进更新，并校验最终的远程分支引用；
- 流式传输受限且脱敏机密信息的 Git 输出，不提供通用 shell 或凭据注入 API。

---

## 本地执行模型

运行时 job 记录位于：

```text
.jobs/
```

路由状态和 project alias 位于：

```text
.control/
```

两者都属于本地 runtime state，并已被 Git 忽略。

可选的 `codex_job_daemon.py` 负责在本地分发排队的 job。

它属于执行层。

它不是：

- reasoning service；
- project Supervisor；
- scheduler；
- acceptance loop。

Harbor 的本地执行路径可以概括为：

```text
Probe
  ↓
Route
  ↓
Queue
  ↓
Lease
  ↓
Run
  ↓
Track
  ↓
Recover
```

---

## 安全

Harness Harbor 可以让功能强大的 coding-agent CLI 对真实本地仓库执行操作。

因此，请把 Harbor MCP endpoint 当作高权限本地执行入口来管理。

只向你信任的客户端和网络暴露 Harbor。

不要提交：

- `.env` 文件；
- API key；
- CLI 凭据；
- tunnel 凭据；
- tunnel profile；
- `.jobs/`；
- `.control/`；
- 本地日志；
- 含有秘密信息的 provider 配置；
- 诊断输出、转储、报告或与单台机器绑定的诊断产物。

AGY 的 `--dangerously-skip-permissions` 默认绝不会自动启用。

只有在你明确需要这种行为并已经阅读 [SECURITY.md](SECURITY.md) 后，才设置：

```text
HARBOR_AGY_DANGEROUSLY_SKIP_PERMISSIONS=1
```

设置向导仅持久化非敏感配置。隧道运行时密钥和自定义 provider 密钥安全存储于 Windows Credential Manager 中，且仅注入到相关子进程环境中；严禁明文回退。托管隧道设置采用操作者提供的通用 HTTPS 控制配置与本地路径，不捆绑私有端点或账户绑定。

在把 Harbor 连接到重要 worktree 或远程暴露 MCP endpoint 之前，请阅读 [SECURITY.md](SECURITY.md)。

---

## 开发与测试

运行隔离的单元测试：

```powershell
python -m unittest discover -s tests -v
```

测试使用临时目录和 mocked subprocess behavior，不要求启动 Harbor、tunnel、daemon 或真实 coding-agent CLI。

GitHub Actions 已配置 Windows/macOS、Python 3.11/3.12 Core 测试以及 macOS Swift 与打包检查；工作流配置不代表远端 CI 已运行通过。

---

## 平台支持

| 平台 | 状态 |
| --- | --- |
| Windows | 1.1.0 CI 和 ZIP 构建已通过；真机验收未完成；暂不公开发布安装包 |
| macOS | Apple Silicon DMG 预览版；未公证；Intel／正式发布验收未完成 |
| Linux | 目前未验证 |

当前实现以 Windows 为首要平台，包括其 PowerShell 启动脚本、Windows Credential Manager 集成、Lighthouse 图形界面以及进程行为。

macOS 原生 App、构建步骤和公开发布验收条件见 [MACOS.md](MACOS.md)。

---

## 项目边界

Harness Harbor 有意保持为一个相对小型的执行 broker。

它不试图接管整个 AI 开发工作流。

上游 Supervisor 可以是：

- 一个普通 ChatGPT 对话；
- 另一个支持 MCP 的 AI assistant；
- 自定义 agent；
- 内部自动化服务；
- 任何实现了自身 reasoning 与 acceptance loop 的应用。

Harbor 始终位于这一层之下。

它的职责可以概括为：

> **让 Supervisor 能够通过一个持久、可靠的本地执行层操作 coding workers，而不必让这些 workers 自己变成 Supervisor。**

---

## 第三方服务与商标

Harness Harbor 是一个独立第三方项目。

它不隶属于、不代表，也未获得 OpenAI、Codex、MiniMax、Antigravity / AGY 提供方或任何 tunnel 服务提供方的认可、赞助或背书。

文中出现的第三方产品名称仅用于说明互操作关系。

---

## License

本项目采用 Apache License 2.0。

详见 [LICENSE](LICENSE)。
