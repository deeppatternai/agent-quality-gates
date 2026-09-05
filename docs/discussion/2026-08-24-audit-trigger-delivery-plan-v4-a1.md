<!-- id: 2026-08-24-audit-trigger-delivery-plan-v4-a1 -->
# 审计触发率改进方案 v4（执行版）

- id: 2026-08-24-audit-trigger-delivery-plan-v4-a1
- date: 2026-08-24
- author: agent (session 4a4e5c64), Owner-reviewed
- status: **active** — P1–P5 可执行且无需 Owner 裁决；批次 C / B3 / D 暂缓，M 需重新设计
- supersedes: `docs/discussion/2026-08-23-audit-trigger-delivery-plan-v3-a1.md`
- 审计谱系: v1 `aud_PyHpo7a0GA6U-kC0` → v2 `aud_fAh49XZyVQmyelui` → v3 `aud_AXRziKw2HJKTn_DU`（三轮均 4/4 `has-serious-issues`）。**v4 本身未经审计** —— 它是对三轮结论的范围切分，不是新设计。
- 正文自此以下为原文，未经改动。

---

# AQG 审计触发率改进方案 v4（执行版）

- **基线**：AQG `79f5df1`（main，0/0，工作区干净）
- **范围**：仅 AQG 仓
- **审计谱系**：v1 `aud_PyHpo7a0GA6U-kC0` → v2 `aud_fAh49XZyVQmyelui` → v3 `aud_AXRziKw2HJKTn_DU`。三轮均为 4/4 `has-serious-issues`。
- **状态**：**尚未执行任何代码改动。**

---

## 0. 为什么有 v4：三轮审计后的方向调整

三轮审计的元结论（opus，第三轮）：

> *"v3 **fixes the specific defects it was told about, but the same failure classes recur**"*

xai 同轮：*"excessive complexity after three revision cycles"*。

**证据**：B2 的工作区读取命令改了三版，每版都被证明还有洞（v1 漏新建文件 → v2 漏目录折叠+暂存内容 → v3 漏未跟踪内容+无初始提交）。而三轮约 60 条 finding 中，**命中批次 A 的只有 1 条**（且已修）。

**v4 的调整**：停止把它当一个大方案反复修订，改为**按成熟度切分**——成熟的立刻做，不成熟的明确暂缓，不再为了凑齐一份完整计划而虚构未验证的细节。

---

## 1. 执行顺序（v4 定稿）

| 阶段 | 内容 | 形态 | 何时 |
|---|---|---|---|
| **P1** | 批次 A —— 4 处深度条款改为引用 policy | PR | **立即** |
| **P2** | 批次 R' —— 解析器提取 + 3 条断言 + 1 个夹具 | PR | P1 之后 |
| **P3** | 批次 E —— 5 条 description 重写 | PR | P2 之后 |
| P4 | B1 —— preflight 注入纪律摘要 | PR | P3 之后 |
| P5 | B2 —— 工作区读取器 | PR | P4 之后 |
| — | **M** —— 测量埋点 | **重新设计** | 见 §6 |
| — | **B3** —— 全局 rules 块 | **暂缓** | 见 §5.3 |
| — | **C** —— 判定机械化 + 拦截 | **暂缓** | 见 §7 |
| — | **D** —— registry 重构 | **跟随 B3** | 见 §8 |

### E 为什么提到前面（v3 排在最后，本版修正）

v3 把 E 排最后的理由是"改 description 会影响触发行为，得等基线稳定才能归因"。**这个理由本身没错，但它只考虑了可归因，没考虑覆盖面。**

实测的 20 个客户端 hook 分布：

| 有 SessionStart（B1 能覆盖） | 有 hooks 但无 SessionStart | **完全没有 hooks** |
|---|---|---|
| claude-code、codex、cursor、codebuddy、qoder-cli、qoder-cli-cn、pi、devin、kimi-code（约 9 家） | qoder、qoder-cn、qoderwork | **kimi-work、qoderwake、trae-work、trae-work-cn、workbuddy、zed（6 家）** |

