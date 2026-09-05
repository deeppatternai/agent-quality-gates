# 推荐互补工具栈

[English](ECOSYSTEM_MUST_INSTALL.md) | 中文

> Scope: 所有 AQG 支持的宿主 agent（见 README 的支持客户端矩阵）
>
> Agent Quality Gates (AQG) 是工程纪律层，不是完整工具链。本文档列出与 AQG **互补** 得好的工具 — 确定性检查、生产反馈、PR/CI 级证据链。原则是 **少而硬**：不追求装最多 skill / plugin，把质量能力固定进施工路径（动手前约束 → 实现中验证 → PR/CI/生产反馈闭环）。按工作流需要选装。

---

## 目录

- [背景与原则](#背景与原则)
- [互补工具（跨 agent）](#互补工具跨-agent)
- [互补工具（Claude Code）](#互补工具claude-code)
- [互补工具（Codex）](#互补工具codex)
- [Harness features（无需安装但要会用）](#harness-features无需安装但要会用)
- [值得评估（要试再决定）](#值得评估要试再决定)
- [明确不推荐](#明确不推荐)
- [参考源（借鉴不引入）](#参考源借鉴不引入)

---

## 背景与原则

### 选型原则

1. **不堆数量**：能力空缺驱动，不是 star 数驱动
2. **不和 AQG 重复**：这些工具是互补而非替代
3. **不引入跟 AQG 冲突的整套方法**（superpowers / spec-kit / BMAD 都是参考不是替代）
4. **优先级**：安全 + 可复现验证 > 生产反馈 > 协作/知识流
5. **跨 agent 一致性**：一个项要么在支持的宿主 agent 上普遍受益（跨 agent），要么明确标出它仅限哪个宿主

### AQG 刻意留给互补工具的部分

AQG 提供纪律 gate（preflight、code construction、systematic debugging、audit adjudication、evidence closeout、skill validation），但刻意不自带：

- **deterministic 检查**（lint / SAST / 类型）— LLM audit 会漏低层规则
- **PR / CI 级证据链** — 本地 closeout ledger 止步于本机
- **生产异常信号**（Actions raw logs / error tracking）— debugging 时常常只看到状态拿不到 logs

下面的工具补这些缺口。

---

## 互补工具（跨 agent）

> 在支持的宿主 agent 上普遍受益。

### 1. Sentry（生产异常 → 反馈 debugging）

**价值**：把真实生产错误拉回 `aqg-systematic-debugging` 的输入端，避免只靠本地测试判断质量。Sentry 远程 MCP 自带 Seer AI 根因分析，作为一个**独立信号源**。

**Claude Code 侧**：
```bash
claude mcp add --transport http sentry https://mcp.sentry.dev/mcp
# 首次连接走 OAuth，无需手配 token
```

**Codex 侧**：装 Sentry plugin（OpenAI marketplace）。

**何时启用**：任意 service 跑到 staging / prod 时启用；纯本地脚本阶段可暂缓。

**风险**：远程 MCP 需要外网访问；self-host Sentry 需要换 sentry-mcp-stdio。

---

### 2. 安全三层（threat model + SAST + PR 级 review）

一个通用 LLM security-review skill（会话内手动过一遍）是好起点，但持久做法是补三层：

**Layer A — 写代码前（threat model 思考）**：Codex 侧装 OpenAI 官方安全三件套
```
codex-security 或单独安装：
  - security-threat-model
  - security-best-practices
  - security-ownership-map
```

**Layer B — 写代码中（确定性 SAST）**：Claude Code 侧装 semgrep
```bash
/plugin install semgrep@claude-plugins-official
```
作用：扫 OWASP / 注入 / hardcoded secrets。**和 LLM audit 互补不重叠** — semgrep 是规则确定性，LLM audit 是语义。closeout 之前两段式跑。**免注册**：semgrep 纯本地对本地 / OSS 规则集跑（`semgrep --config p/owasp-top-ten`）；`SEMGREP_APP_TOKEN` 仅用于可选的 Semgrep 托管平台。

**Layer C — PR 级（CI 自动门禁）**：见 [值得评估](#值得评估要试再决定) 中的 `claude-code-security-review` GitHub Action。

---

### 3. Playwright（前端可复现验证）

**价值**：把视觉检查从"截图印象"升级为可重放脚本。做 UI 时，直接产出可入 evidence ledger 的脚本 + traces。

**Claude Code 侧**：装 Playwright MCP（或含它的 plugin bundle），全 session 可用。

**Codex 侧**：装 OpenAI curated `playwright` skill。

**何时启用**：任意任务涉及前端 / dashboard 渲染时。

---

## 互补工具（Claude Code）

### 4. agent-sdk-dev + mcp-server-dev + hookify（Anthropic 官方）

当你要写 SDK-level subagent / 自己的 MCP server / 复杂 hooks 时，这三个是官方权威 reference，零 API key、零 prod 风险：

```bash
/plugin install agent-sdk-dev@claude-plugins-official
/plugin install mcp-server-dev@claude-plugins-official
/plugin install hookify@claude-plugins-official
```

**hookify 特别有用**：如果你的规则规定 "automated behaviors 必须落 hooks"，hookify 直接把这件事做了。

---

### 5. pyright-lsp（Python 类型反馈）

AQG skill 集含大量 Python helper（`aqg_preflight.py` / `debug_case.py` / `validate_*.py` / `self_test.py`）。**事后 LLM review 抓语义，pyright 给写时确定性类型反馈**，两者互补。

```bash
/plugin install pyright-lsp@claude-plugins-official
npm i -g pyright   # 本机需要 pyright-langserver
```

---

### 6. GitHub 官方 MCP（替换 plugin 版）

**关键发现**：plugin 版 GitHub 拿不到 Actions raw job logs。官方 `github/github-mcp-server` 多出 `get_workflow_run_logs` / `list_workflow_runs` / `get_workflow_run` — 正是 `aqg-systematic-debugging` 在 CI 失败时**最稀缺的能力**。

```bash
claude mcp remove github   # 卸 plugin 版避免工具名冲突
claude mcp add --transport http github-official https://api.githubcopilot.com/mcp
```

`mcp__plugin_*__github__*` 命名空间和 `mcp__github-official__*` 不冲突可共存，所以你也可以两个都留。

---

### 7. agnix（Claude 配置 lint / LSP）

416 条规则覆盖 SKILL.md / CLAUDE.md / hooks / MCP 配置（Claude Code 53 + Agent Skills 31 + MCP 12），三级 auto-fix。**和 `aqg-skill-validator` 互补**：agnix 管语法/格式，aqg-skill-validator 管 AQG 业务边界。

```bash
brew install agent-sh/tap/agnix
```

落地：pre-commit 先 agnix → 再 aqg-skill-validator。

---

## 互补工具（Codex）

### 8. Codex 安全三件套
见 [跨 agent #2](#2-安全三层threat-model--sast--pr-级-review) 的 Layer A。

### 9. Figma plugin（如果做 UI）

Codex 侧自带 `frontend-skill` 和 `figma-implement-design`。如要做高质量 UI，加：
- `figma-use`
- `figma-generate-library`
- `figma-create-design-system-rules`

**何时启用**：项目需要 design system / 多 surface UI 时。非 UI 项目可缓。

### 10. Playwright（curated）
见 [跨 agent #3](#3-playwright前端可复现验证)。

---

## Harness features（无需安装但要会用）

Claude Code CLI 本体（不是 plugin）的新东西，对 gated workflow 直接相关：

### F1. 新 hook 类型
| Hook | 触发 | 用法 |
|---|---|---|
| `PreCompact` | context 压缩前 | 强制写 evidence ledger 再压缩 |
| `SubagentStart` / `SubagentStop` | 子 agent 起停 | 子任务边界审计 |
| `TaskCreated` / `TaskCompleted` | TodoWrite 触发 | 自动跟进任务关闭 |
| `PermissionDenied` | auto mode 拒绝后 | retry 前先 audit |
| `InstructionsLoaded` | CLAUDE.md / rule 加载 | 检测 rule 注入成功 |
| `FileChanged` / `CwdChanged` | 文件 / cwd 变化 | 项目切换跑 preflight |

### F2. `/reload-plugins` 实时热更
写新 AQG skill 不用重启 session，直接 `/reload-plugins` 即生效。

### F3. `SLASH_COMMAND_TOOL_CHAR_BUDGET`
skill 集较大时可能超默认 1% 截断阈值；若 slash-command 描述被截断，在 `~/.zshrc` 提高 budget：
```bash
export SLASH_COMMAND_TOOL_CHAR_BUDGET=20000
```

### F4. Subagent `preload-skills` + `isolation: worktree`
Subagent frontmatter 写 `skills: [aqg-startup-preflight, aqg-evidence-closeout]` 直接预注入，比靠 description 自动 match 更确定性。

---

## 值得评估（要试再决定）

### claude-code-security-review (Anthropic 官方 PR 安全 GitHub Action)
- **价值**：CI 级自动 PR 安全门禁，差异化扫描 + 10 大类漏洞 + 自动假阳性过滤 + PR 行级注释 → 直接入 evidence ledger
- **限制**：按 token 计费（不走订阅），且只用于审查可信 PR（未防 prompt injection）
- **试用**：先在单个仓库 dry-run 一周

### anthropic-skills:example-skills
- 真正值得装的：`doc-coauthoring`（写规范）、`internal-comms`（status report 模板）、`brand-guidelines`
- 跳过：canvas-design / slack-gif-creator / algorithmic-art

```bash
/plugin marketplace add anthropics/skills
/plugin install example-skills@anthropic-agent-skills
```

### promptfoo（编程化 eval + red team）
- 给 `aqg-audit-adjudication` accept/reject 决策准确率写 1-2 条 eval
- evidence ledger 直接吃 promptfoo JSON 当证据
- OpenAI/Anthropic 都在用

```bash
brew install promptfoo
```

### trailofbits/skills（深度安全 audit）
- **不全装**，按需选点 adapter
- 真做密码学时序分析 / CodeQL/Semgrep 规则编写 / 智能合约审计才上对应 skill

---

## 明确不推荐

| 项 | 理由 |
|---|---|
| obra/superpowers 完整套件 | brainstorm → plan → subagent → TDD 完整方法论会和 AQG 纪律 gate 打架。**只可借鉴思想，不引入** |
| github/spec-kit、BMAD-METHOD | 大切片 / 重规格方法论，默认不需要；新系统需要时临时参考 |
| CodeRabbit | 和 LLM audit 层在 review 任务上重叠，再加一层是 noise |
| letta-code / claude-mem | letta 是独立 harness（迁移成本高）；持久化记忆 plugin 通常和你已在跑的记忆层重叠 |
| filesystem MCP / archived postgres MCP | CVE-2025-53109/53110 symlink 越权 + SQL injection 已知漏洞 |
| Datadog / Vercel / Cloudflare / Snowflake / Pinecone connectors | 针对 local-first dev tooling 项目，没对应 SaaS 路径 |
| 任意从 GitHub 随机装的高 star skill | 必须先读 `SKILL.md` / scripts / hooks / 权限边界。**Trail of Bits curated marketplace 已明确警告恶意 hooks/scripts 风险** |

---

## 参考源（借鉴不引入）

这些是设计 AQG 后续 skill 时**值得看代码 / 模式**的来源，但不直接装：

| 仓库 | 借鉴点 |
|---|---|
| [openai/skills](https://github.com/openai/skills) curated | OpenAI 官方 skill 编写规范，`.curated` 目录质量高 |
| [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) | spec / incremental implementation / TDD / debugging / review / security / performance / ADR — 给 AQG 后续扩展提供参考结构 |
| [trailofbits/skills](https://github.com/trailofbits/skills) + [skills-curated](https://github.com/trailofbits/skills-curated) | differential review、static analysis、supply-chain risk、property/mutation testing — 选点 adapter |
| [obra/superpowers](https://github.com/obra/superpowers) | TDD / worktree / verification-before-completion / review flow 思路（**只借鉴**，不引入完整套件） |
| [anthropics/skills](https://github.com/anthropics/skills) | 官方 skill 写法标准 + frontmatter 规范 |
| [anthropics/claude-cookbooks](https://github.com/anthropics/claude-cookbooks) `patterns/agents/` | basic_workflows / evaluator_optimizer / orchestrator_workers Notebook，理解 multi-agent 经典模式 |

---

## 维护

- 定期 review 本清单
- 新工具进入推荐集前先在"值得评估"区跑过一段试用
- 任何 GitHub 第三方 skill 装之前必读 `SKILL.md` + `scripts/` + `hooks/`，禁止盲装
