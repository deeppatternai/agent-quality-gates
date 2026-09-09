# Agent Quality Gates

[English](README.md) | 中文

当前版本：`0.14.13`（权威源：`VERSION`；release notes 见 `CHANGELOG.md`）。

Agent Quality Gates（`AQG`）是给 AI 编码工作流用的**质量纪律工具箱** —— 把质量从"写完再审"推到"边写边守"。它**骑在你现有的 coding agent 之上**、与任何具体 agent 解耦，四大件：

- **skills** —— 16 个质量纪律 skill，覆盖完整工作生命周期（下表）。
- **hooks** —— 机械强制层，把纪律里"可自动触发"的那部分落到 agent 的生命周期事件上。
- **gate scripts + CI adapter** —— 确定性的 worktree / audit / evidence 检查，通过 per-project `quality-gates.json` 接进本地运行和 GitHub PR。
- **docs** —— 工程框架 contract、rollout / decision / audit-evidence 记录、模板。

## 为什么用 AQG

多数质量工具是在代码**写完之后**审、由**单个模型**审、只对**一种语言**审，而且会话之间什么都不记得。AQG 反过来 —— **边写边守**、在接入外部审计引擎后可用**独立厂商模型**交叉校验、适用**几乎任何语言**、把状态**跨会话**带下去。下面每个 skill 都是一项被磨利的独立能力，不是让你"从名字猜"。

### 一次就把代码写对

- **构造期纪律，而非事后审。** `aqg-code-construction` 在实现**过程中**跑 6 步流程。对于新增行为和 bug fix，第 2 步 **Behavior Lock** 使用 RED → GREEN → REFACTOR 的 TDD 循环：先写会失败的测试、**再**写代码，让实现被钉在可验证的行为上。纯重构沿用现有回归网；文档/spec/config 与 migration 按 skill 中的分类型路径处理。第 3 步 **Thin Slice** 只提交最小改动、把顺手的 refactor 推迟，不让冗余搭车。外加 4+4 反模式 blocker、分级的 reviewer-objection 预测、结构化 evidence ledger。
- **几乎任何语言。** 构造、审查、调试三类 skill 语言无关 —— 纪律适用任何代码库，不管用什么语言写的：Python、JavaScript、TypeScript、Java、Kotlin、Scala、Groovy、Clojure、C、C++、C#、F#、Objective-C、Go、Rust、Zig、Nim、Crystal、Swift、Ruby、PHP、Perl、Dart、Elixir、Erlang、Haskell、OCaml、Elm、Racket、Lua、Julia、R、Solidity、Shell/Bash、PowerShell、SQL、HTML/CSS、Vue、Svelte、CoffeeScript 等等。构造提醒开箱即自动触发于 54 个文件扩展名；纪律本身没有语言边界。

### 审出单个模型漏掉的东西

- **为跨-LLM 准备的 5 维审查。** `aqg-multi-review` 为逻辑 / 边界 / 安全 / 性能 / 并发五个维度生成定向 routing prompt 和裁决 ledger。接入外部 `/audit` 引擎后，这些维度可以交给**独立厂商** LLM panel；未接引擎时，本仓交付的是结构化路由 + 自审 + 裁决骨架。
- **真正的安全深度。** `aqg-security-review` 支持 3 层安全工作流：① AQG 自带的 6 步 in-session OWASP Top 10 + CWE Top 25 + secure-by-default 库清单；② 外接 Semgrep 的确定性 SAST；③ 外接 `/audit` 的 LLM 审查。AQG 定义并串起这套工作流，Semgrep 与 LLM 引擎分别接入。
- **测行为、而非测形状的测试。** `aqg-test-quality-review` 揪出那些只断言 SHAPE（类型、key 存在）而非 BEHAVIOR（输出 / 副作用 / 报错）的测试，并识别被削弱、被 skip、flaky（sleep / 挂钟时间 / 无种子随机 / 真网络）的测试。
- **root-cause 调试。** `aqg-systematic-debugging` 强制 复现 → 定界 → 一次一个假设 → 最小 fix → 回归检查。没证据不 fix —— 任何语言/栈通用。

### 跨会话不丢线索

- **8 段式交接。** `aqg-session-handoff` 产出一份 paste-ready 的跨会话 prompt（背景 · 已验证现状 · 下一步 · 纪律 traps · 第一个具体动作 · open decisions），外加一个 `validate` gate —— 缺段或泄密就拒。下一个会话拿着完整交接冷启动接手 —— 项目说得清、不漏这忘那。
- **几个月后还能 grep 的决策日志。** `aqg-decision-capture` 把每个有持久价值的决策 append 进 `docs/decisions/LOG.md`，一行一条、读时脱敏（日期 · actor · 决策 · 理由 · basis）。长期重度使用时，这就是你翻出**当初为什么这么决定**的地方 —— 留下的是理由，不只是结果。
- **项目账本。** `aqg-project-status` 渲染本机的进度账本，让你 review 项目实际进展到哪。
- **memory 卫生。** `aqg-memory-hygiene` 让工程知识落进仓库（代码 / 文档 / PR）而非过时的本机 memory，校验 memory frontmatter，并标出过了 re-verify 期限的条目。