而这 6 家**全部都装了 skills**（`skills_surface = ['link', 'skills/']`）。

**结论**：对那 6 家而言，**skill description 是唯一能到达的通道**。B1 永远覆盖不到它们。把 E 排最后，等于让三分之一的客户端全程拿不到任何改进。

**归因怎么保住**：P1 / P2 / P3 是**三个独立 PR**，逐个合入观察。归因的真正价值不是分功劳，是**出问题时知道退哪个**——分 PR 就够了，不需要靠排序。

**E 的硬依赖**：必须在 A 之后（A 修的正是 skill 自带的冲突深度值，不先修就会把冲突措辞重写进 description）；必须在 R' 之后（E 会给 phase-transition 引入 Gate A 措辞，需要断言守着）。

---

## 2. P1 — 批次 A：把散落的深度决定权收回给 policy

四条是同一个毛病的四种形态：

| | 位置 | 毛病 | 改法 |
|---|---|---|---|
| **A1** | `aqg_phase_router.py:9-10` | docstring 写 `TESTS × stakes → fast/standard/deep`，实现（第 44 行 `DEPTH_BY_STAKES`）是 phase-independent 的 `skip/standard/deep`，同文件 36-38 行已说明旧表因"contradicted rung 1"被移除。且 `Phase × Stakes` 正是 `aqg_doctor.py` 的 `RETIRED_RULES_MARKERS` 判定 rules block 过期的标记之一——**router 的注释在用一个它自己会判为过期的框架** | 改写 docstring，指向 `DEPTH_BY_STAKES` 与 policy |
| **A2** | `aqg-security-review/SKILL.md:83` | 自带一套深度值 `mode=standard`，但其触发面就是 Gate A 清单（policy 判 `deep`，"and it wins"）。**核实：该 skill 全文 0 次提及 phase-transition，安全下限根本不在这条路径上——填什么档就跑什么档，没有事后升级** | 改为引用 policy，**不写死 `deep`**（写死会让"只读一遍现有代码"也触发深审，是新的过度触发） |
| **A3** | `aqg-test-quality-review/SKILL.md` | 全文不提深度 → 静默落到 `/audit` 默认 `standard`，policy 全程未参与 | 加一句深度来源，指向 policy |
| **A4** | `aqg-multi-review/SKILL.md` | `mode=<phase-transition recommended>` 是个**可能填不上的占位符**——phase-transition 没被调用过就不存在推荐值，而"有没有被调用"恰是全链最脆弱的一环 | 补降级路径：无结论则直接按 policy 判定 |

**验收**：与基线一致（见 §9）。

---

## 3. P2 — 批次 R'：不做渲染器，加强现有守卫

> **v3 的 R 设计已废弃。** 原方案是"做一个渲染器，所有载体运行时调它"，两个致命问题：(a) 在已有可用机制的地方引入新机制；(b) **对静态载体物理上做不到**——skill 的 `description` 是 YAML frontmatter，host 直接读文件，没有任何代码在运行。而 P3 要改的正是 description。

### 现状：已经有一套在跑的机制

两个现有载体**都是硬编码文本**：

```
agent-packs/claude-code/hooks/posttooluse_code_construction_reminder.sh:192
scripts/cursor_aqg_hook.py:243
```

一致性靠 `test_audit_gate_sensitivity_list_does_not_drift_between_adapters`（`tests/behavior/test_cursor_support.py:693`）守着——它解析 policy 的 `gate-a-tokens` 标记，断言每个词以词边界出现在**实际 emit 的文本**里（第 716 行）。

**这套机制有效、在跑、经过实战。**

### 但今天已经有一处分歧，测试抓不到

逐字对比两个载体：

```
shell hook :  ...deep regardless of size.  The sensitivity list...
cursor     :  ...deep regardless of size;  the sensitivity list...
                                        ↑        ↑
```

**已经不一样了，测试是绿的。** 它的 docstring 自陈了这个局限：*"asserts the CATEGORY TOKENS only... a wording difference between the two adapters would still pass."*

