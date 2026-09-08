# AQG 自动配置指南（喂给你的 AI agent 执行）

[English](AI_SETUP.md) | 中文

> **怎么用**：把**本文件整篇**粘贴给你当前正在运行的 AQG-supported AI coding client，再说一句
> "按这个把 AQG 配好"。AI 会自主安装 Agent Quality Gates (AQG)，并配置当前客户端匹配 adapter 公开支持的
> skills、hooks 与规则面。本指南默认是 **installed-supported by default**：检测本机已安装且 AQG 正式 supported/full/partial 的桌面端，并为每个命中的 adapter 配置托管能力；unsupported / unknown 不作为默认安装目标。你不用手动编辑任何文件。
>
> 适用：**首次安装** 和 **已装过的幂等 re-sync**（模板更新后重新喂一遍即可对齐，不重复堆叠）。

---

## 给 AI agent 的执行指令

> AI：你正在为用户配置 AQG。**逐步执行，每步验证通过再下一步**；任何一步失败就**停下报告**，不要硬推。
> 用户要求安装或 re-sync AQG，即已授权安装所选客户端的 managed skills、受支持 hooks 和 AQG 规则块。直接静默安装，不再额外询问确认，blocking hooks 也一样。只有用户明确要求且所选 profiles 支持时，才使用 `--no-hooks`。
> 静默指不新增安装确认交互；仍保留 dry-run 检查、备份、验证、失败报告和客户端要求的信任步骤。apply 前先满足下文的来源和 scope 前置条件。
> 全程遵守末尾【边界】。

### 0. 前置

- `git --version` 和 `python3 --version`。**python3 必须 ≥ 3.9**；只有 `python` 或版本过低 → 停下让用户装 python3 ≥3.9。
- 从当前 AI runtime / host context 判断 `CURRENT_CLIENT`，**不要**从配置目录判断：
  - 在 Codex 中运行时，设 `CURRENT_CLIENT=codex`。
  - 在 Claude Code 中运行时，设 `CURRENT_CLIENT=claude-code`。
  - 在 Cursor 中运行时，设 `CURRENT_CLIENT=cursor`。
  - 在 Qoder 家族 host 中运行时，只有能可靠判断精确 runtime profile 才设置 `CURRENT_CLIENT`：`qoder-cli` / `qoder-cli-cn` 是 full profile；`qoder` / `qoder-cn` 是 partial IDE profile。
  - 在 CodeBuddy、WorkBuddy AI、Kimi Code、WorkBuddy、Trae Work、QoderWork 或 QoderWake 中运行时，只有能可靠判断精确 runtime profile 才设置 `CURRENT_CLIENT`：`codebuddy`、`workbuddy-ai`、`kimi-code`、`workbuddy`、`trae-work`、`qoderwork` 或 `qoderwake`。WorkBuddy AI（`workbuddy-ai`）是独立产品，根目录为 `~/.workbuddy-ai`，不与 `workbuddy` 或 `codebuddy` 共享。
  - 在 Pi 中运行时，设 `CURRENT_CLIENT=pi`。
  - 在 Trae 家族 IDE host 中运行时，只有能可靠判断精确 runtime profile 才设置 `CURRENT_CLIENT`：`trae` / `trae-cn` 是 partial IDE profile；`trae-work` / `trae-work-cn` 是 partial Work profile。
  - 在 Zed 中运行时，设 `CURRENT_CLIENT=zed`。
  - 在 Devin CLI 中运行时，设 `CURRENT_CLIENT=devin`。
  - 如果无法可靠判断当前 runtime / host context 或 Qoder profile → **停下询问用户**当前正在运行的客户端/profile 是哪个。不要根据 `~/.claude`、`~/.codex`、`~/.cursor`、`~/.qoder`、`~/.lingma`、`~/.qoder-cn` 或任何其它目录猜测。

### 1. 定位 / 获取 AQG checkout（记为 `AQG_ROOT`）

使用 project / task worktree 之外的持久 checkout。Unix/macOS/Linux 的默认推荐
路径是 `$HOME/.deeppattern/agent-quality-gates`，Windows 的默认推荐路径是
`C:\Users\<user>\.deeppattern\agent-quality-gates`。目录名是 `.deeppattern`，
**不是 `.deeppatternai`**。

按顺序探测，第一个命中的**已存在目录**即用：

1. `$AQG_ROOT`（环境变量，若已设且目录存在）
2. `$HOME/.deeppattern/agent-quality-gates`（开发源仓）
3. `${XDG_DATA_HOME:-$HOME/.local/share}/aqg/the cloud backend`（发布版仓，若存在）

**都没命中** → 需要获取源码。若用户已提供可访问的 AQG 仓库链接，或能确定
仓库地址的文档链接，先解析并展示实际 clone URL，取得用户确认后再 clone 到下面的
持久路径；否则再向用户索取
现有 checkout 本地路径或 AQG 分发仓 git 地址（私有仓需用户有访问权）。