### 不靠"想起来"的护栏

- **动手前先 preflight。** `aqg-startup-preflight` 检查实时的 git + GitHub 状态（dirty worktree / gone upstream / 落后 remote / 缺 context 文件 / open PR），让你绝不在陈旧基线上起步。
- **说"完成"前先 closeout。** `aqg-evidence-closeout` 逼你回答 6 个问题 —— 什么改了 · 什么 fresh check 证明了 · 什么 durable state 更新了 · 什么边界你**没**碰 · 什么仍在 block —— 才能声称完成。
- **机械强制。** Claude Code、Codex、Cursor、CodeBuddy、Kimi Code、Qoder CLI、Trae IDE 与 Devin CLI 有 managed 生命周期 adapter；WorkBuddy、Trae Work、Trae Work CN、Kimi Work、Zed、QoderWork 与 QoderWake 只暴露官方可证明子集。

一眼速览：

| | |
|---|---|
| Skills | **16** 个，覆盖完整工作生命周期 —— 每个都是被磨利的能力 |
| 语言覆盖 | **40+** 种语言 —— 纪律语言无关；自动提醒覆盖 **54** 个文件扩展名 |
| 审查 | **5** 维审查 router · 可选外部 **跨-LLM** panel · OWASP Top 10 + CWE Top 25 安全 |
| 强制层 | Claude Code + Codex **6** 类生命周期事件 / **4** 个 blocking policy · Cursor **5** 个事件 · CodeBuddy/Kimi Code 随 profile 区分 · Qoder 随 profile 区分 |
| Coding agent | **Claude Code + Codex + Cursor + Pi + CodeBuddy + Kimi Code + Qoder 家族 + Trae 家族 + Zed + Devin + 部分 work 客户端** |

## 支持的 coding agent

AQG 能力核心 agent-agnostic；每个 agent 通过一层薄 adapter 接入。[`AI_SETUP.md`](AI_SETUP.md) 是 installed-supported by default：它优先从 `scripts/aqg_client_registry.py` 解析支持状态，检测本地 supported/full/partial 配置目录，并配置每个命中的本地桌面端 adapter。

| Coding agent | 状态 | 托管面 |
|---|---|---|
| Claude Code | 支持 | skills + hooks + `CLAUDE.md` 规则块 |
| Codex | 支持 | skills + hooks + `AGENTS.md` 规则块 |
| Cursor | 支持 | skills + hooks + 项目规则；User Rules 仍由 UI 管理 |
| WorkBuddy（`workbuddy`）| partial | skills + rules/report；无已证实的生命周期 hooks |
| CodeBuddy（`codebuddy`）| full | skills + hooks + rules + MCP |
| WorkBuddy AI（`workbuddy-ai`）| full | skills + hooks + rules + MCP，全部写入独立根 `~/.workbuddy-ai`；不与 `workbuddy` 或 `codebuddy` 共享配置根 |
| Trae Work（`trae-work`）| partial | skills + rules + MCP；TRAE Work hook parity 未知 |
| Kimi Work（`kimi-work`）| partial | Kimi Work Desktop / Kimi Desktop 本地 Daimon skills root；无 rules/hooks/MCP gate |
| Kimi Code（`kimi-code`）| partial | skills + advisory hooks + rules + MCP；hooks fail-open |
| Qoder CLI / Qoder CLI CN（`qoder-cli` / `qoder-cli-cn`）| full | skills + hooks + user/project rules |
| Qoder IDE / 通义灵码（`qoder` / `qoder-cn`）| partial | skills + hooks + 项目规则；无 `SessionStart`、`PreCompact`、WIP save/recover |
| Trae / Trae CN（`trae` / `trae-cn`）| partial | skills + hooks + 项目规则；无 `PreCompact`，User Rules 由 UI 管理 |
| Trae Work CN（`trae-work-cn`）| partial | skills + 项目规则；Work lifecycle hook schema 未验证 |
| Zed（`zed`）| partial | skills + `AGENTS.md` instructions；无 lifecycle hooks |
| Devin（`devin`）| partial | skills + hooks + `AGENTS.md` rules；无压缩前 `PreCompact` |
| QoderWork（`qoderwork`）| partial | skills + rules + MCP；无 Qoder CLI lifecycle hook contract |
| QoderWake（`qoderwake`）| partial | skills + rules + MCP；无 blocking lifecycle hook contract |
| Pi（`pi`）| partial | skills + TypeScript extension hooks + 支持报告；无 AQG MCP connector |

