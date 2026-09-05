# Engineering Framework

[English](ENGINEERING_FRAMEWORK.md) | 中文

> 主题：在高质量工程开发环境下高质量完成工程开发任务
> 载体: AQG 是本框架的参考实施载体；其他项目通过各自的 `quality-gates.json` 接入

## 0. 文档地位

本文是**通用型工程纪律 contract** —— 定义"在什么样的工程环境、用什么纪律，把工程任务做正确"的框架级约定，与任何具体项目、任何工具版本解耦。

AQG 是本框架的**参考实施载体**；任何工程项目都可通过各自的 `quality-gates.json` 接入。

**不进入本文**：项目级 invariant、业务逻辑、产品决策，以及某个工具 / 版本的实施细节 —— 那些落各项目自己的 `CLAUDE.md` / `AGENTS.md` / `03_INVARIANTS.md`，或落 commit / PR / decision log。本文只保留跨项目、跨时间稳定的**原则与边界**。

## 1. 北极星 invariant

**核心问题**：换负责人 / 换 Claude / 换 Codex / 换上下文，系统还能产出正确行为吗？

**Operational verifier — Transfer Test Pack**（落 `tests/transfer/`）：

- fresh checkout + 模型/客户端切换 + 固定 task set
- 期望 artifacts schema：PR body / commits / evidence ledger / decision log
- 必跑 checks 清单
- pass/fail metrics：artifacts schema 匹配率、required check 通过率、边界违反次数
- **可测阈值（建议）**：Transfer Test Pack ≥ 95% pass rate

**Layer 1-3 partial validation**（不是替代 Continuity Drill）：

- Cross-env CI（Layer 1）验证"换 OS / shell / 工具版本"
- Multi-agent handoff manifest（Layer 2）验证"换 agent"
- Incident recall（Layer 3）验证"换上下文"

Continuity Drill / Context Wipe Test 是终极 e2e 手段。

**兑现北极星的连续性机制**：decision log（持久决策，防"当初为何这么定"的健忘）/ handoff manifest（跨 session 交接）/ memory hygiene（记忆不腐）/ re-anchor（长 session 防目标漂移）—— 这些让"换负责人 / 换 agent / 换上下文"之后仍能接上，是把北极星从口号变成机制的手段（能力划分见 §3、抗知识退化见 §5 Layer 3）。

## 2. AQG 边界（保持轻 = 质量层）

### AQG 真正 own

- **schema 定义**（quality-gates / handoff manifest / runtime contract / scorecard / smoke / incident index / metrics ledger / surface fingerprint）
- **validator** 实施
- **judge** 决策（pass / warn / block）
- **review-time analyzer**（doctor / validate_agent_pack / blast-radius / skill 触发点 / weekly retro 聚类 / incident markdown index）
- **construction-time discipline**（`aqg-code-construction` 的 6 步工序 + 构造期 hook：PostToolUse checker / pre-commit gate / secret-scan / tamper-guard —— 在**动手过程中**主动施加纪律，不只事后 review）

> **两个施加时点，都不在目标生产 runtime**：AQG 从"reactive review-time 审查"演进到"也做 proactive construction-time 纪律"（skill + hook 在 agent 动手时就守门）。但两者都发生在**开发/评审期**，AQG 从不进被测项目的 production 链路 —— 见下方边界。

### AQG 永远不做

- 不跑 docker / 不跑**项目级**真实 chaos（自身 synthetic shell/install fixture 除外，详见 §3）
- 不跑 fuzz / 不跑 simulation **实施**（schema 除外）
- 不存远端 telemetry（local metrics ledger 是本地 JSONL，详见 §3）
- 不**编排** sub-agent 协作流程（schema + sample reference 除外，详见 §3）
- 不实施项目 runtime adapter
- 不嵌入**目标项目的生产 runtime**（review-time 事后审 + construction-time 构造期纪律都做，但从不在被测项目的 production 链路里跑）
- **不执行 merge / 不持有 repo privileged action**（仅产出 evidence / score / source-linked answer；merge 由 GitHub Actions branch protection / repo maintainer / admin 执行；不持有 admin token / 不绕过 branch protection）

### AQG core 边界（防"什么都往 core 塞"的膨胀陷阱）

