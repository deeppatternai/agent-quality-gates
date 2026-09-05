<!-- id: 2026-08-23-audit-trigger-delivery-plan-v3-a1 -->
# 审计触发率改进方案 v3（历史版本 · 已被 v4 取代）

- id: 2026-08-23-audit-trigger-delivery-plan-v3-a1
- date: 2026-08-23
- author: agent (session 4a4e5c64), Owner-reviewed
- status: **superseded by** `docs/discussion/2026-08-24-audit-trigger-delivery-plan-v4-a1.md`
- 保留理由: v4 只保留了批次 C（判定机械化 + 拦截）的摘要；C1/C1a/C1b/C2/C3 的完整设计分析只存在于本文件。需要推进 C 时回查此处。
- 审计谱系: v1 `aud_PyHpo7a0GA6U-kC0` → v2 `aud_fAh49XZyVQmyelui` → v3 `aud_AXRziKw2HJKTn_DU`（三轮均 4/4 `has-serious-issues`）
- 正文自此以下为原文，未经改动。

---

# AQG 审计触发率改进方案 v3（待执行）

- **基线**：AQG `79f5df1`（main，0/0，工作区干净）
- **范围**：仅 AQG 仓。DE / hub 侧改动已另行交付。
- **审计历史**：v1 `aud_PyHpo7a0GA6U-kC0`（4/4 has-serious-issues，22 findings）→ v2 `aud_fAh49XZyVQmyelui`（4/4 has-serious-issues，25 findings，但一致评价为"materially improves v1"）→ 本 v3。
- **状态**：待审计、待批准。**尚未执行任何代码改动。**

---

## 0. v2 审计裁决台账

裁决前对每条可检验主张做了**实测复现**（审计员运行在空目录，无法验证仓内事实）。

### 接受并已修正

| # | finding | 实测证据 | v3 处置 |
|---|---|---|---|
| 1 | opus f3 (blocking)：`git status --porcelain` 默认折叠未跟踪目录 | 新建 `nd/sub/x.py` + `nd/top.py` → 默认输出仅 `?? nd/`；加 `-uall` 才列出两个文件 | **B2 改用 `-uall`** |
| 2 | google f4 + v4-pro f1：已暂存内容不可见 | `git add` 后 `git status` 显示 `M  t.py`，但 `git diff` **0 行**，`git diff --cached` 7 行 | **B2 内容侧改用 `git diff HEAD`**（实测同时覆盖已暂存 + 未暂存） |
| 3 | opus f4 + google f2 (blocking) + v4-pro f4：B3a 作用域条款没有机械作用 | `AQG_ROOT` 全机器可解析；skills 装在 `~/.claude/skills/` 全局 → 条件恒为真 | **B3a 重写为仓级机械判定**，见 B3a-0 |
| 4 | opus f1 (blocking) + v4-pro f2：M 循环依赖 | M1 写"在 B2 落地时同步记录"，但 M 必须先于 B | **M1 改挂已生效的 Stop hook**（`wip_checkpoint_save.sh` 已读工作区含 untracked 并按 tree hash 去重） |
| 5 | v4-pro f6 (blocking)：A1 验收不可能达成 | v3 附录自陈基线有 2 条预先失败，A1 却写"现有测试全绿" —— **文档自相矛盾** | **全仓验收统一改为"这两条之外全绿"** |
| 6 | v4-pro f7 (blocking)：门禁继承 fail-open 超时 | v2 让门禁复用提醒的"超时静默放行" | **拆分策略**：提醒 fail-open，门禁 fail-closed |
| 7 | opus f5 (blocking)：R1 单一渲染覆盖不到 C1 | R1 渲染的是**散文句子**，C1 需要的是**路径匹配规则** —— 两种产物 | **R1 缩窄到 B1/B3；C1 的映射表单列为 C1a** |
| 8 | opus f7：C3 的写前检查会在所有仓中止 | 新 `git init` 仓 `.git/hooks` 有 **14 个文件**（全是 `.sample`）→ "非空即中止"处处中止 | **只统计非 `.sample` 项**（新仓为 0） |
| 9 | opus f6：state_hash 绑全工作区导致活锁 | 任何无关编辑都会使记录失效 | **绑定到 Gate A 命中的文件子集**，非全工作区 |
| 10 | opus f8 + v4-pro f5：R2 自指 | 断言"载体 == R1 输出"抓不出 R1 自身错误 | **加金样本 + 负样本夹具**，见 R2 |
| 11 | opus f11 + google f5：读取任意未跟踪文件内容 / 埋点隐私 | v2 未设排除规则 | **B2 加内容读取边界；M1 只记计数与哈希，不记路径与内容** |
| 12 | v4-pro f9：Owner 裁决 #1 声明为前置但未进依赖表 | 属实 | **进表** |
| 13 | v4-pro f8：E 的 PROACTIVELY 边界可能自相矛盾 | "改为祈使句"可能被读成"加 PROACTIVELY" | **明确区分**：祈使语气 ≠ `PROACTIVELY` 令牌 |