能力矩阵与逐功能降级说明见 [`docs/client-support-matrix.zh-CN.md`](docs/client-support-matrix.zh-CN.md) 与 [`docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md`](docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md)。

## 安装

### Quick start —— 让 AI 自己装

把整份 [`AI_SETUP.md`](AI_SETUP.md) 丢给你的 coding agent。它会检测本地已安装且 AQG supported/full/partial 的桌面端，通过 `scripts/install_aqg_clients.py --installed-supported` 规划，并幂等配置每个已选规则面，无需手改配置。

### Multi-client install —— 显式批量入口

只有当你明确想一次安装多个客户端时，才使用 wrapper：

```bash
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --apply
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry --project-root /path/to/project --apply
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --clients codex,claude-code,cursor,qoder-cli --apply
```

wrapper 默认 dry-run；只有加 `--apply` 才会写入。被选中客户端只要支持 lifecycle hooks，就会默认安装/刷新 hooks；只有用户明确要求 skills/rules-only 时才传 `--no-hooks`。`--installed-supported` 检测本地配置目录并只选择 `support_status` 为 `supported`、`full` 或 `partial` 的 registry client。`kimi-work` 指 Kimi Work Desktop / Kimi Desktop，会从 Daimon skills root 检测（`KIMI_WORK_SKILLS_ROOT`、`KIMI_DESKTOP_SKILLS_ROOT`、运行中的 Daimon 进程命令行、Kimi Desktop 日志、`.rehomed` marker、Kimi.exe 安装盘证据）；`kimi-code` 仍指 Kimi Code CLI，目标是 `~/.kimi-code`，两者不是同一个安装目标。`--all-registry` 会从 `scripts/aqg_client_registry.py` 动态展开所有 registry adapter，不按 support-status label 过滤；`--all-supported` 是同一模式的向后兼容别名。当前展开为 `codex`、`claude-code`、`cursor`、`workbuddy`、`codebuddy`、`trae-work`、`kimi-work`、`kimi-code`、`qoder-cli`、`qoder-cli-cn`、`qoder`、`qoder-cn`、`trae`、`trae-cn`、`trae-work-cn`、`zed`、`devin`、`qoderwork`、`qoderwake`、`pi`。plan 会展示每个 client 的 `support_status`，并对降级 label 输出能力提示；对 `kimi-work` 还会展示 `resolved_skills_root` 和 `discovery_source`。它也暴露 registry 的只读/移除动作：`--verify`、`--is-installed`、`--uninstall`。`--core` 只展开为 `codex,claude-code`。Qoder IDE 与 Pi project-scope profiles 在执行写入动作前必须提供 `--project-root`。

registry 客户端的 skills 默认使用 link/symlink 安装。copy 模式必须显式指定：Codex 用 `--copy`；Claude Code 用 `--mode copy`；Cursor、Qoder、work/code clients、Trae/Zed/Devin clients 与 Pi 用 `--mode copy`。Kimi Work 默认先尝试 link；若 Windows symlink/junction 创建失败，会自动 fallback 到 copy，并在输出、marker、support report 中记录 `requested_mode=link`、`effective_mode=copy` 和 `fallback_reason`。显式 `--mode copy` 会直接 copy；显式 `--mode link --strict-link` 会在 link 失败时 fail closed。安装后请重启 Kimi Work / Kimi Desktop 让 Daimon 重新加载 skills。Qoder installer 当前没有 `--no-hooks`，所以 wrapper 的 `--no-hooks` 遇到 Qoder 家族 profiles 会拒绝，而不是伪造不支持的参数。Work/code clients 使用 `scripts/install_aqg_work_clients.py`，Trae/Zed/Devin clients 使用 `scripts/install_aqg_agent_clients.py`，Pi 使用 `scripts/install_aqg_pi.py`；三者都支持 `--no-hooks`。没有官方 hook 合同的 profile 不会写入 hook。

### 一条命令 —— Claude Code + Codex 首装

```bash
bash -lc 'set -euo pipefail; repo="$HOME/.deeppattern/agent-quality-gates"; if [ ! -e "$repo" ] && [ ! -L "$repo" ]; then mkdir -p "$(dirname "$repo")"; gh repo clone deeppatternai/agent-quality-gates "$repo" -- --config core.autocrlf=false --config core.eol=lf; fi; export AQG_ROOT="$repo"; python3 "$repo/scripts/install_aqg_clients.py" --clients codex,claude-code --aqg-root "$repo" --apply; python3 "$repo/scripts/aqg_doctor.py" --no-cli'
```