#### 1.1 clone、fetch、checkout 或 pull 前解析安装来源 / checkout ref

如果用户给出 GitHub 文档 URL，先解析仓库来源和文档 URL ref，再触碰 git 状态。
必须保留带斜杠的分支名：对于
`https://github.com/deeppatternai/agent-quality-gates/blob/feature/v0.0.1/AI_SETUP.zh-CN.md`，
`doc_ref` 是 `feature/v0.0.1`（`/blob/` 与文档路径之间的完整内容），
不是裸后缀 `v0.0.1`。

全程使用这三个名称：

- `doc_ref`：文档 URL 中的完整 ref，例如 `feature/v0.0.1`。
- `requested_version`：用户口头显式指定的短版本，例如 `v0.0.1` 或
  `v0.0.2`；如果用户没有额外指定版本则为空。
- `selected_checkout_ref`：实际用于 `git clone --branch`、`git fetch`、
  `git checkout` 和 `git pull --ff-only` 的精确 ref。

选择规则：

1. 如果 `requested_version` 为空，`doc_ref` 是 authoritative：
   `selected_checkout_ref=doc_ref`。
2. 如果 `requested_version` 与 `doc_ref` 的版本后缀一致，`doc_ref`
   仍然 authoritative。不要因为用户说“安装分支：v0.0.1”就把
   `feature/v0.0.1` 退化成裸 `v0.0.1`。
3. 如果用户显式指定的 `requested_version` 与 `doc_ref` 的版本后缀不一致，
   不要直接使用 URL ref；URL 只作为仓库来源和文档路径模板。优先尝试
   `feature/<requested_version>`。
4. fallback 必须 fail-closed：
   - 若 `feature/<requested_version>` 存在，选择它。
   - 若 `feature/<requested_version>` 不存在，但裸 `<requested_version>`
     分支或标签存在，停止并请求用户确认，不要静默降级。
   - 若两者都不存在，停止并报告找不到匹配安装 ref。
5. 只有用户明确说“不要用 `feature/<version>`，checkout 裸 `<version>`”
   时，才允许使用裸版本 ref。

在任何 `git clone`、`git fetch`、`git checkout` 或 `git pull` 前，必须报告
解析结果：

```text
doc_ref=<URL 中的完整 ref，或空>
requested_version=<用户短版本，或空>
selected_checkout_ref=<精确 ref；若阻塞则为空>
selection_reason=<为什么选这个 ref，或为什么停止>
attempted_candidate_refs=[<按顺序检查过的 refs>]
```

示例：

| 示例 | 用户输入 | 解析结果 / 行为 |
|---|---|---|
| A | “重新安装 AQG，链接是 `blob/feature/v0.0.1/AI_SETUP.zh-CN.md`，安装分支：`v0.0.1`。” | `doc_ref=feature/v0.0.1`；`requested_version=v0.0.1`；`selected_checkout_ref=feature/v0.0.1`；reason：用户短标签与 `doc_ref` 版本后缀一致，URL 完整 ref 优先。 |
| B | “安装 `v0.0.2`，链接是 `blob/feature/v0.0.1/AI_SETUP.zh-CN.md`。”且仓库存在 `feature/v0.0.2`。 | `doc_ref=feature/v0.0.1`；`requested_version=v0.0.2`；`selected_checkout_ref=feature/v0.0.2`；reason：用户显式请求更新版本，URL 仅作为仓库来源和文档路径模板。 |
| C | 同 B，但仓库不存在 `feature/v0.0.2`，存在裸 `v0.0.2`。 | 停止并请求确认：`未找到 feature/v0.0.2，但找到 v0.0.2。是否确认使用裸 v0.0.2？` |
| D | 同 B，但 `feature/v0.0.2` 和 `v0.0.2` 都不存在。 | 停止并报告：`attempted_candidate_refs=[feature/v0.0.2, v0.0.2]`；`result=no matching install ref found`。 |

checkout 后、运行 installer 命令前必须强校验：

```bash
git -C "$AQG_ROOT" rev-parse --abbrev-ref HEAD
git -C "$AQG_ROOT" rev-parse --short HEAD
rg "INSTALL_MODE=installed-supported|--installed-supported|multi-client-all" AI_SETUP*.md scripts/install_aqg_clients.py
```

如果用户期望的安装模式是 `installed-supported`，但 checkout 出来的 setup 文档
没有包含该模式，必须停止并报告 checkout ref 与请求的安装来源不匹配。

```bash
AQG_ROOT=$HOME/.deeppattern/agent-quality-gates
mkdir -p "$(dirname "$AQG_ROOT")" && git clone --config core.autocrlf=false --config core.eol=lf --branch "$selected_checkout_ref" <已确认的仓库地址> "$AQG_ROOT"
```