- AQG core install **仅含**：schema + validator + lightweight scripts + skills + doctor + handoff manifest validator + blast-radius + incident markdown index + weekly retro 聚类
- **adapter 必须 standalone CLI 或项目 own**：OpenAI Agents SDK adapter / Langfuse adapter / Hypothesis adapter / Gitleaks adapter / pre-commit framework wrapper —— **不打包进 AQG core install**
- `install.sh --with-extras` 控制可选 standalone CLI 安装（参考 GNU coreutils），不破坏 core 边界

### AQG 永远 verify external evidence

不仅信任 self-attestation。每个 sub-agent claim 必须有可验证 provenance（详见 §4）。

> **命名说明**：handoff manifest 字段命名**借鉴**（inspired by）MPLP（Multi-Agent Lifecycle Protocol）L2 module 命名（Context / Plan / Confirm / Trace），用 `aqg_context` / `aqg_plan` / `aqg_confirm` / `aqg_trace` 等 prefixed key。**仅命名借鉴，AQG manifest 不是合法 MPLP L2 输出**，勿直接喂进 MPLP-strict parser。

## 3. Build / Adapt / Defer 矩阵

每个 capability 三分：
- **build** — AQG 自己写（schema / validator / evidence gates / 轻量 review-time scripts）
- **adapt** — 业界工具 + AQG 包 schema 一层（adapter 是 standalone 或项目 own）
- **defer** — 等真需要再说

| Capability | AQG | 实施 owner |
|---|---|---|
| Quality gate config schema | build | AQG |
| Gate result JSON schema | build | AQG |
| Evidence closeout / Audit adjudication / Handoff manifest validator | build | AQG |
| Blast-radius / Incident markdown index / Weekly retro 聚类 | build | AQG |
| Doctor 多 mode + Surface fingerprint validator | build | AQG（**含严格 redaction，详见 §6**） |
| Skill 触发点 | build | AQG |
| **Cross-env CI matrix（完整）** | **defer** | 项目 own GitHub Actions workflow（AQG 提供 reference example，不强制） |
| **Cross-env minimal AQG compliance smoke** | **build** | AQG（**必跑**，**限窄不跑真实 task / 不跑 Docker**：fresh checkout + install + doctor + validate_agent_pack + 生成 1 个 no-op handoff manifest 验证 schema）|
| Pre-commit hooks 引擎 | adapt（standalone） | pre-commit framework；AQG own hook evidence schema + 安装器 |
| Static analysis (shellcheck / mypy / ruff / bandit / Semgrep / OSV / Scorecard) | adapt（standalone） | 业界工具；项目自己声明 analyzer 列表 |
| Secret detection (deep scan) | adapt（standalone, **分层 mandatory**） | **AQG 自身 mandatory** Gitleaks；**高 stakes target repo mandatory**；**普通 target repo warn-only / reference example**（降低 adoption 阻力）；AQG keep 一组精简的内建 regex 自防（18 条；单一权威源 `scripts/_secret_patterns.py`） |
| Docker ephemeral install | defer | 项目 own Dockerfile + 镜像 pin |
| **Chaos / adversarial dogfooding** | **build**（AQG self synthetic shell/install fixture only；不跑项目级真 chaos） | AQG own scenario schema + 自身 synthetic fixture（删 +x / 改 ENV / PowerShell paste 测试 / git fileMode false 等环境 chaos） |
| Property-based testing | schema only | hypothesis 直接 CI 跑（项目 own）；AQG own invariant schema + seed/shrink/version/repro 字段 |
| Simulation environment | schema only | 项目 own simulation harness + CI |
| LLM eval / red-team | adapt（standalone） | promptfoo / DeepEval / OpenAI Evals |
| **Local metrics ledger**（本地 JSONL only, opt-in） | **build** | AQG own append-only JSONL ledger 在 `~/.aqg/metrics-ledger.jsonl`；**显式 opt-in**：`--record-metrics` flag 或 `AQG_METRICS=1` 环境变量启用，默认关。**严格边界**：无远端发送 / 无路径原文 / 无 auth-env-raw text / 仅 hash + count + status + version。**远端 telemetry 类必须另起 ADR** |
| **Multi-agent orchestration / sub-agent invoker** | **schema + sample reference only** | OpenAI Agents SDK / Swarm 参考 + Claude/Codex 内置 spawn 能力；AQG **不打包 invoker** |
| Docker sandbox / autonomous fix loop | defer | mini-swe-agent / SWE-ReX / OpenHands（evaluation only，throw-away branch，不进 AQG core） |
| Vector DB recall | defer | markdown index 不够时再说 |
| Continuity-of-Operation Drill | build | AQG own Context Wipe Test；长周期 drill 等稳定后 promote |
| **AQG dangerous command guard policy + example hook**（policy + example hook spec；**不是 active enforcer**） | **build** | AQG own (1) policy 定义（production write / branch protection bypass `gh pr merge --admin` / `rm -rf` 风险范围 / 已知 secret leak pattern）+ (2) example PreToolUse hook script 供 agent 客户端复用。实际拦截执行 = agent 客户端（Claude Code / Codex / 其他）own；AQG 不持有 runtime enforcement authority。**不替代 closeout boundary discipline**（skill 的 surface boundary 仍主导），是 technical policy 补丁防 prompt-level 漏接 |
| **Agent pack behavior tests**（prompt-level smoke） | **build** | AQG own 行为测试集，验证 agent pack 真会在对应触发条件下调用正确 skill。**`scripts/validate_agent_pack.py` 校验 disk shape / static metadata；behavior tests 校验触发条件下的运行时行为**——两者互补 |
| **Proactive construction discipline**（构造期 6 步 + 反模式 blocker + reviewer-objection 预测） | **build** | AQG own skill（`aqg-code-construction`）+ 构造期 hook（PostToolUse checker / pre-commit gate）；`AQG_AGENT` env gating（人透明 / AI fail-closed）—— 把质量从事后 review 推到动手过程中（§2 construction-time）|
| **Decision + knowledge continuity**（durable 决策 / 记忆卫生 / 交接） | **build** | AQG own append-only decision LOG（grammar + secret-scan gate，`docs/decisions/LOG.md`）+ memory schema/staleness scan + handoff manifest —— 操作化北极星的连续性（§1）、抗知识退化（§5 Layer 3）|
| **Agent-lifecycle hook layer**（SessionStart / PreToolUse / PostToolUse / Stop / PreCompact / UserPromptSubmit） | **build** | AQG own hook 脚本集（preflight / secret-scan / tamper-guard / construction check / closeout+handoff 提醒 / WIP checkpoint）；**warn-only 语义为主，强制项 fail-closed**；实际拦截仍由 agent 客户端 own（§4 边界保留）|
| **Security review**（OWASP Top 10 / CWE Top 25 / secure-by-default 库） | **build** | AQG own in-session checklist skill（`aqg-security-review`）；与本表 Secret detection + semgrep SAST + 外审**互补不替代** |