### 部分接受

| # | finding | 裁决 |
|---|---|---|
| 14 | google f1 (blocking)：C3 chaining 机制阻止安装 | 接受"不能因存在旧 hook 就一律中止"；但保留"不得静默顶替"。见 C3 的三态处理 |
| 15 | google f3 + opus f6：审计记录可被绕过 | v2 已声明不解决对抗性伪造。v3 **进一步收窄适用声明**：记录是防遗漏的书签，不是安全控制，且必须写进 policy 边界声明 |
| 16 | opus f10：性能结论由单次热缓存测量外推 | 接受。v3 **不再声称性能无问题**，改为"必须有前置检查 + 超时"，并把实测数据降级为单点参考 |

### 拒绝（含一条上轮被质疑、现已补齐证据）

| # | finding | 裁决与依据 |
|---|---|---|
| 17 | opus f2 (blocking)：v1 f8 的拒绝依据不等价 | **上轮拒绝证据不足，本轮已补齐，结论维持**：三个核心 host 的注入通道均已逐一验证 —— Claude Code `hookSpecificOutput.additionalContext`；**Codex `scripts/run_aqg_codex_hook.py:266-272`** 对 `SessionStart` / `UserPromptSubmit` 发 `hookSpecificOutput.additionalContext`；**Cursor `scripts/cursor_aqg_hook.py:320` `_session_start()`** 发 `{"additional_context": ...}`。上轮我只验证了"事件存在"，审计员指出这不等价于"注入可用" —— 该批评正确，证据现已补齐 |
| 18 | opus f9：DE 不 clobber 的结论只对当前代码成立 | 接受为**限制声明**而非缺陷：`codex_routing.py:61` `_splice()` 现在按 marker 保留外部内容；v3 增加一条**跨仓约定**要求（见 B3d），而不是假设它永不变 |

---

## 关于本方案性质的再声明

v2 已承认、v3 维持并强化：

> **批次 A/B/R 是概率性改进，不是保证。只有 C（机械判定 + 拦截）能闭环。**

审计员两轮都指出：最高优先级交付物仍是"更多模型可读文本"。**v3 不再试图辩解这一点，而是把它变成可证伪的**：

1. **Owner 裁决 #1（拦截 vs 提醒）进入依赖表，是 B 的硬前置**
2. **M 批次先行采基线**，且**不依赖 B**（循环已解，见 M1）
3. 若 Owner 选择不拦截，方案上限即为概率性改进 —— M3 的数值门槛就是判定它是否奏效的唯一标准

---

## 执行顺序总览（v3）