首次安装会 clone 到 `$HOME/.deeppattern/agent-quality-gates`，已有安装则复用当前 checkout，再通过统一 wrapper 安装 Codex 和 Claude Code 并运行 Doctor。默认目录安装成功后会尝试建立可切换的版本目录；必须检查最后的自动更新结果，安装成功不代表目录转换成功。该命令不会在受管版本中执行 checkout。主动升级使用 `scripts/upgrade.sh`。前提与仍需协调的更新类型见[Windows 自动更新](docs/WINDOWS_AUTO_UPDATE.md)。

Claude Code hooks 需要在客户端环境里 export `AQG_ROOT`。Codex hooks 固化已审阅 checkout 路径，不依赖 shell 继承该变量：

```bash
export AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"   # 建议加进 shell profile
```

> 没 `gh`？把 `gh repo clone …` 换成 `git clone git@github.com:deeppatternai/agent-quality-gates.git "$repo"`。Windows / PowerShell：保留 `bash -lc '...'` 外壳（PowerShell 不展开 `$HOME`）。

### Claude Code + Codex 升级

```bash
"$HOME/.deeppattern/agent-quality-gates/scripts/upgrade.sh"
```

pull main → 重装 + prune 掉残留 skill → 默认安装/刷新支持的 Claude Code/Codex hooks → `aqg_doctor.py` 验证。它**不会**自动改写 `CLAUDE.md` / `AGENTS.md` 中的 AQG rules block；请另行运行 `scripts/install_aqg_rules.py --client <claude-code|codex> --apply`，该命令会先备份，并且只替换受管区段。只有明确要 skills-only 升级时才传 `--no-hooks`。Cursor/Qoder/work-code 家族通过重跑下方幂等 `--apply` 刷新。选项：`--ref vX.Y.Z`（钉 tag/commit）、`--hooks`、`--no-hooks`、`--clean-only`。

### Hooks & pre-commit

- **Codex hooks（安装 Codex support 时默认安装，含 4 个 blocking policy）：** `python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"`（支持 `--verify` / `--uninstall` / `--is-installed`）；幂等合并 `~/.codex/hooks.json`，把 runner+policy 内容摘要绑定到定义，重启后通过 `/hooks` 审阅信任。
- **Claude Code hooks：** `python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --apply`（支持 `--verify` / `--uninstall` / `--is-installed`）。永不 blocking 的起步版用 `settings.warn-only.example.json`。
- **Cursor（user scope）：** `python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope user --mode link` 安装 skills 与 hooks；项目级用 `--scope project --project-root /path/to/project --mode link`，并写 `.cursor/rules/aqg.mdc`。支持 `--verify` / `--uninstall` / `--is-installed`，也可传 `--no-hooks`。Cursor User Rules 由 UI 管理，AQG 不写未公开的 user rule 文件。
- **Work/Code clients：** `python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client codebuddy --scope user --aqg-root "$AQG_ROOT" --mode link --apply`。profiles: `workbuddy`、`codebuddy`、`kimi-work`、`kimi-code`、`qoderwork`、`qoderwake`。`trae-work` 已从这个 adapter 退役；它与 Trae 共用 skills root，因此改由 `install_aqg_agent_clients.py` 安装，旧入口会以 exit 2 输出重定向说明。`kimi-work` 指 Kimi Work Desktop / Kimi Desktop，安装到动态解析出的 Daimon `daimon-share/daimon/skills`；特殊路径或测试可用 `--skills-root /absolute/path/to/skills` 覆盖。`kimi-code` 指 Kimi Code CLI，仍使用 `~/.kimi-code`，不要混用。支持 `--verify` / `--uninstall` / `--is-installed`，也可传 `--no-hooks`。降级 profile 只安装官方文档可证明的托管面，并在支持报告里列出缺失的 gate coverage。