### 门的出错哲学：fail-closed vs warn-open

上面 hook-layer 行说"warn-only 语义为主，强制项 fail-closed"。两个 PreToolUse 门把这个分野具体化，且在**各自机制降级时刻意相反**。差异由"漏判的代价与可逆性"决定，不是不一致：

| 门 | 自身机制坏（pattern bank / validator 不可导入） | 为什么 |
|---|---|---|
| `pretooluse_secret_scan.sh` | **fail-CLOSED** —— 拒绝（exit 2） | 泄漏的 secret **不可逆**：必须轮换，且可能已被索引。若 pattern bank 不可导入、或 tamper canary 不再匹配（bank 被停用的迹象），放行就冒不可撤销的泄漏风险 —— 所以门拒绝。这也防敌手停用扫描器。 |
| `pretooluse_bash_skill_validator.sh` | **fail-OPEN** —— 告警 + 放行（exit 0） | 畸形的 skill 注册**可恢复**，且被下游兜住（CI drift test / `aqg_doctor` / review）。因 validator 一时不可用就拦下每次 skill 编辑，会白白阻断正当工作、零安全收益 —— 所以门告警并放行。 |

两门在 `python3` 完全缺失时都 fail-OPEN（环境跑不了任何 Python 门；能做的只有 stderr 可见提示），且**命中真实违规都 exit 2 拦截**。所以哲学是一致的、不矛盾：**不可逆性 + blast radius 决定降级方向** —— fail-closed 守不可逆（secret 泄漏），warn-open 守可恢复（skill lint）。下方 §4 的 sub-agent verify contract 用同一条规则（高 stakes fail-closed，普通 fail-open）。