| 批次 | 主题 | 风险 | 依赖 |
|---|---|---|---|
| **M** | 测量与埋点 | 低 | **无**（v2 的循环已解） |
| **A** | 文档与措辞修正 | 极低 | 无 |
| **R** | Gate A 句子单一渲染 | 低 | A |
| **B** | 投递机制 | 中 | R；**Owner 裁决 #1**；B3 另需 DE 侧 marker 约定 |
| **C** | 判定机械化 + 执行点 | 中高 | B2、R、**C1a（Owner 审核）**、Owner 裁决 #1 |
| **D** | 数据模型 | 中 | B3 |
| **E** | description 改写 | 中 | A + B + M |

---

# 批次 M —— 测量与埋点（先行，已解循环）

## M1. 未审计敏感改动计数器 —— **挂已生效的 Stop hook**

> **v2 循环依赖已解**（opus f1 / v4-pro f2）。v2 把 M1 定义为"在 B2 的读取器落地时同步记录"，但 M 必须先于 B。

**v3 做法**：挂到**已经在跑**的 `agent-packs/claude-code/hooks/wip_checkpoint_save.sh`（Stop / PreCompact）。该 hook 已经在读工作区（**含 untracked**）并按 tree hash 去重 —— 检测部分现成，M1 只需在其旁边追加一次只读判定与计数。**不依赖 B2 的任何改动。**

**记录内容（隐私受限，回应 opus f11 / google f5）**：

| 记录 | 不记录 |
|---|---|
| Gate A/B 命中的**类别名** | 文件路径 |
| 命中文件**数量** | 文件内容或片段 |
| `state_hash`（不可逆） | 明文 diff |
| 是否存在匹配的审计记录 | 仓名、分支名、用户标识 |

**存储**：仅本机，不外传。

## M2. 采集变更前基线
执行批次 B 之前，用 M1 跑一个固定窗口（建议 ≥ 2 周真实使用）。**没有基线就不执行 B。**

## M3. 数值门槛
Owner 给出门槛（例：未审计的 Gate A 改动占比从 X% 降到 Y%）。低于门槛即判定 A/B 路线失败，转入 C 的拦截方案。

---

# 批次 A —— 文档与措辞修正

## A1. router docstring 与实现矛盾
**位置**：`skills/aqg-phase-transition/scripts/aqg_phase_router.py:9-10`
docstring 写 `TESTS × stakes → fast/standard/deep`，实现（第 44 行 `DEPTH_BY_STAKES`）为 phase-independent 的 `skip/standard/deep`；同文件 36-38 行注释已说明旧表因 *"contradicted the policy's rung 1"* 被移除。且 `Phase × Stakes` 正是 `aqg_doctor.py` 的 `RETIRED_RULES_MARKERS` 判定 rules block 过期的三个标记之一。

**验收（v3 修正，回应 v4-pro f6）**：`grep -c "Phase × Stakes\|TESTS × stakes"` == 0；**测试结果与基线一致 —— 即 `tests/test_aqg_skill_install.py` 的两条预先失败之外全绿**（不得写"全绿"，基线本就不全绿）。

## A2. `aqg-security-review` 深度与 Gate A 冲突
`skills/aqg-security-review/SKILL.md:83` 建议 `mode=standard`，但其触发面即 Gate A 清单（policy 判 `deep`，"and it wins"）。**改动**：改为引用 policy，不写死深度值。

## A3. `aqg-test-quality-review` 深度静默
全文不提深度 → 静默落到 `/audit` 默认 `standard`。**改动**：加一句深度来源。

## A4. `aqg-multi-review` 未声明的前置依赖
`mode=<phase-transition recommended>` 依赖 phase-transition 已被调用。**改动**：补降级路径 —— 无结论则按 policy 判定。

**A 批次统一验收**：与基线一致（两条预先失败之外全绿）。

---

# 批次 R —— Gate A 句子的单一渲染（**范围已缩窄**）

> **v2 越界已修正**（opus f5）。R1 渲染的是给人和模型读的**散文句子**；C1 需要的是**路径匹配规则**。两者是不同产物，不能由同一个渲染器产出。**R 只负责句子。**