**不要**默认 clone 到当前 workspace、Codex 临时任务目录、project worktree 或
`Documents/Codex/.../work`。

Windows 原生 PowerShell 使用等价的持久路径：

```powershell
$AQG_ROOT = Join-Path $HOME '.deeppattern\agent-quality-gates'
New-Item -ItemType Directory -Force -Path (Split-Path $AQG_ROOT) | Out-Null
git clone --config core.autocrlf=false --config core.eol=lf --branch $selected_checkout_ref <已确认的仓库地址> $AQG_ROOT
```

> 本指南刻意**不硬写分发仓地址**（开发源 `agent-quality-gates` 与发布版 `the cloud backend` 会随阶段变）。

**命中已存在的 git checkout** → 先解析并报告 `selected_checkout_ref`，再 fetch、
checkout 这个精确 ref；只有当它是可 fast-forward 的分支时，才用
`git -C "$AQG_ROOT" pull --ff-only` 更新。
失败（diverged / dirty / 不是 git 仓库，比如解压的 tarball）→ **别强推**，停下把情况告诉用户；**仅当用户明确同意"就用当前 checkout"时才继续**，否则退出等用户处理。

**跑任何脚本前，先亮出并让用户过目**（防止 supply-chain：你正要执行这个目录里的代码）：

```bash
echo "AQG_ROOT=$AQG_ROOT"; git -C "$AQG_ROOT" remote -v; git -C "$AQG_ROOT" rev-parse --abbrev-ref HEAD; git -C "$AQG_ROOT" rev-parse --short HEAD
rg "INSTALL_MODE=installed-supported|--installed-supported|multi-client-all" "$AQG_ROOT"/AI_SETUP*.md "$AQG_ROOT"/scripts/install_aqg_clients.py
```

非标准路径（用户自定义 / env 指定）→ 让用户确认这是可信 checkout 再继续。

### 1.5 解析 `SUPPORTED_CLIENTS` 并选择安装目标

先从 AQG checkout 内的权威来源构造 `SUPPORTED_CLIENTS`，再检测本地客户端配置目录，只选择 `support_status` 为 `supported`、`full` 或 `partial` 的本地已安装 adapter。这里是默认 **installed-supported** 流程：安装所有本地命中的 supported/full/partial 桌面端，跳过 unsupported / unknown，并报告检测依据。

权威支持来源按顺序使用：

1. 若当前 checkout 有 `scripts/aqg_client_registry.py`，以它作为 supported-client registry 和 adapter contract。
2. 若缺少该 registry，只能以 **AQG 已落地 installer / adapter / rules template** 加上 **AQG 文档明确标为 supported 的客户端**为准。
3. **不要**根据配置目录、相似产品名、或另一个客户端的 schema 推断支持。

下表不是永久支持 registry；它只是记录当前安装分支。只有上面的证据已确认该客户端在当前 checkout 中受支持后，才能使用对应分支：