分歧本身无伤大雅，但**漂移通道是开着的**。

### R' 的改动

**R1'** —— 把 policy 解析器 `_policy_gate_a_tokens()` 从 `tests/behavior/test_cursor_support.py:646` 提升为共享 helper。纯搬家，零行为变化。

**R2'** —— 三条断言：

1. **保留**现有的"每个类别词以词边界出现在实际输出里"
2. **新增**：所有载体的 Gate A 句子**互相之间逐字符相同** —— 立刻抓出上面那个 `. The` / `; the`
3. **新增**：载体文本中不得出现非 policy 来源的深度词（`standard` / `deep`） —— 抓的是 A2 那类缺陷，"包含检查"抓不到

**R3'** —— 负样本夹具：把 v1 那份写错的草稿（`spanning repositories` 而非 `cross-repo`）签进仓库，**断言它必须让测试 FAIL**。这样测试自身的有效性也被测了。

**统一原则**：静态载体只能测试断言、不能渲染，所以**所有载体统一用测试断言**，机制单一。

---

## 4. P3 — 批次 E：description 重写

### 现状诊断

| skill | 长度 | 预算实际花在 |
|---|---|---|
| `aqg-code-construction` | 512 | 六步骤的名字、TDD 细节；**触发条件被挤到最后一句** |
| `aqg-phase-transition` | 671 | dedup 窗口、override 语法；**判据零** |
| `aqg-security-review` | 633 | 触发清单写得好（唯一做对的）；深度冲突见 A2 |
| `aqg-multi-review` | 793 | `new`/`validate` 模式、YAML skeleton |
| `aqg-test-quality-review` | 876 | 输出格式、与其他 skill 的关系 |

**共同病症**：描述在讲「这个 skill 内部长什么样」，而非「什么情况下必须叫我」。

**最关键的一处**：`aqg-code-construction` 杠杆最高（审计关卡在其第 5 步），但触发措辞是**陈述句**（*"Triggers on intent to write..."*），而另外 4 个 AQG skill 用 `Use PROACTIVELY when...` 祈使句。**最该主动触发的那个，恰恰没用主动触发的措辞。**

### 改动原则

- 触发条件前置，机制细节移入正文
- `aqg-code-construction` 改为**祈使语气**，明确其为编码任务入口
- `aqg-phase-transition` 补入 Gate A 判据（解开"判据藏在 skill 之后"的循环依赖）

### 边界（必须守住）

**祈使语气 ≠ `PROACTIVELY` 令牌。要前者，禁后者。**

- ✅ 允许：`"Triggers on intent to write..."` → `"Invoke this before writing or modifying code..."`
- ❌ 禁止：新增 `Use PROACTIVELY` 字样

理由：16 个 AQG skill 已有 4 个使用该令牌，叠加其他来源后同一上下文数十条描述都标 proactive。**当一切都是 proactive，proactive 便不再是信号。**

本批改的是触发条件的**具体性**，不是紧迫感的**音量**。2026-08-11 的单一源工作治的正是音量过大导致的过度触发，**不得回退**。

### 验收

- 每条改写后长度不超过改写前
- R2' 的断言覆盖 description 中引入的任何 Gate A 文本
- 合入后单独观察：**未出现对 trivial 改动的过度触发**

---

## 5. 批次 B

### 5.1 P4 — B1：preflight 注入纪律摘要

**通道已在三个核心 host 逐一验证**：

| host | 机制 | 证据 |
|---|---|---|
| Claude Code | `hookSpecificOutput.additionalContext` | `sessionstart_preflight.sh` |
| Codex | 同上 | `scripts/run_aqg_codex_hook.py:266-272` |
| Cursor | `{"additional_context": ...}` | `scripts/cursor_aqg_hook.py:320` |

**实现代价低**：preflight 内部就是把 `parts` 拼成 `summary` 再 emit，追加就是再拼一段（现有的 cwd-scope 提示就是这么加的）。

