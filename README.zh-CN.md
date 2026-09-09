[English](README.md) | [简体中文](README.zh-CN.md)

# Harness Harbor

*面向 AI 编程 harness 的轻量级本地代理层。*

Harness Harbor 将本地安装的编程 harness 组成可靠的工作池，供上游
Supervisor/Agent 使用。本公开版本以 MCP 作为主要集成接口：上游系统
提交工作，Harbor 提供一个小而基于磁盘的执行层，负责
**Probe → Route → Lease → Run → Track → Recover**。Supervisor 仍然是大脑：
它负责推理、规划、验收、重试策略，以及定时或事件驱动的唤醒。

> **当前状态：Windows-first / validated on Windows（以 Windows 为首要平台，
> 已在 Windows 上验证）。** 本公开版本已针对 Windows 实现并测试。
> **macOS：Not yet supported / planned（尚不支持 / 计划中）。** 本版本尚未
> 实现或验证 macOS 支持。

## Why Harness Harbor?

不同编程 harness 往往有各自独立的认证方式、模型目录、配额与可用性信号，
以及不同的进程行为。Supervisor 不应为每个本地 CLI 编写一套专用胶水代码。
Harbor 为能够调用 MCP 的上游系统提供一个收窄且统一的控制面，用于发现
harness、启动工作、轮询结果以及处理本地执行状态。

## How it fits

```text
Supervisor / Agent（推理、规划、验收、唤醒）
                         |
                        MCP
                         v
Harness Harbor（Probe · Route · Lease · Run · Track · Recover）
                         |
                         v
                 Codex / AGY / MiniMax
```

Harbor 刻意保持小巧且相对“愚笨”。它不判断任务含义，也不判断结果是否可
接受。任何能够调用 MCP 的上游系统都可以使用当前版本。对于长时间运行的
工作流，如果上游系统同时具备调度器、未来唤醒机制、cron/事件循环或等效
能力，使用体验尤其合适。Harbor 自身不提供这种唤醒能力。

## What Harbor does

- **Probe**：探测已安装的 harness 集成及其经过验证的能力。
- **Route**：将调用方选择的工作路由到当前本地项目/工作区；对 Codex，
  还可以选择第二层通用执行路由。Harbor 不执行智能自动路由，两层都由
  Supervisor 选择。
- **Lease**：按 harness 并发限制领取队列中的工作，并独占工作区租约。
- **Run**：以受监管的子进程 I/O 运行有边界的本地 CLI 任务。
- **Track**：将任务状态与结果持久化到 `.jobs/` 下的磁盘记录，供轮询和
  诊断使用。
- **Recover**：在 worker 或 daemon 故障/重启后回收过期的预留和工作区租约。

完整 MCP 服务端是 `server_legacy.py`；文件名为保持源代码兼容而保留。
`server.py` 是更小的、仅支持 Codex 的兼容服务端。可选的
`codex_job_daemon.py` 负责在本地分发队列中的任务；它是执行组件，不是
上游的推理服务或唤醒服务。

## Two-layer routing

Supervisor 在 `task_start` 中控制两个相互独立的选择：

1. **Harness 选择**：选择 `codex`、`minimax` 或 `agy`。
2. **Codex 路由选择**：当 harness 为 Codex 时选择 `current`、`official`、
   `custom` 或 `official_then_custom`。MiniMax 和 AGY 只接受 `current`。

`current` 使用用户当前的 Codex CLI 默认配置，不添加 Harbor 路由覆盖。
`official` 添加进程本地 Codex 覆盖 `-c model_provider="openai"`。
`custom` 添加名为 `harbor_custom` 的通用进程本地 provider，并使用 Responses
API wire mode。请设置 `HARBOR_CODEX_CUSTOM_BASE_URL` 和
`HARBOR_CODEX_CUSTOM_API_KEY`；`HARBOR_CODEX_CUSTOM_MODEL` 可选。Harbor 只将
密钥通过 provider 的 `env_key` 放入复制的子进程环境，绝不写入用户 Codex
配置，也绝不将密钥放入 argv。

例如，Supervisor 可以请求 `route="official_then_custom"`。Harbor 首先
尝试订阅路线的 `official`；只有当首次尝试明确识别为 OpenAI/Codex 订阅或
用量配额耗尽时，才尝试通用 `custom`。认证错误、普通限流、429、未知错误，
以及任务、代码、提示词、解析和测试失败都不会触发回退。job 记录只保存
请求/实际路由、经过清理的分类和有界的尝试摘要。

## What Harbor deliberately does not do

- 它不是 agent framework、模型运行时，也不是 Supervisor 的大脑。
- 它不提供推理、规划、结果验收、记忆、人格或通用编排。
- 它不负责定时/事件驱动的唤醒，也不负责制定重试策略。
- 它不宣称支持所有 harness；集成范围仅限于本版本代码中已验证的实现。