## R1. 句子渲染器（仅供 B1 / B3）
解析 `docs/policies/audit-trigger.md` 的 `<!-- gate-a-tokens: ... -->` 标记，渲染出唯一一句 Gate A 文本。**B1 与 B3 emit 同一字符串**，不得各自手写。

**不适用于 C1** —— C1 的判据结构见 C1a。

## R2. 守卫测试（**解自指**，回应 opus f8 / v4-pro f5）

v2 的断言是"载体文本 == R1 输出"，这抓不出 R1 自身的错误。v3 三层：

1. **等价断言**：每个载体中的 Gate A 句子逐字符 == R1 输出
2. **金样本**：R1 的输出与一份**签入仓库、需人工评审**的 golden 文件比对 —— 渲染器改了必须有人看过
3. **负样本夹具**：把 v1 的错误草稿（写 `spanning repositories` 而非 `cross-repo`）作为 fixture 喂入，**断言必须 FAIL**

现有 `test_audit_gate_sensitivity_list_does_not_drift_between_adapters`（`tests/behavior/test_cursor_support.py:693`）是词边界正则包含检查（第 716 行），其 docstring 自陈 *"asserts the CATEGORY TOKENS only... a wording difference would still pass"*。R2 在其之上叠加，不替换。

4. **反向断言**：载体文本中不得出现非 policy 来源的深度词（`standard` / `deep`）——正是 A2 那类缺陷用包含检查抓不到的。

---

# 批次 B —— 投递机制

## B1. SessionStart preflight 注入纪律摘要

**通道已在三个核心 host 逐一验证**（补齐 v2 的证据缺口）：

| host | 机制 | 证据 |
|---|---|---|
| Claude Code | `hookSpecificOutput.additionalContext` | `sessionstart_preflight.sh`，已验证注入 |
| **Codex** | `hookSpecificOutput.additionalContext` | `scripts/run_aqg_codex_hook.py:266-272`，对 `SessionStart`/`UserPromptSubmit` |
| **Cursor** | `{"additional_context": ...}` | `scripts/cursor_aqg_hook.py:320` `_session_start()` |

**实现代价**：preflight 内部把 `parts` 拼成 `summary` 再 emit；追加纪律段落即往 `summary` 再拼一段（现有 cwd-scope 提示就是这么做的）。

**改动**：追加 R1 渲染的句子 + 跳过条款 + 入口指引 + 已解析的 policy 绝对路径（解析失败输出显式 `UNRESOLVED`）。

**边界**：SessionStart 属**提醒**路径 → fail-open（沿用现有 warn-only）。

## B2. 工作区读取器（**两处盲区已修正**）

> v1 用 `git diff` → 对新建文件失明。v2 改 `git status --porcelain` → **仍有两处盲区**，均已实测复现。

**v3 的命令对（实测验证）**：

```
文件集：git status --porcelain -uall
内容：  git diff HEAD
```

实测（暂存 + 未暂存 + 嵌套未跟踪同时存在）：

```
git status --porcelain -uall  →  MM t.py
                                 ?? nd/sub/x.py     ← 未折叠
git diff HEAD --stat          →  t.py | 3 ++-      ← 暂存与未暂存都在
```

对比 v2 的错误：`--porcelain` 默认输出 `?? nd/`（**目录折叠**，Gate A 路径匹配失效）；`git diff` 不带 `HEAD` 时对已暂存内容返回 **0 行**。

**已知且明确声明的剩余盲区**：

| 场景 | 行为 | 处置 |
|---|---|---|
| 被 `.gitignore` 忽略的文件 | 两条命令均不可见（需 `--ignored`） | **不纳入** —— 纳入会引入大量构建产物噪声。写入 policy 边界声明 |
| 非 git 目录 | 两条命令均报错 | **静默退出**，并在 M1 计数为"不可观测会话" |

