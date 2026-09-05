# AQG Work / Code 客户端支持能力矩阵

基线：`origin/feature/v0.0.1`。本矩阵对应 `scripts/aqg_client_registry.py` 与
`scripts/install_aqg_work_clients.py` 中的已落地支持合同。

| 客户端 | client_id | support_level | AQG 安装面 | 降级 / 不支持 | 官方证据 |
|---|---|---|---|---|---|
| WorkBuddy | `workbuddy` | 部分支持(partial) | AQG skills、规则包、支持报告 | 未发现可托管的本地生命周期 hooks / blocking schema；MCP/connector 仅作为产品能力，不写入 AQG MCP 配置 | [WorkBuddy docs](https://www.workbuddy.ai/docs)、[agents](https://www.workbuddy.ai/agents) |
| CodeBuddy | `codebuddy` | 完整支持(full) | AQG skills、rules、`mcp.json`、`settings.json` lifecycle hooks、verify/uninstall/is-installed | IDE 端仍取决于宿主实际加载 hook 配置；AQG 只验证落盘合同 | [hooks](https://www.codebuddy.ai/docs/cli/hooks)、[skills](https://www.codebuddy.ai/docs/cli/skills)、[MCP](https://www.codebuddy.ai/docs/cli/mcp) |
| Qoder Desktop | `qoder` | 部分支持(partial) | AQG skills、hooks、项目 rules，写入 `~/.qoder` | 接受 `Qoder.app` / `com.qoder.app` 与 `Qoder IDE.app` / `com.qoder.ide` 两个精确产品身份；多个 alias 只选择一次；无 `SessionStart` / `PreCompact` / WIP save-recover | [Qoder docs](https://docs.qoder.com/) |
| Qoder CN Desktop | `qoder-cn` | 部分支持(partial) | AQG skills、hooks、项目 rules，写入 `~/.qoder-cn` | 接受 `Qoder CN.app` / `com.qodercn.app` 与 `Qoder CN IDE.app` / `com.aliyun.lingma.ide` 两个精确产品身份；多个 alias 只选择一次；`~/.lingma` 是历史通义灵码根，不是本 profile 的安装目标；无 `SessionStart` / `PreCompact` / WIP save-recover | [Qoder CN docs](https://docs.qoder.cn/) |
| Kimi Work / Kimi Desktop | `kimi-work` | 部分支持(partial) | AQG skills 安装到 Kimi Work Desktop / Kimi Desktop 本地 Daimon `daimon-share/daimon/skills`，并写支持报告；verify/uninstall/is-installed | 只托管 `aqg-*` skills；rules/MCP/hooks/blocking hook gate 未发现可靠公开合同。Daimon shareDir 会随安装盘 rehome，因此通过 `--skills-root`、`KIMI_WORK_SKILLS_ROOT`、`KIMI_DESKTOP_SKILLS_ROOT`、运行进程、日志、`.rehomed` marker、注册表/快捷方式动态发现；不新增 `kimi-desktop` client_id。公开 Kimi 文档主要覆盖 Kimi Code CLI，此 profile 的 Daimon root 证据来自本地安装包/日志/进程。 | [Kimi](https://kimi.moonshot.cn/)；local Daimon evidence |
| Kimi Code | `kimi-code` | 部分支持(partial) | AQG skills、rules、MCP、`config.toml` hooks、verify/uninstall/is-installed | Kimi Code 官方说明 hook 失败/超时为 fail-open，不能作为唯一阻塞安全门 | [hooks](https://moonshotai.github.io/kimi-code/en/customization/hooks)、[skills](https://moonshotai.github.io/kimi-code/en/customization/skills.html)、[MCP](https://moonshotai.github.io/kimi-code/en/customization/mcp.html) |
| QoderWork | `qoderwork` | 部分支持(partial) | AQG skills、rules、MCP、`settings.json` hook 子集、支持报告 | 不复用 `qoder-cli` 的完整 lifecycle；QoderWork 可托管 `PreToolUse` / `PostToolUse` / `Stop` / `UserPromptSubmit`，但未证明 `SessionStart` / `PreCompact` / WIP save-recover parity | [introduction](https://docs.qoder.com/qoderwork/introduction)、[connectors](https://docs.qoder.com/qoderwork/connectors)、[hooks](https://docs.qoder.com/qoderwork/hooks) |
| QoderWake | `qoderwake` | 部分支持(partial) | AQG skills、rules、MCP、支持报告 | Waker 自动化/权限能力可承载提醒，但无本地 blocking lifecycle hooks 合同 | [overview](https://docs.qoder.com/qoderwake/overview)、[skills and integrations](https://docs.qoder.com/qoderwake/skills-and-integrations)、[manage wakers](https://docs.qoder.com/qoderwake/manage-wakers) |
| Pi | `pi` | 部分支持(partial) | AQG skills、托管 TypeScript extension hooks、支持报告、verify/uninstall/is-installed | MCP/connectors 不支持；closeout/WIP save 依赖 Pi `session_before_compact` / `session_shutdown` 投递，host runtime discovery/trust 仍需用户确认 | [extensions events](https://pi.dev/docs/latest/extensions#events)、[skills](https://pi.dev/docs/latest/skills)、[docs latest](https://pi.dev/docs/latest) |

## AQG 功能举证

| 功能 | WorkBuddy | CodeBuddy | Trae Work | Kimi Work | Kimi Code | QoderWork | QoderWake | Pi |
|---|---|---|---|---|---|---|---|---|
| AQG skills 可发现性 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 |
| 持久 rules / instructions | 支持 | 支持 | 支持 | 不支持 | 支持 | 支持 | 支持 | 部分支持 |
| MCP / connector 配置 | 不支持 | 支持 | 支持 | 不支持 | 支持 | 支持 | 支持 | 不支持 |
| 会话启动 preflight | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 不支持 | 不支持 | 支持 |
| WIP checkpoint recover | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 不支持 | 不支持 | 支持 |
| PreToolUse shell skill-validator | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 支持 | 不支持 | 支持 |
| PreToolUse secret scan | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 支持 | 不支持 | 支持 |
| PostToolUse debugging reminder | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 不支持 | 不支持 | 支持 |
| PostToolUse code-construction reminder | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 支持 | 不支持 | 支持 |
| PostToolUse test-quality reminder | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 支持 | 不支持 | 支持 |
| PostToolUse security-review reminder | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 支持 | 不支持 | 支持 |
| AQG skill-edit validator reminder | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 支持 | 不支持 | 支持 |
| PreCompact / Stop closeout reminder | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 部分支持 | 不支持 | 部分支持 |
| WIP checkpoint save | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 不支持 | 不支持 | 部分支持 |
| 用户提示词 handoff mandate / routing | 不支持 | 支持 | 不支持 | 不支持 | 部分支持 | 支持 | 不支持 | 支持 |
| trust / enable / disable flow | 未知 | 支持 | 未知 | 未知 | 部分支持 | 未知 | 未知 | 部分支持 |
| verify / uninstall / is-installed | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 | 支持 |

说明：`部分支持` 表示 AQG 能落盘并验证该能力，但存在宿主语义降级，例如 Kimi Code hooks fail-open；`不支持`
表示官方文档未给出可安全实现的生命周期 hook 或托管配置合同。
