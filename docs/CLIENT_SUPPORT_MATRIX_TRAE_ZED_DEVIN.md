# AQG 客户端能力矩阵：Trae / Zed / Devin

评估日期：2026-07-28

开发基线：`origin/feature/v0.0.1`

## 总览

| 客户端 | `client_id` | Registry 状态 | 已实现托管面 | 降级 / 不支持 |
|---|---|---|---|---|
| Trae | `trae` | `partial` | skills、项目 `AGENTS.md` rules、TRAE hooks、verify / uninstall / is-installed | 无 `PreCompact` 官方证据；User Rules 仍由 UI 管理。 |
| Trae CN | `trae-cn` | `partial` | skills、项目 `AGENTS.md` rules、TRAE CN hooks、verify / uninstall / is-installed | 无 `PreCompact` 官方证据；User Rules 仍由 UI 管理。 |
| Trae Work | `trae-work` | `partial` | skills（共享 `~/.trae/skills`）、项目 `AGENTS.md` rules、verify / uninstall / is-installed | Work lifecycle hook schema 未验证；hooks / blocking gates 不安装。 |
| Trae Work CN | `trae-work-cn` | `partial` | skills（共享 `~/.trae-cn/skills`）、项目 `AGENTS.md` rules、verify / uninstall / is-installed | Work lifecycle hook schema 未验证；hooks / blocking gates 不安装。 |

## 产品身份与共享 skills 根

四个 Trae 产品全部由 `scripts/install_aqg_agent_clients.py` 管理（`trae-work` 已从 work-client adapter 退休）。自动检测使用 macOS 产品身份，不使用配置目录：

| `client_id` | 产品 / bundle id | 用户 skills 根 | hooks |
|---|---|---|---|
| `trae` | `Trae.app` / `com.trae.app` | `~/.trae/skills` | TRAE hooks（无 `PreCompact`） |
| `trae-work` | `TRAE SOLO.app` / `com.trae.solo.app` | `~/.trae/skills`（与 `trae` 共享） | 不安装 |
| `trae-cn` | `Trae CN.app` / `cn.trae.app` | `~/.trae-cn/skills` | TRAE CN hooks（无 `PreCompact`） |
| `trae-work-cn` | `TRAE SOLO CN.app` / `cn.trae.solo.app` | `~/.trae-cn/skills`（与 `trae-cn` 共享） | 不安装 |

共享根由一个所有权账本记录当前持有该根的 profile。两种安装顺序都幂等；只卸载其中一个 profile 时，另一个仍持有该根，16 个 AQG skills 与用户自有 skill 目录都保留，只有最后一个持有者卸载时才移除。
| Zed | `zed` | `partial` | skills、`AGENTS.md` instructions、verify / uninstall / is-installed | 未找到官方 lifecycle hooks；所有 AQG hook gates 不安装。 |
| Devin | `devin` | `partial` | skills、`AGENTS.md` rules、Devin CLI hooks、verify / uninstall / is-installed | 无 `PreCompact` 前置事件；`PostCompaction` 只能提醒，不能保存压缩前 WIP。 |

## 官方证据