## 4. Sub-agent + AQG Contract（verifiable provenance）

每个 sub-agent claim 必须有**可验证 provenance**，不只信 self-attestation：

1. 主 session 派 sub-agent 时，prompt 显式要求"必须调 `aqg-startup-preflight` 把 result run_id 写入 manifest"。
2. sub-agent 跑工作 → 业界工具实际执行（CI / docker / hypothesis）。
3. sub-agent 完成 → 显式调 `aqg-evidence-closeout`，manifest 必含：actor / parent_session_id / task / result / command_line / tool_version / env_fingerprint / exit_code / artifact_uri / log_digest / seeds / scenario_ids / ci_run_id。
4. 主 session 收 manifest → AQG **cross-check 外部 evidence**：
   - 调 `gh api` 反查 ci_run_id 真存在且 conclusion=success；
   - **cross-check：ci_run_id 关联的 commit SHA == 当前 PR head SHA**（防 hallucinate 重用历史 run_id）；
   - **cross-check：workflow name 匹配 expected workflow**；
   - fetch artifact 比对 log_digest。
5. verify 失败 → **高 stakes fail-closed（block）+ 标 INC**；普通 PR 不强制 contract（fail-open = not-required，不污染 baseline）。

**签名链（future）**：GitHub artifact attestation 当前仅对 CI 产物（OIDC/SLSA）自动签、不覆盖 desktop sub-agent 的 local commands，故对 local work 的 fallback 是 replayable commands + artifact digests。等 attestation profile 定型（明确 producer / subject digest / issuer / repo+ref+workflow constraints / verifier policy）再接 `signed_attestation` 完整链，走 ADR。

## 5. 4-Layer 模型

### Layer 1：环境一致性

| 能力 | AQG own | 实施 owner |
|---|---|---|
| Cross-env CI matrix（完整）| matrix schema | GitHub Actions（项目 own workflow） |
| **Cross-env minimal compliance smoke** | **build + 必跑** | AQG |
| Pre-commit / pre-push hooks | hook evidence schema + 安装器 | pre-commit framework |
| Static analysis | analyzer 声明 schema | 项目声明（语言相关） |
| `aqg_doctor --mode {commit,push,pr,session}` | core（session mode 已实装；其余按需扩展）| — |
| **Surface fingerprint（严格 redaction）** | **schema + validator + redaction enforcement** | — |

### Layer 2：任务执行可靠性

| 能力 | AQG own | 实施 owner |
|---|---|---|
| Skill 触发点 | ✅ | — |
| Blast-radius analyzer | analyzer | AQG（review-time） |
| Multi-agent handoff manifest schema + validator | schema + validator + cross-check（commit SHA / workflow name 匹配）| 每 PR 内 manifest 落 `docs/handoff-manifests/` |
| Quality scorecard | template + scoring runner | 项目自定义权重 |
| Failure taxonomy（5 类标签） | enforce 进 INC template | — |
| Closed-loop smoke 框架级要求 | enforce schema | 项目 own smoke 命令 / `aqg-smoke.yaml` / owner-approved N/A with expiry |

### Layer 3：系统抗退化

| 能力 | AQG own | 实施 owner |
|---|---|---|
| Context Wipe Test | runner + bootstrap verifier | issue 池准备 |
| **AQG self synthetic chaos fixture**（不是项目级 chaos） | scenario schema + light fixture | release 前必跑 |
| Incident markdown index + grep recall | sanitized index schema + retention | 写 INC（已有约定） |
| **Local metrics ledger** | append-only JSONL 在 `~/.aqg/`，**显式 opt-in（默认关；详见 §3）**，严格 redaction | AQG own |
| AQG self-improvement loop（weekly retro 自动聚类）| weekly cron + grep + 聚类 | AQG own |
| **Decision + knowledge continuity** | append-only decision LOG schema（grammar + secret-scan gate）+ memory schema/staleness scan + handoff manifest | AQG own —— 抗的是**知识退化**（"当初为何这么定" / 记忆是否过期 / 跨 session 接不接得上），与抗系统退化同源 |

### Layer 4：Runtime contract

AQG own schemas + validators + required evidence fields + compliance reports；项目 own 真实 runtime adapters + instrumentation。