**改动**：追加 4 行——Gate A 句子（措辞受 R2' 断言约束）、"琐碎的不审"、"写代码前先调 `aqg-code-construction`"、已解析的 policy 绝对路径（解析失败输出显式 `UNRESOLVED`）。

**⚠️ 新增设计要求**：`sessionstart_preflight.sh:27` 在非 git 目录**直接跳过整个 preflight**：

```bash
if ! git -C "$project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[aqg session-preflight] skip: not a git worktree"
```

Cursor 适配器同理（`cursor_aqg_hook.py:326-331`）。**纪律段落本身不需要 git**，所以必须**放在 git 检查之前**，否则非 git 项目连这几行都拿不到。

**边界**：SessionStart 属**提醒**路径 → fail-open。

### 5.2 P5 — B2：工作区读取器

**要解决的场景**（引发整场调查的那个）：用 Bash heredoc 改文件 → PostToolUse 挂在 `Edit|Write|MultiEdit` 上不匹配 → 就算匹配，脚本从 `tool_input.file_path` 取路径而 Bash 负载里没这字段 → **提醒一个字都没发出**。

**修法**：判定输入从**事件**改为**状态**——不问"事件说改了哪个文件"，直接看工作区现在什么样。副作用是不再绑死任何 matcher，任何时机都能拿它去看一眼。

**完整的读取需要四件事，不是一对命令**（三版演进的教训）：

```
1. 文件清单    →  git status --porcelain -uall
                  （不加 -uall 会把未跟踪目录折叠成 "?? nd/"，路径匹配失效）
2. 已跟踪内容  →  git diff HEAD
                  （不带 HEAD 时已暂存内容返回 0 行；
                    无初始提交的仓里 git diff HEAD 直接报错，需回退）
3. 未跟踪内容  →  直接读文件
                  （git diff 系列【永远】看不到未跟踪文件内容；
                    须限大小、跳二进制、限数量、超时；仅本地匹配，不落盘不外传）
4. 非 git 目录 →  安静退出
```

**已声明的已知盲区**：

| 场景 | 处置 |
|---|---|
| 被 `.gitignore` 忽略的文件 | **不纳入**（纳入会引入大量构建产物噪声）。写入 policy 边界声明 |
| 非 git 工作区 | 静默退出。**退回纯提醒模式**——判据能送达，但没有任何机械观测或拦截 |

**性能**：本仓 659 文件单次热缓存实测 `--porcelain` 0.018s / `diff --name-only` 0.016s。**这是单点参考，不可外推到大型 monorepo 或冷缓存。** 因此强制要求：保留并**扩充** matcher（加 `Bash`；Codex 侧加其 shell 面）而非移除；调 git 前先做轻量前置检查（工作区 mtime / 上次判定后是否有写操作）；加超时。

**超时策略分叉**：提醒路径 fail-open；将来若接门禁，门禁路径必须 fail-closed。

### 5.3 B3 —— 暂缓（全局 rules 块）

**为什么暂缓，而不是"再改一版"**：

1. **三轮里唯一的 `critical` 在这**，且 3/4 审计员独立指出同一件事——全局配置影响用户**所有**仓库，包括与 AQG 毫无关系的项目
2. **v3 想用作用域条款限定，但那句话是假的**：`AQG_ROOT` 全机器可解析、skills 全局安装，**条件在任何装了 AQG 的机器上恒为真**，等于没有作用域
3. **挖出一个更根本的缺口**：AQG **目前没有任何可自动发现的仓级标识**
   - `.aqg/` 是 skill **运行时**创建的并写进 gitignore ——用它判断"要不要用 skill"是循环的
   - `quality-gates.json` 只被 `--config <显式路径>` 读取，**无根目录发现约定**

**所以 B3 真正的第一步不是"写块"，而是先给 AQG 定义仓级 opt-in 标识。** 这是一件独立的产品决策，不该塞在一个投递方案里顺手做。

**一个相关的好消息**（值得单独评估）：`quality-gates.json` **已经实现了完整的 per-project 门禁机制**——

