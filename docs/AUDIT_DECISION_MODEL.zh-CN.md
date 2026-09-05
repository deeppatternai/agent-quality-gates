# AQG Audit Decision Model — 单一权威源

[English](AUDIT_DECISION_MODEL.md) | 中文

> 一次改动里：**谁决定要不要审 / 多深、在哪触发外审、审什么维度、审完怎么办。**
> 规则模板（`CLAUDE.md` / `AGENTS.md`）的"审计编排"段指向本图，避免在多处重述而漂移。
> 这是"调用审计冲突"的归口：以前深度选择编码在 2 处、自审/维度散在 3-4 处各说各话，本文把它们收成一条链。

## 一张图：一次代码改动的审计编排

```
session 起 → aqg-startup-preflight                （一次性 worktree/state 闸；不是审计）
             │
写代码     → aqg-code-construction
             ├─ 6 步建造（workflow §2）：step1–5（pattern→behavior→slice→rules→local-verify）
             │                          + step6 构造期自审（5 axes，inline）   ← 自审在前
             └─ audit-before-commit gate（workflow §5，6 步之后 / commit 前）   ← 代码外审唯一 chokepoint
                        │  深度 = audit-trigger.md（唯一来源）；phase-transition = 时机
                        │  维度 = multi-review（5 维面板） / security-review（安全单维）
                        ▼
                   de_audit(mode, focus)              ← audit-mcp，真正的外审引擎
                        │  有 findings？
                        ▼
                   aqg-audit-adjudication             （accept / reject / needs-user-decision 表）
commit → aqg-evidence-closeout（完工闸，核实审计做过，自己不审）→ PR / handoff
```

> 编号：`§2` / `§5` 是 `aqg-code-construction` `## Workflow` 的顶层小节号；`§2` 内部才有 step1–6（**step6 = Self Review**）。

## 1. 谁决定审计深度（fast / standard / deep）

**唯一权威:`docs/policies/audit-trigger.md`。** 其中的 `depth-by-stakes` 映射是深度的唯一来源
—— trivial → `skip`,moderate → `standard`,触发升级闸门 → `deep`。

不再有"主路径 / 兜底"这一对。旧框架把 `audit-self-routing.md` 列为主路径,但**从来没有任何安装器
铺过那个文件**,所以主路径从未 fire,实际上一直只有 `aqg-phase-transition` 在决定——而它自带一张
phase × stakes 表,把 trivial 改动路由到 `standard`,与策略 rung 1(trivial 不审)直接矛盾。
两套规则、相反答案,而"取更深信号"的规则让更严的那套静默胜出。Owner 2026-08-11 裁决合并为一套。

| 角色 | 工具 | 决定什么 |
|---|---|---|
| **深度** | `docs/policies/audit-trigger.md` | 按 stakes 定深度 —— 唯一来源 |
| **时机** | `aqg-phase-transition` | 在 PLAN / IMPL / TESTS 边界决定*何时该问*;深度读策略 |

- **high stakes 永不低于 deep**(安全下限;用户说"快速扫"也不能降;唯一例外是显式"别审" = skip opt-out,
  而高 stakes 的 opt-out 需要二次确认)。
- 来自深度映射的 `skip` 是**策略在说话**,不是用户 opt-out,不会被当作 opt-out 处理。
- phase-transition 是 **signal-only**;它自己不调审计工具(ADR §5 边界)。

## 2. 谁是外审触发入口（chokepoint）

| 改动类型 | chokepoint | 时点 |
|---|---|---|
| **代码** | `aqg-code-construction` 的 **audit-before-commit gate**（workflow §5，6 步构造之后） | `git commit` 前 |
| **设计契约**（DesignSpec / ADR / schema） | **默认 pass1 Deep**（发现+修订，`PLAN_DONE`/定稿前触发）；pass1 引发修订才走 **pass2 Deep** 验证闭环（pass1 clean → pass2 免/快速确认）。**trivial 文档编辑不走此路、按 §1 路由** | 定稿前 |
| **完工 / PR / handoff** | `aqg-evidence-closeout`（核实审计做过，**自己不审**） | 声称 done 前 |

→ 每个场景**各有其单一 chokepoint**（上表）；phase-transition / multi-review / security-review 都是喂给 chokepoint 的**输入**，不是平行入口。

## 3. 谁补充维度

| 工具 | 维度 | 用途 |
|---|---|---|
| `aqg-multi-review` | 5 维 cross-LLM 外部面板：logic / edge_cases / security / performance / concurrency | 大改动（≥200 LOC 或 ≥3 file）要多维独立判断 |
| `aqg-security-review` | 安全单维 in-session：OWASP Top 10 + CWE Top 25 | 安全敏感代码（auth/crypto/input/SSRF…） |

## 4. 自审 ≠ 外审（别把外审当第一道 reviewer）

- **构造期自审** = `code-construction` 6 步建造的 **step6 Self Review**，5 axes：correctness / readability / architecture / security / performance —— inline 快检，在 §5 gate **之前**完成。
- **外部面板** = `multi-review` 5 维 —— 跨 LLM、由 `de_audit` 跑，**不同训练分布 = 真独立判断**。
- 两者是**不同用途的两套 axes，不需要统一成一套**；各自内部一致即可。
- **规则**：外审前**必须先做结构化多维自审 —— inline 逐维评估即可满足**；把 `de_audit` 当**第一个** reviewer = AQG 设计的 anti-pattern。
  （`aqg-multi-review` 是外部面板的编排器，其 `new` 子命令可选**生成维度 prompt 骨架**辅助自审，但自审动作本身是 inline，不需先跑外部面板。）

## 5. 谁处理审计输出

`aqg-audit-adjudication` —— 每条 finding 给 `accepted` / `rejected` / `needs-user-decision`：
accepted 落地、rejected 写技术理由、needs-user-decision 点名 actor + 具体决定。
audit 输出是**输入**不是停下理由。

## 6. 档名

- **档位**：`fast` / `standard` / `deep`（无需审核时为 `skip`）。
- **声部 roster**（哪些 LLM 在哪一档）由 **decision-engine 拥有**，本文与策略文件均**不枚举**——roster 会随版本变（声部增减），规则层只认档名、不硬写名单。

---

> 维护：本文是审计编排的 source-of-truth。改这里时，同步检查
> `examples/aqg-claude-rules.example.md` / `examples/aqg-codex-agents.example.md`
> 的"审计编排"指针段是否仍一致（它们只放精简版 + 指回本文），并对照
> `skills/aqg-code-construction/SKILL.md` 的 §2（6 步，step6 自审）/ §5（audit-before-commit gate）编号未变。