**内容读取边界（回应 opus f11）**：未跟踪文件的内容读取必须设 —— 单文件大小上限、二进制跳过、路径数量上限、超时。**读取内容仅用于本地匹配，不落盘、不外传。**

**性能（v3 不再声称无问题，回应 opus f10）**：本仓 659 文件单次热缓存实测 `--porcelain` 0.018s / `diff --name-only` 0.016s —— 这是**单点参考**，不能外推到大型 monorepo 或冷缓存。因此**强制要求**：

- **保留并扩充 matcher**（加 `Bash`；Codex 侧加其 shell 面），不得移除
- 调 git 之前先做轻量前置检查（工作区 mtime / 上次判定后是否有写操作），无变化直接退出
- 超时保护

**超时策略分叉（回应 v4-pro f7）**：

| 路径 | 超时行为 |
|---|---|
| **提醒**（PostToolUse / SessionStart / Stop） | fail-open：静默放行 |
| **门禁**（C3 pre-commit） | **fail-closed**：非零退出并说明原因；不得因超时放行 |

**验收**：嵌套未跟踪文件 → 提醒产生；已暂存改动 → 内容可见；修改已跟踪文件 → 提醒产生；Edit/Write 路径回归不变；同一状态两次 → 不重复；非 git 目录 → 静默退出；大仓超时 → 提醒放行 / 门禁拦截。

## B3. 全局 rules 块

### B3a-0. **先定义仓级标识**（新增，v2 缺失的前提）

v2 的作用域条款判的是 `AQG_ROOT` 可解析 + skills 已安装 —— **两者都是机器级事实，在装了 AQG 的机器上恒为真**，等于没有作用域。而且它把判断交给模型，正是方案声明为不可靠的机制。

**核实**：AQG **目前没有可自动发现的仓级标识**。
- `.aqg/` 由 skill 运行时创建（`mkdir -p .aqg/code-construction`）并写入 `.gitignore` —— **它在第一次用过 skill 之后才存在，用它判断"要不要用 skill"是循环的**
- `quality-gates.json` 是 per-project 配置，但由 `run_quality_gates.py --config <显式路径>` 读取，**无根目录自动发现约定**

**B3a-0 的改动**：确立一个**仓级 opt-in 标识**并实现自动发现（建议：仓根的 `quality-gates.json`，补上根目录发现约定）。**这是 B3a 的前置。**

### B3a. 作用域由机器判定，不由模型判定

块内文字仍写明适用条件（供人阅读），但**实际作用域由 hook / 安装器机械判定**：SessionStart 与 PostToolUse 在**仓级标识不存在时直接静默退出**，不注入任何纪律文本。

于是即使全局块存在，在无关仓库里也**不会有任何 AQG 内容进入模型上下文** —— blast radius 由代码控制，不由模型自觉控制。

块内容（约 15 行）：R1 渲染的 Gate A 句子 + 跳过条款 + 入口指引 + **已解析的绝对路径**（v1/v2 草稿里的 `<AQG_ROOT>` 字面占位符必须替换，解析失败写 `UNRESOLVED`）。

**同时修订现有模板**：`examples/aqg-claude-rules.example.md:21` 的无条件 "mandatorily invoke AQG's 16 skills" 是本缺陷源头，一并加作用域限定语。

### B3b. marker-less 文件：追加，不拒绝

| 情况 | 行为 |
|---|---|
| 文件不存在 | 创建并写入 |
| 存在、无 AQG marker | **末尾追加**（满足"marker 外一字节不碰"） |
| 存在、有 marker、内容一致 | 不动 |
| 存在、有 marker、内容不同 | 备份后仅替换 marker 之间 |
| 有 marker 但畸形/重复 | **拒绝并报错**（唯一的拒绝情形） |

**失败降级**：单客户端 rules 写入失败只记警告并汇总，不得非零退出中断 `install_aqg_clients.py` 的多客户端流程（PR #83 已建立的逐客户端失败隔离契约）。