## Example: ChatGPT as a Supervisor

ChatGPT 只是一个具体的集成示例，并非依赖项或推荐背书。一个支持 MCP 的
ChatGPT 工作流可以是：

1. ChatGPT 对请求进行推理并规划一个阶段。
2. 通过 MCP 调用 Harbor 的 `task_start`，指定 harness 和项目/工作区。
3. 保存返回的 `job_id`。
4. ChatGPT 利用自身的定时唤醒能力稍后回访任务，并在适当时调用
   `task_poll`。Harbor 不负责安排这次唤醒。
5. ChatGPT 检查结果并决定该阶段是否通过验收。
6. 通过 MCP 分发下一个任务或后续阶段。

同一个执行层也可以由支持 MCP 的 Supervisor/Agent 使用，例如自定义 agent
或其他兼容的本地工作流。在此提及 ChatGPT 不代表与 OpenAI 存在任何关联，
也不代表 OpenAI 对本项目的认可。

## Supported platform

**Windows-first / validated on Windows（以 Windows 为首要平台，已在 Windows
上验证）。** 当前 release candidate 已针对 Windows 实现并测试，包括进程
行为和 PowerShell 启动器。**macOS：Not yet supported / planned（尚不支持 /
计划中）**，尚未实现或验证。本版本暂不提供 macOS 安装说明。

## Supported harnesses

当前代码中的集成包括：

- **Codex**
- **MiniMax**
- **Antigravity / AGY**

每个 CLI 及其凭据/配置都必须在本地安装和配置。使用 MCP 的
`harness_list` 或 `harness_status` 工具，可以检查本机的实际可用性和已验证
能力。这些是当前集成，并不代表通用覆盖范围，也不代表与第三方存在关联。
Harbor 不捆绑 CLI、模型、凭据或账户访问权限。

## Setup

此 release candidate 面向 Windows PowerShell，并已使用 Python 3.11 和
3.12 验证。

1. 创建并激活虚拟环境：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. 安装运行时依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. 将要使用的 CLI 放入 `PATH`，或在启动 Harbor 的进程中设置
   [.env.example](.env.example) 中对应的 `HARBOR_*` 变量。Harbor 不会自动
   加载 `.env.example`。AGY 的 `--dangerously-skip-permissions` 默认不会
   添加；只有在阅读 [SECURITY.md](SECURITY.md) 并评估风险后，才设置
   `HARBOR_AGY_DANGEROUSLY_SKIP_PERMISSIONS=1`。

4. 启动完整 MCP 服务端：

   ```powershell
   python server_legacy.py
   ```

5. 如需在后台分发队列任务，请在单独的 PowerShell 窗口中运行可选 daemon：

   ```powershell
   .\start-codex-job-daemon.ps1
   ```

   除非由 `HARBOR_PYTHON` 指定其他解释器，否则它使用 `python`。

## Optional tunnel integration

只有在希望将远程 MCP 客户端连接到 Harbor 本地服务端时，才需要使用 tunnel。
本地 stdio 使用和单元测试都不需要 tunnel。外部 tunnel 客户端、传输方式、
配置文件和凭据不会随项目提供；请按照适用服务提供方的说明另行获取和配置。

项目内的 `start-tunnel.ps1` 仅是便利工具。只有通过环境变量提供可执行文件
和配置设置后，它才会启动；项目不包含任何可执行文件、配置文件或凭据。

## Development and testing

无需启动 Harbor、tunnel、daemon 或真实编程 agent CLI，即可运行隔离的单元测试：

```powershell
python -m unittest discover -s tests -v
```

测试使用临时目录，并对运行时状态使用模拟的子进程。GitHub Actions 也会在
Windows 的 Python 3.11 和 3.12 上运行同一套测试。

## Local data and security

运行时记录位于 `.jobs/`；路由状态和项目别名位于 `.control/`。两者都是本地
状态，并已被 Git 忽略。不要提交 `.env`、运行时目录、日志、CLI 配置文件、
tunnel 配置、凭据或诊断脚本。Harbor 可以代表 MCP 客户端调用本地 agent CLI
并执行文件系统/Git 操作，因此只应向你信任的客户端和网络暴露它。在连接
真实工作区前，请阅读 [SECURITY.md](SECURITY.md)。

Harbor 是独立的第三方项目，不隶属于 OpenAI、Codex/MiniMax/Antigravity 的
提供方，也不隶属于或得到任何 tunnel 提供方的认可或赞助。

## License

本项目采用 Apache License 2.0。详见 [LICENSE](LICENSE)。