| client_id | 必要 AQG 证据 | 安装分支 |
|---|---|---|
| `claude-code` | Claude Code agent pack installer (`agent-packs/claude-code/install.sh`)、Claude Code skill / hook surface (`agent-packs/claude-code/`)、Claude rules template (`examples/aqg-claude-rules.example.md`)，以及仓库文档明确标记 Claude Code supported | Claude Code |
| `codex` | Codex installer (`scripts/install.sh`)、Codex hook installer (`scripts/install_aqg_codex_hooks.py`)、Codex skill surface (`skills/`)、Codex rules template (`examples/aqg-codex-agents.example.md`)，以及仓库文档明确标记 Codex supported | Codex |
| `cursor` | Cursor support installer (`scripts/install_cursor_support.py`)、该 installer 托管的 Cursor hook / rule surface，以及仓库文档明确标记 Cursor supported | Cursor |
| `workbuddy` / `workbuddy-ai` / `codebuddy` / `trae-work` / `kimi-work` / `kimi-code` / `qoderwork` / `qoderwake` | Work/code client installer (`scripts/install_aqg_work_clients.py`)、按 `docs/client-support-matrix.zh-CN.md` 声明的 support report / rules / skills / MCP / hook surfaces，以及 registry `support_status`。`workbuddy-ai` 是独立的 WorkBuddy AI Desktop profile（`~/.workbuddy-ai`，bundle id `com.workbuddy.workbuddy-ai`），不与 `workbuddy` 或 `codebuddy` 共享根目录。 | Work/Code clients |
| `qoder-cli` / `qoder-cli-cn` | Qoder support installer (`scripts/install_aqg_qoder.py`)、Qoder agent pack / hook surface (`agent-packs/qoder/`)、installer 托管的 Qoder CLI rule surfaces，以及仓库文档把 CLI profiles 标为 full | Qoder CLI |
| `qoder` / `qoder-cn` | Qoder support installer (`scripts/install_aqg_qoder.py`)、Qoder agent pack / hook surface (`agent-packs/qoder/`)、精确 macOS 产品身份（`Qoder.app` / `com.qoder.app` 或 `Qoder IDE.app` / `com.qoder.ide`；`Qoder CN.app` / `com.qodercn.app` 或 `Qoder CN IDE.app` / `com.aliyun.lingma.ide`），以及 registry evidence 将 IDE profiles 标为 partial | Qoder Desktop |
| `trae` / `trae-cn` | Agent-client support installer (`scripts/install_aqg_agent_clients.py`)、hook adapter (`scripts/agent_client_aqg_hook.py`)、Trae 官方 Skills/Rules/MCP/Hooks 证据，以及 `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` 将 IDE profiles 标为 partial | Trae IDE |
| `trae-work-cn` | Agent-client support installer (`scripts/install_aqg_agent_clients.py`)、Trae Work Skills/Rules/MCP 证据，以及 `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` 因 lifecycle-hook schema 未验证将 Work CN 标为 partial | Trae Work CN |
| `zed` | Agent-client support installer (`scripts/install_aqg_agent_clients.py`)、Zed Skills/Instructions/MCP 证据，以及 `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` 因 lifecycle hooks 不可用将 Zed 标为 partial | Zed |
| `devin` | Agent-client support installer (`scripts/install_aqg_agent_clients.py`)、hook adapter (`scripts/agent_client_aqg_hook.py`)、Devin Rules/Skills/MCP/Hooks 证据，以及 `docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md` 因缺少压缩前 PreCompact 将 Devin 标为 partial | Devin CLI |
| `pi` | Pi support installer (`scripts/install_aqg_pi.py`)、Pi hook runner (`scripts/pi_aqg_hook.py`)、Pi 官方 Skills/Extensions 证据，以及 `docs/client-support-matrix.zh-CN.md` 因 MCP/connectors 不支持和 closeout/WIP save 依赖 extension 生命周期投递将 Pi 标为 partial | Pi |

已知非安装目标状态：

| client_id | 状态 | 动作 |
|---|---|---|
| `qoder` / `qoder-cn` | partial | 检测到任一合法的精确 macOS bundle identity 时纳入默认 installed-supported 目标集合；同一 profile 的多个 alias 只选择一次。报告缺少 `SessionStart`、`PreCompact` 和 WIP save/recover coverage，并说明 AQG 证据来源。 |
| `workbuddy` / `trae-work` / `kimi-work` / `kimi-code` / `qoderwork` / `qoderwake` | partial | 本地检测到时纳入默认 installed-supported 目标集合；按 `docs/client-support-matrix.zh-CN.md` 报告功能级降级。`kimi-work` 指 Kimi Work Desktop / Kimi Desktop 本地 Daimon skills root；`kimi-code` 指 Kimi Code CLI 的 `~/.kimi-code`。 |
| `trae` / `trae-cn` | partial | 本地检测到时纳入；报告缺少 `PreCompact`、WIP save 只能在 Stop 时执行，以及 User Rules 由 UI 管理。 |
| `trae-work-cn` | partial | 本地检测到时纳入；报告 Work lifecycle hooks 未验证，因此只托管 skills / project rules。 |
| `zed` | partial | 本地检测到时纳入；报告 lifecycle hooks 不可用，因此不安装机械 hook gates。 |
| `devin` | partial | 本地检测到时纳入；报告缺少压缩前 PreCompact，WIP save 只能在 Stop 时执行。 |
| `pi` | partial | 本地检测到时纳入；报告 MCP/connectors 不支持与 lifecycle/trust caveat。 |

只有当 AQG checkout 已经包含正式 adapter / installer / rules template / capability evidence，并且有 supported/full/partial 状态声明时，才能添加未来客户端行。manual-only、experimental、unsupported 或 unknown 客户端**不是**本指南的安装目标。

选择规则：

- 默认目标集合：运行 `scripts/install_aqg_clients.py --installed-supported`，以 wrapper plan 作为 `SELECTED_CLIENTS`。
- 安装和配置 `SELECTED_CLIENTS` 中的每个客户端；不要收窄成只装 `CURRENT_CLIENT`。
- 如果没有检测到任何本地已安装的 supported/full/partial 客户端，写入前停止，并报告空集合与 AQG 证据来源。
- unsupported / unknown 客户端即使配置目录存在，也不是默认安装目标。
- 配置每个已选客户端拥有的规则面：Codex -> `AGENTS.md`，Claude Code -> `CLAUDE.md`，Cursor/Qoder CLI/work-code clients -> 由对应 installer 和 scope 托管的规则面。
- project scope 仍需显式确认：只有用户确认已存在的绝对路径 `PROJECT_ROOT` 后，才使用 `--project-root`。
默认模式是 `INSTALL_MODE=installed-supported`：先 dry-run `scripts/install_aqg_clients.py --installed-supported`，校验计划及所需的来源/scope 输入后，自动 apply 同一组 detected supported/full/partial 客户端，不再让用户确认计划或 hooks。被选中客户端只要支持 lifecycle hooks，就默认安装/刷新 hooks；只有用户明确选择 skills/rules-only 且所有 selected profiles 都支持跳过时才传 `--no-hooks`。如果用户明确要求一次安装 AQG registry 内所有 adapter（不按 support-status label 过滤），才切到 `INSTALL_MODE=multi-client-all` 并使用 `scripts/install_aqg_clients.py --all-registry`；`--all-supported` 保留为同一 registry-all 模式的向后兼容别名。