### B3c. 卸载路径
新增验收：移除 AQG marker 块后，文件逐字节等于「原文件减去该块」，且 DE 的块完好。

### B3d. 与 DE 共存 + **跨仓约定**（回应 opus f9）
已核实 `codex_routing.py:61` `_splice()` 只统计自身 marker、marker 外逐字保留、自身出现 >1 块时 fail-closed。**当前共存安全。**

但"当前安全"不等于"永远安全"。v3 增加一条**跨仓约定**：两侧安装器都承诺**按 marker 作用域读改写**，不整文件重渲染；该约定写入双方文档，并在 AQG 侧加一条检测 —— 写入后若发现自己的 marker 消失，报告而非静默重写。

---

# 批次 C —— 判定机械化 + 执行点

## C1a. Gate A 类别 → 路径/内容匹配规则表（**新增，从 R 分离**）

policy 的 `gate-a-tokens` 是**类别名**（`install integrity`、`auth`…），不是匹配规则。类别到规则的映射是一层**解释**，policy 未提供，R1 也无法产出。

**实测反例**（本仓，naive 子串匹配）：

| 类别 | naive 命中 | 实际性质 |
|---|---|---|
| `install` | `docs/ECOSYSTEM_MUST_INSTALL.md` | 文档 |
| `auth` | `benchmarks/.../ws8-authorization-*.example.json` | 测试夹具 |
| `schema` | `contracts/ledger/fixtures/**/*.json` | 测试夹具 |

naive 匹配会把文档和夹具判成 Gate A → **过度触发**，正是 2026-08-11 要治的病。

**C1a 的产出**：一份**显式的映射表**（类别 → include 模式 + exclude 模式），要求：
- **Owner 审核**后方可生效
- 与 policy 同源维护，有守卫测试断言每个 `gate-a-token` 都有对应条目（不得漏配）
- exclude 至少覆盖：文档目录、测试夹具目录、benchmark 数据

## C1. 判定器 `scripts/aqg_audit_decide.py`
**输入**：B2 的工作区状态。**输出**：`{"decision": "skip|standard|deep", "reasons": [...], "matched": [...], "scope_hash": "..."}`

**判定**：套用 C1a 的映射表 + policy 的 `depth-by-stakes` 标记。Gate A/B 命中 → `deep`；rung 1 → `skip`；其余 → `standard`。

**语义类判据**（如"首次引入某模式"）机械判定能力有限 —— **必须保守地向上取整到 `standard`**，不得因判不出而落到 `skip`。

**不得硬编码判据**（删除 v1 的逃生口）。

## C1b. 审计记录（**绑定范围已收窄**，回应 opus f6）

| 项 | 定义 |
|---|---|
| 存储 | 仓内 `.aqg/audit-records/` |
| **绑定身份** | **`scope_hash` —— 仅覆盖 Gate A 命中的文件子集**，不是全工作区 |
| 写入者 | `/audit` 完成路径写入，不由被门禁的 agent 手写 |
| 内容 | `audit_id` + `scope_hash` + `decision` + 时间戳 |
| 失效 | `scope_hash` 不匹配即失效 |

**为什么收窄**：v2 绑全工作区哈希 → 审计后改任何无关文件（哪怕 README）都会使记录失效 → **活锁**，永远过不了门禁。绑到命中子集后，只有敏感文件再次变动才需要重审 —— 与 policy 的"没变过的代码不重复审"一致。

**适用声明（收窄，回应 google f3 / opus f6）**：记录由 agent 可写的文件系统承载，**可被同一 agent 伪造**。它是**防遗漏的书签，不是安全控制**。此限制必须写入 policy 边界声明；任何依赖它的表述都不得声称"保证已审计"。

## C2. `--auto-stakes`（词表统一）
先把 `--stakes` 经 `DEPTH_BY_STAKES` 映射到深度词表；在深度词表上取 max，序为 `skip < standard < deep`；该序在一处定义，由 C1、C2、router 共享（复用 router 已有的 `DEPTH_RANK`，`aqg_phase_router.py:58`）。