```
ALLOWED_MODES = {"warn", "blocking", "off"}     quality_gates_config.py:18
ALLOWED_GATES = ("audit_adjudication", ...)      第 20 行
mode == "blocking" and status == "fail"          run_quality_gates.py:97
```

代码已经在消费 `blocking`，只是接的是 CI runner，没接 hooks。**这意味着"拦截还是提醒"本来就被设计成每个项目自己配，不必是全局开关。**

---

## 6. M —— 需重新设计（原方案循环未解）

v3 声称把 M1 挂到已在跑的 Stop hook（`wip_checkpoint_save.sh`）就解开了循环。**审计员指出这只是把挂点搬走，依赖没断**，核实属实：

```
M1 要统计"命中 Gate A 的改动"
  → 需要 Gate A 分类能力 = C1
  → C1 依赖 C1a（映射表）+ B2（读取器）
  → 而 M 本应先于 B/C
```

换 hook 挂点不解决分类依赖。**现有 Stop hook 能独立提供的只有"工作区变没变 + tree hash"，不含任何分类。**

**可行的出路**（未定，需另行设计）：M1 第一版只记**原始信号**（改了哪些路径、什么后缀），不做 Gate A 分类；等 C1a 那张表存在之后再**回溯分类**。这样 M 可以先跑起来采数据。

**在 M 重新设计完成之前，P1–P5 的效果只能定性观察，无法定量归因。** 这一点必须明说，不能假装有测量。

---

## 7. C —— 整批暂缓

**理由**：C 是唯一能闭环的一批（把判断从模型手里拿走），但它的关键路径 **C1a 不是代码问题，是产品决策**。

C1a 是一张"什么路径算敏感"的映射表。policy 里写的是**类别名**（`install integrity`、`auth`…），不是匹配规则；中间缺一层翻译。

**naive 匹配的实测后果**（本仓）：

| 类别 | 命中 | 实际 |
|---|---|---|
| `install` | `docs/ECOSYSTEM_MUST_INSTALL.md` | 文档 |
| `auth` | `benchmarks/.../ws8-authorization-*.example.json` | 测试夹具 |
| `schema` | `contracts/ledger/fixtures/**/*.json` | 测试夹具 |

**而 exclude 本身又是新风险**（三轮里的另一个 `critical`）：

```
contracts/ledger/fixtures/    → 夹具，该排除
contracts/ledger/conformance.py
contracts/ledger/paths.py     → 【真代码】，绝不能排除
```

**exclude 写粗一格，真代码就被放行。** 所以这张表实际定义的是**安全边界**，必须 Owner 逐条审核，且要按文件类型兜底（可执行源码后缀永不排除，不论路径）。

C 的其余子项（C1 判定器 / C1b 审计记录 / C2 词表统一 / C3 pre-commit 三态）设计都已成型，分析保留在 v3 文档中，**但它们全部依赖 C1a 和 B2，且 C3 还依赖 Owner 对"拦截 vs 提醒"的裁决。**

**唯一可以提前推进的**：C1a 那张表可以先起草——纯数据 + 审核，不写代码也能推进。

---

## 8. D —— 跟随 B3（registry 重构）

**D 本身不提高任何触发率**，是内部整洁度改善。它的存在理由完全依赖 B3 会不会做：B3 做了，rules 写入实现从 4 份变 5 份，D 值得做；B3 暂缓，重复还是 4 份，维持现状。

### 但查证时发现一个可以单独修的实际 bug

现在有**四份独立的 rules 写入实现**：

```
install_cursor_support.py       _render_project_rule() + _install_rule()
install_aqg_agent_clients.py    _rule_text()          + _install_rule()
install_aqg_qoder.py
install_aqg_work_clients.py
```

前两个函数**重名**、签名不同，而且**行为已经分叉**：

```python
# install_cursor_support.py:492 —— 文件存在但无 marker
if RULE_MARKER not in current:
    raise RuntimeError("refusing to overwrite existing Cursor rule")     ← 拒绝

# install_aqg_agent_clients.py:566 —— 同样的情况（else 分支）
rendered = (current.rstrip() + "\n\n" if current.strip() else "") + text  ← 追加
```