| 客户端 | 官方文档 URL | 能力事实 |
|---|---|---|
| Trae / Trae CN | https://docs.trae.cn/ide_skills | IDE Skills 支持持久 skill 目录。 |
| Trae / Trae CN | https://docs.trae.cn/ide_rules | IDE Rules 支持项目规则；用户规则由产品 UI 管理。 |
| Trae / Trae CN | https://docs.trae.cn/ide_add-mcp-servers | IDE 支持 MCP server 配置。 |
| Trae / Trae CN | https://docs.trae.cn/ide_hook-configuration-reference | IDE Hook schema 支持 `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop` 等事件和工具调用拦截。 |
| Trae Work / Trae Work CN | https://docs.trae.cn/work_what-is-trae-solo | Work 是云端/本地任务型产品面，不等同于 IDE hook runtime。 |
| Trae Work / Trae Work CN | https://docs.trae.cn/work_skills | Work Skills 支持可复用技能。 |
| Zed | https://zed.dev/docs/ai/skills | Zed Agent 支持 skills。 |
| Zed | https://zed.dev/docs/ai/instructions | Zed 支持 `AGENTS.md` instructions。 |
| Zed | https://zed.dev/docs/ai/mcp | Zed 支持 MCP servers。 |
| Devin | https://docs.devin.ai/cli/extensibility/rules | Devin CLI 支持 `AGENTS.md` rules。 |
| Devin | https://docs.devin.ai/cli/extensibility/skills/overview | Devin CLI 支持 skills。 |
| Devin | https://docs.devin.ai/cli/extensibility/mcp/overview | Devin CLI 支持 MCP。 |
| Devin | https://docs.devin.ai/cli/extensibility/hooks/lifecycle-hooks | Devin CLI 支持 lifecycle hooks，包括 `PreToolUse`、`PostToolUse`、`UserPromptSubmit`、`Stop`、`SessionStart`、`PostCompaction`。 |

## AQG 功能举证

| 功能 | Trae / Trae CN | Trae Work / Work CN | Zed | Devin | 说明 |
|---|---|---|---|---|---|
| AQG skills 可发现性 | 支持 | 支持 | 支持 | 支持 | `scripts/install_aqg_agent_clients.py` 安装 16 个 `skills/aqg-*`。 |
| 持久 rules / instructions | 部分支持 | 部分支持 | 支持 | 支持 | Trae 用户规则 UI-managed；项目 scope 写 `AGENTS.md`。 |
| MCP / connector 配置 | 部分支持 | 部分支持 | 部分支持 | 部分支持 | 客户端官方支持 MCP；AQG 本轮不写 MCP server 配置。 |
| 会话启动 preflight | 支持 | 不支持 | 不支持 | 支持 | Trae IDE/Devin hooks 有 `SessionStart`；Work/Zed 无已验证 hook。 |
| WIP checkpoint recover | 支持 | 不支持 | 不支持 | 支持 | 绑定 `SessionStart`。 |
| PreToolUse shell skill-validator | 支持 | 不支持 | 不支持 | 支持 | Trae IDE/Devin hook profile 安装 blocking PreToolUse。 |
| PreToolUse shell / file secret scan | 支持 | 不支持 | 不支持 | 支持 | Trae IDE/Devin hook profile 安装 blocking secret scan。 |
| PostToolUse shell 失败 debugging reminder | 支持 | 不支持 | 不支持 | 支持 | 通过 hook adapter 复用 AQG hook policy。 |
| PostToolUse code-construction reminder | 支持 | 不支持 | 不支持 | 支持 | 同上。 |
| PostToolUse test-quality reminder | 支持 | 不支持 | 不支持 | 支持 | 同上。 |
| PostToolUse security-review reminder | 支持 | 不支持 | 不支持 | 支持 | 同上。 |
| AQG skill-edit validator reminder | 支持 | 不支持 | 不支持 | 支持 | 同上。 |
| PreCompact / Stop closeout reminder | 部分支持 | 不支持 | 不支持 | 部分支持 | Trae/Devin 支持 `Stop`；无 PreCompact-before-compaction。 |
| WIP checkpoint save | 部分支持 | 不支持 | 不支持 | 部分支持 | Stop-only save；Devin `PostCompaction` 不能替代压缩前保存。 |
| 用户提示词 handoff mandate / routing | 支持 | 不支持 | 不支持 | 支持 | 绑定 `UserPromptSubmit`。 |
| 用户 trust / enable / disable 流程 | 部分支持 | 不支持 | 部分支持 | 支持 | Installer 可 uninstall；实际客户端 enable/trust 仍按各官方产品流程。 |
| verify / uninstall / is-installed | 支持 | 支持 | 支持 | 支持 | `scripts/install_aqg_agent_clients.py` 提供四动作合同。 |