| 能力 | AQG own | 项目 own |
|---|---|---|
| Trace id 全链贯通 | schema + 验证 evidence 字段必填 | 真实埋点 |
| Decision replay | schema + replay log 格式校验 | replayer 实装 |
| Kill switch / safe mode | required evidence field | 真 kill switch 代码 |
| Shadow → canary → rollout | 晋级规则 schema + compliance report | rollout pipeline |
| Closed-loop smoke contract | schema + N/A 机制 + expiry | smoke 命令 |

## 6. Surface Fingerprint 严格 Redaction Schema

桌面客户端 surface 含敏感数据。doctor `--mode session` fingerprint 必须严格 redaction。

### 允许字段（仅这些）

- `exists`: boolean
- `size_bytes`: int
- `sha256_first8`: hex string（first 8 chars）
- `*_count`: int（hooks_count / permissions_count / mcp_servers_count / scopes_count / path_dirs_count 等）
- `version`: string（CLI 版本号）
- `set` / `is_directory` / `verify_root`: boolean / status enum
- `host`: 标准化 hostname（github.com / 等）
- `*_status`: enum 状态（pass / fail / warn）

### 严格禁止存储

- 任何 `.env` value 原文
- 任何 token / API key / password / credential（包括 hash truncation 都不允许，因为可彩虹表）
- 完整路径（仅存 dirs_count；如必要存 path 则只存 anonymized hash）
- `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` 文本内容（可存 size + sha256_first8）
- MCP server connection strings 原文
- Model 默认值的 API endpoint URL（仅 model 名）

### 实施样例

```json
{
  "claude_settings_json": {
    "exists": true,
    "size_bytes": 1234,
    "sha256_first8": "a1b2c3d4",
    "hooks_count": 1,
    "permissions_count": 5,
    "mcp_servers_count": 3
  },
  "env_AQG_ROOT": {
    "set": true,
    "is_directory": true,
    "verify_root": "pass"
  },
  "gh_auth": {
    "logged_in": true,
    "scopes_count": 3,
    "host": "github.com"
  },
  "path_dirs_count": 12,
  "claude_cli_version": "1.2.3"
}
```

### Validator（belt-and-suspenders 三层）

- `scripts/_surface_redaction.py`：fingerprint output 守门器，发现禁止字段直接 raise + block。
- `scripts/_session_fingerprint.py`：collect surface 并保证只含上面 allowlist 字段。
- `scripts/aqg_doctor.py --mode session`：调上述两个模块 + emit 层做 install detail path 脱敏 + violation 输出 sanitize（不漏 raw value）。

### 桌面客户端 surface 完整清单

针对常见的 Codex + Claude Code 桌面客户端：

| 类别 | 具体 surface | doctor 检查模式 |
|---|---|---|
| User-level prompt | `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` | size + sha256_first8（不存原文）|
| User-level skills | `~/.claude/skills/` / `~/.codex/skills/` | dir 存在 + count |
| User-level hooks | `~/.claude/settings.json` | size + sha256_first8 + hooks_count |
| MCP servers | `settings.json` mcpServers | count + name list（标准化）|
| 环境变量 / `.env` | `$AQG_ROOT` / `$XDG_DATA_HOME` / `$CODEX_HOME` | set + is_directory + verify_root（不存 value）|
| CLI 版本 | git / gh / python3 / bash / pwsh | version string |
| 模型默认 | Claude / Codex 默认 model | model name only（不存 endpoint URL）|
| 权限 / sandbox | macOS Full Disk Access / Codex sandbox 模式 | enum status |
| `PATH` | dirs count（不存原 path）| dirs_count |
| Auth state | gh auth / git credential / audit-mcp | logged_in boolean + scopes_count + host |

**不可控变量**：LLM 模型本身（Anthropic / OpenAI 升级）、客户端 release 行为变化、业务真实信号。

## 7. 版本与更新规则

- v 升级走 ADR：影响 ownership 边界 / Layer 划分 / 北极星 invariant 操作化方式 → 必须 ADR + 双审。
- 不影响 contract 的小改 → PR + 单审。
- 每完成一块框架能力，本文更新对应 Layer 的 own/owner 划分（不记录具体版本实施日志 —— 那些落 commit / PR / decision log）。