## C3. git pre-commit（**三态处理**，回应 google f1 + opus f7）

**已核实**：`install_aqg_construction_hook.py` 备份旧 `core.hooksPath`、需 `--force`、`--uninstall` 逐字节恢复。**可逆性已解决。**

**未解决的真实风险**：`core.hooksPath` **不叠加** —— 生效期间原有 husky / lefthook / pre-commit 的钩子**不运行**。在 pre-commit 跑 gitleaks 的仓里装 AQG 门禁 = 用审计门禁换掉安全门禁。

**v2 的"非空即中止"不可行**（opus f7 实测）：新 `git init` 仓的 `.git/hooks` 就有 **14 个 `.sample` 文件**。

**v3 三态**：

| 现状 | 行为 |
|---|---|
| 无 `core.hooksPath`，`.git/hooks` **无非-`.sample` 项** | 直接安装 |
| 有既存 hook（非 `.sample`）或已设 `core.hooksPath` | **chaining**：AQG 的 pre-commit 先调原钩子（原样传参、透传退出码），再跑自己的检查 |
| chaining 无法安全实现（如原方案自身也接管了 hooksPath） | **中止并给可操作提示**，不静默顶替 |

**保持 per-repo opt-in**，不论 Owner 裁决 #1 结果 —— v1 提议"默认对所有 commit 生效"会让一次误报阻断从未 opt-in 的人类提交。

**门禁条件**：命中 `deep` 且无匹配 `scope_hash` 的记录 → 非零退出并**打印判据本身**。**超时 fail-closed**（见 B2）。

## C3b. `standard` 档无执行点
明确承认且有意：`standard` 是"提交前审一次"的建议档，拦截它摩擦大于收益。M1 会统计 `standard` 的遗漏率，数据说话再议。

---

# 批次 D —— 数据模型

## D1. `rules_surface` 升级为可执行数据
`scripts/aqg_client_registry.py:41` 现为散文（`("CLAUDE.md rule block",)`），安装器无法据此行动。升级为结构化数据（用户级路径 / 项目级路径 / 是否 UI 管理 / 渲染格式）+ 通用 renderer。

**必须能表达"此 host 无用户级 rules 面"** —— 实测 4 个 trae 变体用户级为 `None`，cursor / trae 用户级由 GUI 管理。

**与 B3 的关系**：B3 先落地意味着写入逻辑先写一次、D1 再收敛。**有意接受的一次性重复** —— B3 止血，D1 重构。

---

# 批次 E —— description 改写（最后）

**依赖 A + B + M。**

## 现状诊断
| skill | 长度 | 预算花在 |
|---|---|---|
| `aqg-code-construction` | 512 | 六步骤名字、TDD 细节；触发条件在最后一句 |
| `aqg-phase-transition` | 671 | dedup 窗口、override 语法；判据零 |
| `aqg-security-review` | 633 | 触发清单写得好；深度冲突见 A2 |
| `aqg-multi-review` | 793 | `new`/`validate`、YAML skeleton |
| `aqg-test-quality-review` | 876 | 输出格式、与他者关系 |

**最关键**：`aqg-code-construction` 杠杆最高，但触发措辞是陈述句（*"Triggers on intent to write..."*），另外 4 个 AQG skill 用 `Use PROACTIVELY when...` 祈使句。**最该主动触发的那个没用主动触发的措辞。**

## 改动原则
触发条件前置，机制细节移入正文；`aqg-code-construction` 改为**祈使语气**并明确其为编码任务入口；`aqg-phase-transition` 补入 R1 渲染的句子。

## 边界（**v3 消歧**，回应 v4-pro f8）

**祈使语气 ≠ `PROACTIVELY` 令牌。** 本批次要求前者，禁止后者：