Kimi Work Desktop / Kimi Desktop 的 Daimon shareDir 可能随安装盘 rehome：旧安装可能在 `%APPDATA%\kimi-desktop\daimon-share`，D 盘安装可能在 `D:\KimiData\daimon-share`。AQG 因此动态发现 root，不新增 `kimi-desktop` client_id。公开 Kimi 文档主要覆盖 Kimi Code CLI；Kimi Work Desktop/Daimon root 来自安装包 README、运行日志、进程命令行等本地证据，所以 adapter 支持显式 root 覆盖。
- **Qoder 家族：** `python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client qoder-cli --scope user --aqg-root "$AQG_ROOT" --mode link --apply`。profile 四选一：`qoder`、`qoder-cli`、`qoder-cn`、`qoder-cli-cn`；项目级还需 `--project-root /path/to/project --mode link`。支持 `--verify` / `--uninstall` / `--is-installed`。CLI profile 为 full；IDE profile 为 partial，因为缺少 `SessionStart`、`PreCompact` 与 WIP save/recover。Qoder 没有 `--no-hooks` 模式。
Qoder Desktop profile 只按精确 macOS 产品身份选择，不根据配置目录猜测。`qoder` 接受 `Qoder.app` / `com.qoder.app` 和 `Qoder IDE.app` / `com.qoder.ide`；`qoder-cn` 接受 `Qoder CN.app` / `com.qodercn.app` 和 `Qoder CN IDE.app` / `com.aliyun.lingma.ide`。同一 profile 的多个合法 bundle 同时存在时只选择一次。CLI profile 仍要求在 `PATH` 上找到对应可执行文件；Desktop 与 CLI 同时命中共享 settings surface 时保持 fail-closed，要求显式选择 `--clients`。
- **Trae / Zed / Devin：** `python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client trae --scope user --aqg-root "$AQG_ROOT" --mode link --apply`。profile 六选一：`trae`、`trae-cn`、`trae-work`、`trae-work-cn`、`zed`、`devin`；项目级还需 `--project-root /path/to/project --mode link`。支持 `--verify` / `--uninstall` / `--is-installed` 和 `--no-hooks`。六个 profile 都是 partial；`trae-work` 现在通过这个 adapter 使用共享的 `.trae` root。降级项见 `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md`。
- **Pi：** `python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope user --aqg-root "$AQG_ROOT" --mode link --apply` 会把 AQG skills 安装到 `~/.pi/agent/skills`，把托管 TypeScript extension 写到 `~/.pi/agent/extensions`，并写支持报告。项目级使用 `.pi/`：`--scope project --project-root /path/to/project --mode link`。支持 `--verify` / `--uninstall` / `--is-installed` 和 `--no-hooks`；等级为 partial，因为 MCP/connectors 不支持，closeout/WIP save 依赖 Pi extension 生命周期投递。
- **pre-commit**（git-lifecycle secret 门，opt-in）—— 通过 [pre-commit framework](https://pre-commit.com/) 接 [Gitleaks](https://github.com/gitleaks/gitleaks)：`python3 "$AQG_ROOT/scripts/install_pre_commit.py" --target-repo /path/to/project`。AQG 默认**不** `pip install`（`--allow-pip` 才开）。模板：`templates/pre-commit-config.aqg.example.yaml`、`templates/.gitleaks.aqg.example.toml`。
- 各 agent 低层 installer（`--copy` / `--force` / `--dest`）、版本化/钉版安装、多机部署：见 `docs/INTEGRATION_GUIDE.md` + `docs/INSTALL_VERSIONING.md`。

> **用 hooks 前先把 `.aqg/` 加进目标项目 `.gitignore`**（warn-only hook 会读本地 `<project>/.aqg/pr-body.md`，别进 commit）。可 cat `examples/aqg-gitignore.example`。

## Skills（16 个）

所有受支持客户端的 installer 都交付同一组 16 个 skill（Claude Code 使用生成的 wrapper pack），按工作 phase 分：

| phase | skill | 作用 |
|---|---|---|
| 启动 | `aqg-startup-preflight` | 干活前查 worktree + GitHub state（dirty / gone upstream / behind / 缺 required file）+ 并行-session advisory；永不 blocker，默认当前 git root（`--repo` override）|
| 构造 | `aqg-code-construction` | 6 步 proactive 构造工序 + 4+4 anti-pattern blocker + tiered objection 预测 + structured ledger（详见下面 Code Construction 段）|
| 调试 | `aqg-systematic-debugging` | root-cause-first 6 步 debug + 8 层 feedback-loop tactic（type/lint < 5s → unit < 30s → … → E2E）；没 evidence 不 propose fix |
| 审查 | `aqg-multi-review` | 跨 5 维度（logic / edge_cases / security / performance / concurrency）的审查 router；生成分维度 prompt 与结构化裁决 ledger |
| 审查 | `aqg-security-review` | in-session OWASP Top 10 + CWE Top 25 + 8 类 secure-by-default 库 checklist；与 semgrep SAST + audit-mcp 外审三层互补 |
| 审查 | `aqg-test-quality-review` | 判测试是 BEHAVIOR（输出/副作用/错误）还是 SHAPE（类型/结构/键存在）+ 找 coverage gap / weakened / flaky；signal-only |
| 审查 | `aqg-audit-adjudication` | 把 audit / code-review / second-opinion finding 决出 accept / reject / needs-user-decision，出结构化 table |
| 完工 | `aqg-evidence-closeout` | 完工前出 evidence ledger；自动 import `.aqg/current_ledger.md`（来自 construction skill）+ Transfer Test Pack run summary |
| 完工 | `aqg-decision-capture` | 持久决策（裁定 / agent 自主 / 外审裁决）→ `docs/decisions/LOG.md` 一行可 grep、读时脱敏条目（`format` / `query` / `validate`）；抗"当初为何这么定"健忘 |
| 交接 | `aqg-session-handoff` | CTX 满 / 已压缩 / 交接下个 session 时出 paste-ready 交接 prompt（背景 + 现状 + 下一步 + 纪律 traps）|
| 编排 | `aqg-phase-transition` | 三个 phase 节点（`PLAN_DONE` / `IMPL_DONE` / `TESTS_WRITTEN`）emit signal，深度取自 `docs/policies/audit-trigger.md` 的 `depth-by-stakes` 映射（本 skill 只提供时机，不决定深度）+ 5min dedup |
| 编排 | `aqg-re-anchor` | 长 session step / WorkPacket 边界 emit 紧凑重述（goal + 活跃 gates + progress），抗 goal-drift；emit-only |
| 元 | `aqg-skill-validator` | commit 前验证 skill sidecar manifest + SKILL.md frontmatter + boundary 段 + cross-cutting registration + wrapper-sync drift（`--strict` 下 fail）|
| 元 | `aqg-automation-audit` | 自动化栈 inventory（hooks / MCP / plugins / skills / env）+ overlap-check + 结构化 verdict（`keep` / `migrate` / `disable` / `fix`）；advisory + read-only |
| 元 | `aqg-project-status` | 查本机本地 ledger 的低保真进度事件（会话窗口内非-merge commit）；progress-only，绝不联网/上云 |
| 元 | `aqg-memory-hygiene` | 写 memory 前自检 —— 工程/项目内容落 repo（code/docs/PR）不进 machine-local memory；只留 user 偏好 / how-to-work / 外部指针 |

这些 skill 是工具层：无生产密钥、不部署、不限制项目路径。默认在当前工作目录的 git root 跑；多仓检查传 `--repo /path/to/repo`。

## 纪律怎么落地

三个面从软到硬落同一套纪律。

### 1. Rule block —— 所有 agent

Skill 会按 description 自动 match，但要在关键节点**强制**触发，得把纪律安装到客户端公开支持的规则面。Claude Code 与 Codex 使用以下可手工同步的文件：

| scope | Claude Code | Codex |
|---|---|---|
| 全局（user）| `~/.claude/CLAUDE.md` | `~/.codex/AGENTS.md` |
| 项目 | `<project>/CLAUDE.md` | `<project>/AGENTS.md` |

全局规则块用 client-support installer 装：它把块追加在文件已有内容之后、给自己那一段打上标记（重跑只替换该段）、解析块里的 checkout 路径，并在覆盖前先备份：

```bash
# Claude Code
python3 "$HOME/.deeppattern/agent-quality-gates/scripts/install_aqg_rules.py" --client claude-code --apply
# Codex
python3 "$HOME/.deeppattern/agent-quality-gates/scripts/install_aqg_rules.py" --client codex --apply
```

`--verify` 用随仓模板复查已装的块；`--uninstall` 只删受管区段，文件其余部分不动。**项目级**规则块请用模板头部自带的 `sed` 命令贴进项目文件——installer 只写 user scope。若你在这个 installer 之前手工装过块，`--apply` 会拒绝而不是再追加一份：先把旧的那一段删掉。

Cursor 与 Qoder 家族的规则由各自 installer 托管：Cursor 只写项目规则（User Rules 由 UI 管理）；Qoder IDE 写项目规则，Qoder CLI profile 支持 user/project rules。installer 会拒绝覆盖非 AQG 托管的规则文件。

这个 block 只点名**一个入口** —— 写代码前调用 `aqg-code-construction`，外加判断「这次改动到底要不要审」的 Gate A 判据。它刻意不复述每个 skill 干什么：host 已经把 16 个 skill 的 description 当索引加载了，每个 `aqg-*` skill 也在那里声明自己的触发条件。block 承载的是那些必须在**读任何东西之前**就常驻的内容。全局 vs 项目：自己用全局，项目级 block（提交进 git）让协作者共享同一套规则。

> **Dogfood 提醒**：在 AQG 主导的项目里跑的 session 本身就是这套规则的用户 —— 要 invoke skill，别只"照精神办"（schema 一致性 + 发现 ergonomic 缺陷 + 跨-actor 交接）。

### 2. Hook 层 —— 受支持客户端

Hooks 把“可机械触发”的子集落到各客户端公开支持的生命周期事件上。Claude Code 使用 canonical managed scripts；Codex、Cursor、Qoder 通过薄 adapter 转换事件 schema，并在 host 提供对应事件时复用同一 policy。因此覆盖范围以开头支持矩阵为准，不暗示所有客户端事件完全相同。托管/hosted tools 及明确绕过本地 hook pipeline 的专用路径仍不在机械强制边界内。

| 触发 | 级别 | 效果 |
|---|---|---|
| PreToolUse(Bash) —— 即将 `git commit` | **BLOCK** | staged 改动碰 `skills/aqg-*/SKILL.md` 或 `install.sh` → 自动跑 `aqg-skill-validator`；失败拒绝 commit |
| PreToolUse(Edit\|Write\|MultiEdit) —— 写 memory | **BLOCK** | 目标在 `~/.claude/projects/*/memory/` 且内容 code-shaped（fenced block / `def`/`class`）→ 拒（工程知识该落 repo）|
| PreToolUse(文件编辑) —— AQG gate-file tamper guard | **BLOCK** | 从 AQG checkout 外部编辑受托管的 AQG gate 文件 → 拒绝（跨项目防篡改）|
| PreToolUse(Write\|Edit\|Bash) —— secret 扫描 | **BLOCK** | 已知 secret 写盘或进 Bash 命令（18 条内建 pattern）→ 拒 |
| PostToolUse(Bash, error) | warn | 提示 `aqg-systematic-debugging` 6 步 |
| PostToolUse(改代码文件) | warn | 问是否走了 `aqg-code-construction` 6 步 |
| PostToolUse(改测试文件) | warn | 提示 `aqg-test-quality-review`（BEHAVIOR vs SHAPE / flaky）|
| PostToolUse(改安全敏感文件) | warn | 提示 `aqg-security-review`（OWASP / CWE）|
| PostToolUse(改 AQG skill 文件) | warn | 提示 commit 前 `aqg-skill-validator` |
| PreCompact + Stop | warn | 查 `aqg-evidence-closeout` 6 问 + 提示 `aqg-session-handoff`；把 worktree 快照进 `refs/aqg-wip/<session>`（零改动、绝不出机器）|
| SessionStart | info | 自动跑 `aqg-startup-preflight`（summary 上限 50 行）+ surface 未恢复的 wip checkpoint |
| UserPromptSubmit(handoff 意图) | inject | 检测到 handoff 意图 → 注入指令强制 `aqg-session-handoff`（禁自由发挥）；否则静默 |

4 个 blocking 门各带 `AQG_AGENT=human-opt-in` 逃生口。managed 脚本在 `agent-packs/claude-code/hooks/`。Claude Code 依赖客户端环境中的 `AQG_ROOT`；Codex runner 固化 checkout 并转换 schema。Codex 的 memory-placement 门覆盖 `apply_patch`；shell 写入只做 literal secret 检查，不承诺 memory-placement 语义。hosted tools 和绕过本地 hook pipeline 的专用路径不在强制边界内。行为测试：`tests/behavior/test_aqg_hooks.py`、`tests/test_codex_hooks.py`。

### 3. Code construction —— proactive 纪律

`aqg-code-construction` 把质量从事后 review 推到**动手过程中**施加，治三个 failure mode：freestyle 不读邻居代码、顺手重构、自审空话。强制 6 步工序（Pattern Mining → Behavior Lock → Thin Slice → Construction Rules → Local Verification → Self Review）+ 4+4 anti-pattern blocker + tiered（1/3/5）reviewer-objection 预测 + structured 5-列 ledger。

一个 pre-commit checker 强制它，但**只在 `AQG_AGENT` 指定 agent 时**生效 —— 人的 commit 静默通过（透明）。设 `AQG_AGENT=claude` / `codex` 才强制。per-repo setup + 完整 workflow：见 `skills/aqg-code-construction/SKILL.md`。

## Gate scripts & CI

Skill 在 session 内提醒；gate script 给可复现、CI-ready 的红绿信号。**先 warn-only**，模板和习惯稳了再切 blocking。

确定性 gate（各带离线 `--self-test`）：

```bash
python3 scripts/check_dirty_or_gone_worktree.py --repo /path/to/repo   # dirty / detached / upstream gone / behind → 非零
python3 scripts/validate_audit_adjudication.py path/to/audit.md         # 须含 finding/decision/action/verification 表
python3 scripts/check_evidence_closeout.py --strict path/to/pr.md       # scope / verification / audit / durable state / boundary / blockers
```

**Config** —— per-project `quality-gates.json`（`version: 1`；每个 gate 的严格度：`warn` / `blocking` / `off`；未知 version/gate/mode 一律 fail-closed）。校验：`python3 scripts/quality_gates_config.py --config quality-gates.json --print-json`。

**CI adapter** —— 本地 adapter（`scripts/run_quality_gates.py`）读 config + 本地 PR-body markdown，出 console summary + redacted JSON（不碰 GitHub、绝不写 raw PR body）。GitHub PR adapter（`scripts/fetch_pr_body.py` / `render_pr_comment.py` / `post_pr_comment.py`）发一条有界 sticky comment。把 `examples/github-actions/quality-gates-warn.yml` + `examples/quality-gates.json` 拷进目标仓即可接入；绝不改目标仓 CI 或 branch protection。完整 rollout（private-read 凭据、PAT 轮换、fork-PR 安全、blocking）：`docs/INTEGRATION_GUIDE.md`。

## 边界与安全

- **只是工具层** —— 无生产密钥、不部署、不改 branch protection；只产出 evidence / score / source-linked answer，绝不执行 merge。
- **绝不进目标项目的生产 runtime** —— 纪律在 review-time 和构造期（dev-time）施加，从不在项目的 production 链路里跑。
- **默认 redaction** —— doctor session fingerprint 只存 metadata（hash / count / status / version），绝不存 `.env` 原文 / token / 完整路径；本机 ledger 和 wip checkpoint 绝不出机器。
- **`.aqg/` 必须 gitignore** —— 启用 hooks 前，把它加入每个目标项目的 `.gitignore`，避免 workspace 状态进入 commit 历史。

## 已知局限

清楚区分本仓**装了什么**、**没装什么**：

- **跨厂商外审引擎在仓外。** `aqg-multi-review` 与 `aqg-code-construction` 中的 `/audit` 交接点生成 routing prompt，并裁决引擎返回的 finding；panel 引擎本身**不在本仓**，是由你自备或 stub 的独立组件。开箱状态下，把这些能力当作**结构化路由 + 自审 + 裁决的骨架**，而非可用的跨厂商第二意见。在你接入引擎之前，任何叙述都不该被理解成"装了 AQG 就有独立多模型外审"。
- **hook 提醒是尽力送达、未端到端验证。** hook 层在触发点 emit 提醒，但其在每个 agent 界面 / 版本上的实际送达未做完整测试。把 hook 当作让 gate 常驻的 nudge，不是保证送达的通道。
- **"catches what one model misses" 是机制论证、不是实测结果。** 它基于"独立厂商 panel 盲区不相关"这一机制推理，而非统计功效充分的 benchmark（内部对比目前 n=1）。请当作机制的示意，而非量化的质量增益。

## Docs & templates

- `docs/ENGINEERING_FRAMEWORK.md` —— 通用型工程纪律 contract（北极星 invariant / AQG 边界 / Build-Adapt-Defer / 4-Layer 模型 / redaction）。AQG 是它的参考实现。
- `docs/AGENT_COMPATIBILITY_STRATEGY.md` —— 跨 agent 的 adapter 策略。
- `docs/DOCUMENTATION_OPERATING_MODEL.md` —— bugfix / decision / gate-rollout / audit-evidence 记录的落点。
- `docs/INTEGRATION_GUIDE.md` · `docs/INSTALL_VERSIONING.md` —— 多机部署 + 钉 commit / tag 的安装策略。
- `templates/` —— PR、bugfix、decision、gate-rollout、audit-evidence 记录模板。

## 维护发布版本

`VERSION` 是唯一权威源。在仓库根目录运行：

```bash
python3 scripts/set_version.py               # 同步中英文 README 的当前版本行
python3 scripts/set_version.py 0.15.0        # 设置 VERSION 并同步（下一版本示例）
python3 scripts/set_version.py --check       # 只读检查；漂移时退出码为 1
```

支持 SemVer，包括预发布号、构建后缀，以及可选的 `v` 前缀。
`--check` 始终以 `VERSION` 为准，不能同时指定新版本。
三个受管文件必须已存在。同步时也会将 `VERSION` 规范为版本号加一个 LF 换行；
`--check` 会报告格式不规范。以后新增当前发布版本字段时，须显式加入脚本的标记表和测试。
脚本写入前检查全部版本标记，并保留 README 换行格式。发布说明仍需单独编写；
历史版本、schema/tool 版本、tag 和已签名 manifest 不会被改写。
相关 push、PR CI 和 tag 发布流程都会运行 `--check`，发现漂移即失败。

## License（许可证）

MIT —— 见 `LICENSE` 文件。© 2026 Zhou Peng。

覆盖本公开发布物（shipped 的 skills、hooks、scripts、文档、模板）。跨厂商外审引擎是独立组件、不在本发布内 —— 见[已知局限](#已知局限)。