**一个拒绝，一个追加。** 而这恰是 B3b 争论三轮的问题——审计员指出"拒绝"会让多数机器装不上（用户的 `~/.claude/CLAUDE.md` 通常已有内容），结论是应当**追加**。

**也就是说：`install_aqg_agent_clients.py` 那份早就是对的，`install_cursor_support.py` 那份需要修。** 这跟 D 做不做无关，**是一个可以单独提的小修复**。

### 关于"项目级"的表述修正

v3 文档按**数据模型**描述项目级路径。核实**实际执行路径**后需修正：

```
supported_scopes 里声明 ('user', 'project')     ← 理论支持两种
adapter_actions['apply'] 写死 --scope user      ← 17/20 实际只走用户级
--scope 参数默认值就是 "user"                    ← 两个安装器都是
自动安装遇到 project-only（qoder/qoder-cn）      ← PR #82 主动跳过
```

**在默认安装路径里，项目级 scope 一次都不会被走到。** 要走必须显式 `--scope project --project-root <绝对路径>` 手动调用。

**它是一条存在但没接线的支路。** D 若开工，应顺带决策：**项目级支路是留着维护，还是删掉。**

---

## 9. 全局约束

### 验收统一表述

**不得写"测试全绿"** —— 基线本就不全绿：

```
python3 -m pytest tests/ -q  →  3296 passed, 2 failed, 7 skipped
```

两条失败在 `tests/test_aqg_skill_install.py`（`test_plain_directory_created_by_link_attempt_falls_back_to_copy`、`test_cli_reports_copy_not_linked_after_plain_directory_link_result`），**干净 main 上即为红**，与 `a2a0b95` 提交信息记录的"2 个预先存在的失败"一致。本方案不引入、不修复它们。

**所有验收一律表述为"与基线一致：这两条之外全绿"。**

### 不做什么

- 不修改 policy 的判据本身（本方案全部是投递问题，措辞正确、清单完整、单一源成立）
- 不回退 2026-08-11 的措辞调整（该次同时加入 SKIP 子句并**强化**了敏感类强制，且替换了一个指向从未发布过文件的死链）
- 不新增第二个深度权威（所有新文本引用 policy）
- 不触碰 DE / hub 仓
- 不声称解决对抗性伪造
- 不声称性能无问题（单点热缓存实测不可外推）
- **不再为了凑齐完整计划而虚构未验证的实现细节**（这是三轮审计的直接教训）

---

## 10. 待 Owner 裁决

**P1–P5 不需要任何裁决即可执行。** 以下是被暂缓项的前置：

| # | 事项 | 卡住谁 |
|---|---|---|
| 1 | **仓级 opt-in 标识**选型（建议评估复用 `quality-gates.json` + 补根发现约定） | B3 |
| 2 | **拦截 vs 提醒** —— 注意 `quality-gates.json` 已实现 per-project 的 `warn/blocking/off`，此题可能不必是全局开关 | C3 |
| 3 | **C1a 映射表**（含 exclude 规则）—— 定义安全边界，需逐条审核 | C 全部 |
| 4 | `installer/` 下纯 UI 常量是否算 Gate A —— 建议不算，写入 policy 作边界示例 | C1a |
| 5 | **项目级 scope 支路**留还是删 | D |
| 6 | 是否降低默认安装的 skill 数量（低频者改按需启用），以降低描述竞争噪声 | E 的后续 |

---

## 11. 可单独提出的小修（与本方案无依赖）

1. **`install_cursor_support.py:492` 的"拒绝写入"行为**应改为追加（见 §8）
2. **两个 `_install_rule` 重名**、异常类型不一（`RuntimeError` vs `InstallError`）
3. **`tests/test_aqg_skill_install.py` 的两条长期失败**——不在本方案范围，但"main 长期有红测试"本身值得单独处理