默认 `installed-supported` 流程：

```bash
# 先 dry-run。这里检测本地 supported/full/partial registry client 配置目录，
# 例如 ~/.codex、~/.claude、~/.cursor、~/.qoder、~/.qoder-cn、
# ~/.trae、~/.trae-cn、~/.trae-work-cn、~/.config/zed、~/.config/devin、~/.pi/agent。
# Kimi Work Desktop / Kimi Desktop Daimon skills root is detected from
# KIMI_WORK_SKILLS_ROOT / KIMI_DESKTOP_SKILLS_ROOT, running Daimon command lines,
# Kimi Desktop logs, .rehomed markers, or Kimi.exe install-drive evidence.
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT"

# 校验 AQG_ROOT、remote、commit、detected/skipped clients、scope 和 commands。
# 前置条件满足后自动 apply，不再额外询问 hooks/计划确认。
# 如果 plan 包含 Qoder IDE partial profiles 等 project-scope adapter，apply 时必须传 --project-root。
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT" --apply
# python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --installed-supported --aqg-root "$AQG_ROOT" --project-root "/absolute/path/to/project" --apply
```
可选 `multi-client-all` 流程：

```bash
# 先 dry-run。这里展开 scripts/aqg_client_registry.py 内的全部 adapter；
# 不会探测 ~/.claude、~/.codex、~/.cursor、~/.qoder、~/.trae、
# ~/.trae-cn、~/.trae-work、~/.trae-work-cn、~/.config/zed、~/.config/devin、~/.pi/agent 或任何其它配置目录。
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry --aqg-root "$AQG_ROOT"

# 校验 AQG_ROOT、remote、commit、clients、scope 和 commands 后，按用户要求的 registry-all 范围自动 apply。
# 当计划包含 Qoder IDE profiles 等 project-scope adapter 时，必须传 --project-root。
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry --aqg-root "$AQG_ROOT" --project-root "/absolute/path/to/project" --apply
```

在 `multi-client-all` 中，要根据 wrapper plan 报告每个被选中的 registry client 及其 support-status label。Partial / unsupported / unknown label 也会被选中，因为这个模式是 registry-all；但在自动 apply 前，必须报告 registry 证据展示出的能力降级或不确定覆盖，不为此停下重复询问安装确认。不要传 `--no-hooks`，除非展示出的 client set 都能支持；Qoder 家族 profiles 当前会 fail closed，因为它的 installer 没有 `--no-hooks` 模式。

### 2. 为 `SELECTED_CLIENTS` 安装 managed client support（幂等）

默认路径是运行 wrapper 为 `SELECTED_CLIENTS` 中每个客户端规划的命令组。下面的单客户端分支是 wrapper 不可用或用户明确只选一个客户端时的 fallback / reference。命令是 repo 内相对路径，无论装在哪个仓都通用。

仅在选择 Cursor 或 Qoder CLI project scope 时，替换并校验 `PROJECT_ROOT`：

```bash
PROJECT_ROOT="/absolute/path/to/project"                 # 仅 project scope：替换为确认后的路径
test -d "$PROJECT_ROOT" || { echo "PROJECT_ROOT does not exist" >&2; exit 2; }
```

若 `CURRENT_CLIENT=codex`：

```bash
# 默认静默安装受支持的 hooks（含 4 个 blocking policy），并修改客户端 hook 配置。
# 只有用户明确选择 skills-only 时才使用 --no-hooks。
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --clients codex --aqg-root "$AQG_ROOT" --apply
```

Codex hook 的落盘位置与实际生效是两件事：

1. AQG Codex installer 默认把 managed definitions 写入 `~/.codex/hooks.json`；
   只有用户明确要 skills-only 时才传 `scripts/install.sh --no-hooks`；自定义
   `--dest` 默认跳过 hooks，除非同时传 `--hooks`。不应期待
   存在 `~/.codex/hooks/` 目录。definitions 通过
   `scripts/run_aqg_codex_hook.py` 这个 Codex wrapper 执行。
2. wrapper 当前复用 `agent-packs/claude-code/hooks/*.sh` 的共享 policy 脚本；
   这是有意的代码复用，不代表在 Codex 中安装或配置 Claude adapter。