- ✅ 允许：把 *"Triggers on intent to write..."* 改成 *"Invoke this before writing or modifying code..."*
- ❌ 禁止：新增 `Use PROACTIVELY` 字样

理由：16 个 AQG skill 已有 4 个用该令牌，叠加其他来源后同一上下文数十条描述都标 proactive。**当一切都是 proactive，proactive 便不再是信号。** 改的是触发条件的**具体性**，不是紧迫感的**音量** —— 2026-08-11 治的正是音量过大导致的过度触发，不得回退。

## 验收
长度不超过改写前；R2 覆盖 description 中引入的任何 Gate A 文本；**用 M1 计数器对比 M2 基线，达到 M3 门槛**；未出现对 trivial 改动的过度触发（以计数器衡量）。

---

# 全局约束

## 判据文本一律经 R1 渲染并被 R2 断言
不得手写 Gate A 措辞。已有先例：`test_router_depth_matches_the_policy_single_source`（`skills/aqg-phase-transition/tests/test_phase_transition.py:756`）；`test_audit_gate_sensitivity_list_does_not_drift_between_adapters`（`tests/behavior/test_cursor_support.py:693`）。

## 验收统一表述
**不得写"测试全绿"** —— 基线本就不全绿。一律表述为"与基线一致：`tests/test_aqg_skill_install.py` 的两条预先失败之外全绿"。

## 不做什么
- 不修改 policy 的判据本身（本方案全部是投递问题）
- 不回退 2026-08-11 的措辞调整
- 不新增第二个深度权威
- 不触碰 DE / hub 仓
- **不声称解决对抗性伪造**（C1b）
- **不声称性能无问题**（单点热缓存实测不可外推）

## 风险
| 风险 | 缓解 |
|---|---|
| B3 全局块影响无关仓库 | **B3a 由 hook 机械判定仓级标识后静默退出**；同时修订现有模板 |
| B3 在已有内容文件上装不上 | B3b 追加；失败降级为每客户端警告 |
| B2 漏改动 | `-uall` + `git diff HEAD`；gitignored / 非 git 目录已声明为已知盲区 |
| 大仓 git 拖慢 | 保留扩充 matcher + 前置检查 + 超时（提醒 fail-open / 门禁 fail-closed） |
| C3 顶掉安全钩子 | 三态：直装 / chaining / 中止；保持 opt-in |
| C1 过度触发 | C1a 映射表含 exclude，需 Owner 审核 + 守卫测试 |
| 审计记录活锁 | `scope_hash` 只绑命中子集 |
| 判据文本成为新副本 | R1 渲染 + R2 三层断言（等价 / 金样本 / 负样本） |
| A/B 只是概率改进 | M 先行采基线 + 数值门槛；Owner 裁决 #1 前置 |

## 待 Owner 裁决
1. **【B 的硬前置】拦截 vs 提醒** —— C1 判 `deep` 时是否 exit 2？决定 A/B 是主路径还是铺垫。
2. **M3 数值门槛**。
3. **B3a-0 的仓级标识**选型（建议仓根 `quality-gates.json` + 补自动发现约定）。
4. **C1a 映射表**（含 exclude 规则）—— 需逐条审核。
5. **`installer/` 下纯 UI 常量是否算 Gate A** —— 建议不算，写入 policy 作边界示例。
6. **是否降低默认安装的 skill 数量**（低频者改按需启用）。
7. **`AQG_AGENT` 门禁**是否改默认生效（与 #1 相关）。

---

## 附：基线状态
`python3 -m pytest tests/ -q` → **3296 passed, 2 failed, 7 skipped**。

两条失败在 `tests/test_aqg_skill_install.py`（`test_plain_directory_created_by_link_attempt_falls_back_to_copy`、`test_cli_reports_copy_not_linked_after_plain_directory_link_result`），**干净 main 上即为红**，与 `a2a0b95` 提交信息记录的"2 个预先存在的失败"一致。本方案不引入、不修复它们；**所有验收均以"这两条之外全绿"表述**。