3. 成功状态必须分三层报告：installer 已写入 `~/.codex/hooks.json`；Codex
   runtime discovery 已发现 definitions；用户已通过 `/hooks` 审阅并 trust。

`install_aqg_codex_hooks.py --verify` 与 doctor 只能验证 **落盘/on-disk** 的 AQG
definition 和完整性契约；不能证明 Codex Desktop runtime discovery 成功，不能证明
Settings 页面已展示 hooks，也不能替代用户在 `/hooks` 中的 trust。installer 支持时，
Codex 会安装 managed `UserPromptSubmit` handoff-routing hook；AGENTS.md 规则仍是
model-side fallback 和说明。

若 `CURRENT_CLIENT=claude-code`：

```bash
# Claude Code skills + supported hooks（skills 装到 ~/.claude/skills，源是 agent-packs/claude-code/skills，与 Codex 不同）
# 只有用户明确选择 skills-only 时才传 --no-hooks。
# 静默安装：hook pack 含 4 个 blocking gate（secret-scan / memory-write-guard /
# skill-validator / tamper-guard，都带 AQG_AGENT=human-opt-in escape）；想要永不阻塞
# 版改用 settings.warn-only.example.json。
python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --clients claude-code --aqg-root "$AQG_ROOT" --apply
```

若 `CURRENT_CLIENT=cursor`：

```bash
# Cursor —— scope 二选一。默认静默安装 fail-closed preToolUse hook，无需额外确认；
# 只有用户明确要求 skills/rules-only 时才传 --no-hooks。project scope 还会安装
# .cursor/rules/aqg.mdc；User Rules 由 UI 管理。
python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope user --mode link
# python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope project --project-root "$PROJECT_ROOT" --mode link
```

若 `CURRENT_CLIENT` 是 `workbuddy`、`workbuddy-ai`、`codebuddy`、`trae-work`、`kimi-work`、`kimi-code`、`qoderwork` 或 `qoderwake`：

```bash
WORK_CLIENT="$CURRENT_CLIENT"
case "$WORK_CLIENT" in workbuddy|workbuddy-ai|codebuddy|trae-work|kimi-work|kimi-code|qoderwork|qoderwake) ;; *) echo "invalid WORK_CLIENT" >&2; exit 2;; esac

# Work/Code clients 使用 docs/client-support-matrix.zh-CN.md 中的 per-profile support level。
# 降级 profile 只安装官方文档可证明的托管面；只要 skills/rules/MCP 时可传 --no-hooks。
# Kimi boundary: kimi-work = Kimi Work Desktop / Kimi Desktop local Daimon
# daimon-share/daimon/skills root, dynamically discovered and overrideable with
# --skills-root /absolute/path/to/skills. kimi-code = Kimi Code CLI under
# ~/.kimi-code; do not mix these install targets.
python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client "$WORK_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --apply
# python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client "$WORK_CLIENT" --scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT" --mode link --apply
# Kimi Work link installs fall back to copy if symlink/junction creation fails;
# use --mode copy to copy directly, or --mode link --strict-link to fail closed.
# Restart Kimi Work / Kimi Desktop after apply so Daimon reloads AQG skills.
```

若 `CURRENT_CLIENT=trae`、`trae-cn`、`trae-work-cn`、`zed` 或 `devin`：

```bash
AGENT_CLIENT="$CURRENT_CLIENT"
case "$AGENT_CLIENT" in trae|trae-cn|trae-work-cn|zed|devin) ;; *) echo "invalid AGENT_CLIENT" >&2; exit 2;; esac

# Trae/Zed/Devin profiles 使用 docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md。
# 无已验证 lifecycle hooks 的 profile 只安装 skills/rules surface。
python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client "$AGENT_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --apply
# python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client "$AGENT_CLIENT" --scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT" --mode link --apply
```

若 `CURRENT_CLIENT=qoder-cli` 或 `CURRENT_CLIENT=qoder-cli-cn`：

```bash
QODER_CLIENT="$CURRENT_CLIENT"
case "$QODER_CLIENT" in qoder-cli|qoder-cli-cn) ;; *) echo "invalid QODER_CLIENT" >&2; exit 2;; esac

# Qoder CLI 家族 —— 使用已确认的 full profile，scope 二选一。
# Qoder installer 总会管理 hooks，其中含 2 个 blocking preToolUse gate，且没有
# --no-hooks 模式。按所选 profile/scope 直接静默安装。
python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client "$QODER_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --apply
# python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client "$QODER_CLIENT" --scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT" --mode link --apply
```

若 `CURRENT_CLIENT=pi`：

```bash
# Pi —— 安装 skills，并用托管 TypeScript extension 接入官方 lifecycle events。
# 只要 skills/report 时可传 --no-hooks。
python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope user --aqg-root "$AQG_ROOT" --mode link --apply
# python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope project --project-root "$PROJECT_ROOT" --aqg-root "$AQG_ROOT" --mode link --apply
```

然后运行：

```bash
# 健康检查
python3 "$AQG_ROOT/scripts/aqg_doctor.py"
```

`aqg_doctor` 报 **FAIL** → 停下按提示修，别继续。（doctor 校验 skills、基础环境和 Codex 落盘 hook 定义；它不能确认 `/hooks` 信任状态，也**不**校验下一步 CLAUDE.md/AGENTS.md 的规则内容。）

> Claude Code/Codex hook installer 与 Cursor/Qoder CLI/Work-Code/Trae-Zed-Devin/Pi support installer 都提供 `--apply`、`--verify`、`--uninstall`、`--is-installed`；每次必须使用完全相同的客户端/profile 与 scope。`aqg_doctor` 不能替代 Cursor/Qoder CLI/Work-Code/Trae-Zed-Devin/Pi 的 `--verify`。

> Codex：安装后重启，执行 `/hooks` 审阅并信任定义；installer 固化已审阅 checkout、Python 路径和 runner+policy 内容摘要，不依赖 shell 继承 `AQG_ROOT`；路径或 hook 内容更新后需重跑 `--apply` 并审阅变化后的定义 hash。
>
> Claude Code：仍须把 `AQG_ROOT` export 到客户端环境，否则 hooks silent-skip；安装后重启生效。

#### Codex Desktop hook 排障

如果 `~/.codex/hooks.json` 已存在，但 Codex Desktop Settings 显示“未找到钩子”，
先完全退出并重启 Codex Desktop；然后在 Codex 会话中运行 `/hooks`，不要只依赖
Settings 页面。在 `/hooks` 中审阅并 trust hook definitions 后才算生效。若 `/hooks`
仍不显示，报告为 **Codex runtime discovery failure / possible schema drift**，并附上
`~/.codex/hooks.json` 路径和 Codex 版本。

### 3. 装当前客户端的规则块（**关键 —— 常驻投送通道**）

| 客户端 | 目标文件 `$target` | 模板源 `$tmpl`（在 `$AQG_ROOT/`） |
|---|---|---|
| Claude Code | `~/.claude/CLAUDE.md` | `examples/aqg-claude-rules.example.md` |
| Codex | `~/.codex/AGENTS.md` | `examples/aqg-codex-agents.example.md` |

若 `CURRENT_CLIENT` 是 `claude-code` 或 `codex`，只选择对应目标行。不要修改非当前客户端的规则文件。

> Cursor/Qoder CLI/Work-Code/Trae-Zed-Devin clients 不执行这一段，它们的 installer 托管公开支持的规则面。user-scope Cursor 与 Trae User Rules 按官方产品约束留给 UI。

**3.1 装规则块**——每个所选客户端一条命令；备份、幂等、校验都由 installer 负责：

```bash
python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client claude-code --apply --aqg-root "$AQG_ROOT"
python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex        --apply --aqg-root "$AQG_ROOT"
```

只跑 `SELECTED_CLIENTS` 里有的那一行。脚本已保证以下几点，不必再手工复核：

- `$target` 不存在就创建；已存在就**追加**，用户原有内容原样保留。
- 区段由 `<!-- BEGIN AQG rules ... -->` / `<!-- END AQG rules -->` 界定，重跑**只**替换该区段，前后内容都不动。
- `<AQG_ROOT>` 会被解析成本次安装所用的 checkout 路径。
- 覆盖前把原文件存入中央备份库（`scripts/_aqg_backup.py`），并打印 run 目录。
- 拒绝穿 symlink 的**规则文件**写入；遇到只剩一半的区段直接拒绝，不猜它到哪结束。若是**客户端目录**本身是 symlink（dotfiles 布局），按设计跟随。

**3.2 处理历史遗留的无标记块**——在这个脚本之前安装过的人，文件里那一段没有 marker（是 agent 按旧手工流程贴进去的）。这种情况 `--apply` 会**拒绝**，而不是再追加一份：

```
ERROR: found an unmarked legacy AQG block (no BEGIN/END markers) ...
```

手工把那一段从 `$target` 删掉——边界是从 `Agent Quality Gates (AQG) engineering discipline` 标题起，到下一个同级或更高级标题之前（没有就到文件末尾）——然后重跑 3.1。**用户自己写的子段一律保留**；拿不准是不是用户自定义的，就保留并问用户。

**3.3 确认**——用 installer 自带的检查，不要手写 grep：

```bash
python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client <claude-code|codex> --verify --aqg-root "$AQG_ROOT"
```

`--verify` 会把已安装区段与随仓模板逐字节比对，所以旧版本写的块、或被手工改过的块也会被抓出来。这里非零退出意味着块缺失、过期或被改过——如实报告，别糊弄过去。

### 4. 收尾验证

```bash
python3 "$AQG_ROOT/scripts/aqg_doctor.py"     # 再跑一次；同时验证 Codex 落盘 hook 定义
python3 "$AQG_ROOT/scripts/install_cursor_support.py" --verify --scope user --mode link  # 若选择了 Cursor user scope
python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client "$WORK_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --verify  # 若选择了 Work/Code profile
python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client "$QODER_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --verify  # 若选择了 Qoder CLI user scope
python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client "$AGENT_CLIENT" --scope user --aqg-root "$AQG_ROOT" --mode link --verify  # 若选择了 Trae/Zed/Devin profile
python3 "$AQG_ROOT/scripts/install_aqg_pi.py" --scope user --aqg-root "$AQG_ROOT" --mode link --verify  # 若选择了 Pi user scope
```

project scope 验证时重复 `--apply` 使用的 `--project-root "$PROJECT_ROOT"` 参数。任何验证失败都停止流程。以后检查或移除 managed install，使用同一组参数配 `--is-installed` 或 `--uninstall`。

对 Cursor/Qoder CLI/Work-Code/Trae-Zed-Devin/Pi clients，还要确认其 `--verify` 输出覆盖该 profile 声明的托管面；不要套用 step 3 的手工规则段检查。

规则与 hooks 在 client/session 启动时加载。重启每个已配置客户端；Codex 还须通过 `/hooks` 审阅并信任定义。

### 5. 报告

报告以下内容：检测到的 `CURRENT_CLIENT`；本地检测到的 `SELECTED_CLIENTS`；每个已选客户端的 AQG 支持证据来源；被跳过的 unsupported / unknown 本地客户端；适用时报告所选 scope；装了几个 skill；托管了哪些 hooks/rules；Claude/Codex 规则段是**新增**还是 **re-sync**（3.3 的 a/b/c）；installer/手工编辑报告的全部备份路径；验证结果；以及需要重启生效。

如果其它客户端目录存在但未配置，只有当它们的 AQG registry 状态是 unsupported / unknown，或超出已确认 scope 时，才报告这是 intentional。如果 `CURRENT_CLIENT` 不被 AQG 正式支持，报告 `unsupported` / `unknown` 并引用 AQG 证据来源，同时仍安装其它本地命中的 supported/full/partial 客户端。

---

## 边界（AI 必须遵守）

- 只动 `CLAUDE.md` / `AGENTS.md` 的 **AQG 段**和所选 managed installer 明确拥有的路径；不碰用户其它配置 / production / secrets / branch protection。
- 默认配置所有本地检测到的 supported/full/partial adapter；不要因为存在 `~/.claude`、`~/.codex`、`~/.cursor`、`~/.qoder`、`~/.lingma`、`~/.qoder-cn`、`~/.pi/agent` 或其它配置目录就声称支持；不要把一个客户端的 schema 套到另一个客户端。
- 新客户端支持声明必须有已落地 AQG adapter / installer / rules template / capability evidence 支撑。
- **写规则文件前必备份、写后校验段外零变更、不过即回滚**（3.1 / 3.4）；case (c) 拿不准的子段**保留并问**，绝不删。
- 安装/re-sync 请求已授权所选 managed skills、受支持 hooks（含 blocking gate）和 AQG 规则块，这些步骤不再重复征求同意。保留来源/scope 检查和客户端要求的信任步骤；destructive / irreversible 仍需显式授权。

---

## 维护 / 分发说明

- **本文件是 AQG 分发包的一部分**。`examples/` 模板更新后，已配置的用户**重新把本文件喂给 AI** 即可幂等 re-sync（情况 b/c）。
- **本文件在仓库整体搬迁到另一个仓时必须一起带上**（连同 `examples/aqg-*.example.md`、`scripts/install.sh`、`agent-packs/claude-code/install.sh`、`scripts/install_aqg_hooks.py`、`scripts/install_aqg_codex_hooks.py`、`scripts/install_cursor_support.py`、`scripts/install_aqg_qoder.py`、`scripts/install_aqg_work_clients.py`、`scripts/install_aqg_agent_clients.py`、`scripts/install_aqg_pi.py`、`scripts/aqg_client_registry.py`、`scripts/install_aqg_clients.py`、`scripts/run_aqg_codex_hook.py`、`scripts/cursor_aqg_hook.py`、`scripts/agent_client_aqg_hook.py`、`scripts/pi_aqg_hook.py`、`agent-packs/claude-code/hooks/`、`agent-packs/qoder/hooks/`、`scripts/aqg_doctor.py`、`docs/AUDIT_DECISION_MODEL.md`）—— 它们是"配置可自举"的最小集合。
- 本指南刻意**不硬写分发仓地址**（分发仓地址会随阶段变），靠 step 1 探测 / 向用户索取。
