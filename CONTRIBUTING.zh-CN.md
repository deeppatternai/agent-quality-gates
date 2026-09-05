# Contributing to AQG

[English](CONTRIBUTING.md) | 中文

> 面向所有参与 AQG 的人 **和** AI session。**先读下方"双仓模型"** —— 它决定你能不能
> 直接贡献代码。在私有上游开发的维护者再读并行工作树纪律（AQG 常有多个 AI session
> 并行开发）。

## 双仓模型：公开仓是只读镜像

AQG 采用**双仓治理模型**：

- **私有上游** —— 所有开发的唯一发生地（本文件的编写处）。
- **公开镜像**（[`deeppatternai/agent-quality-gates`](https://github.com/deeppatternai/agent-quality-gates)）
  —— **每次发布时从私有源单向重新生成**。公开树是 carve 出来的快照，永不回并。

**若你正在看公开镜像，它是只读的：** 不接受 pull request 和直接 push —— 发布时的重新
carve 会覆盖它们。请改为在公开仓开 **issue** 或 **discussion**；bug 反馈、功能想法、
提问都欢迎，会被 triage 进私有上游。下方所有内容都是给在私有上游开发的维护者看的。

## 并行 AI session：各用独立 git worktree（重要）

**不要让多个 session 共享同一个工作树。** 它们会抢同一个 `HEAD`：一个 session
`git checkout` 切 branch 会改变所有共享该工作树的 session 看到的 HEAD，未提交的编辑会
**bleed 到别人的 branch**（2026-06-10 实测发生过：一个 session 的 HEAD 被并行 session
在共享树里来回切了 3 次，未提交改动一度落到别人 branch 上）。

**规则**：每个并行 session 一开始就开自己的独立工作树：

```bash
# 从最新 main 开一个独立工作树 + 独立 branch
git worktree add ../aqg-<session-name> -b <your-branch> origin/main
cd ../aqg-<session-name>
# …在这里干活：edit / commit / push / 开 PR…
# 完事后（PR 合并后）清理
git worktree remove ../aqg-<session-name>
```

独立工作树 = 独立 `HEAD` + 独立 index，从根上不抢、不 bleed。Claude Code 用户也可让
session 一开始就 `EnterWorktree`（同等效果）。

**若确实共享一个工作树**（不推荐）：严格 **commit-before-switch** —— 每个逻辑改动
立即 commit、**显式 stage 具体文件**（不要 `git add -A`）、切 branch 前确认 `git status`
clean，把未提交窗口压到最小。验证别人改动用 explicit ref（如 `origin/main`）不要用 `HEAD`。

## 改 AQG skill → 三件套同步

改 `skills/aqg-*/SKILL.md` 或 sidecar（`description.md` / `triggers.md`）后，**必跑**：

```bash
python3 scripts/aqg_skill_gen.py regen --all   # 同步生成的 wrapper
```

并把改动的 wrapper 一起 commit。CI 的 **"Wrapper generated-artifact gate"** 会卡
out-of-sync（本地漏跑 = CI 红）。新增 / 改 skill 还要跑
`python3 scripts/aqg_skill_validator.py --strict <skill-name>`。详见
[`docs/SKILL_AUTHORING_GUIDE.md`](docs/SKILL_AUTHORING_GUIDE.md)。

## audit / 测试纪律

- **改动 commit 前**按 [`docs/AUDIT_DECISION_MODEL.md`](docs/AUDIT_DECISION_MODEL.md) 选 audit
  深度（代码外审 chokepoint = `aqg-code-construction` 的 audit-before-commit gate）。
- 测试走 **vertical-slice TDD**（一个 test → 一个实现，别 horizontal 批量）；行为测试是 CI
  门禁：本地先 `python3 -m pytest tests/behavior/ -q` 绿了再推。
- 给人读的产物（README / 指南 / 报告）额外用**读者视角**审一遍（反过度设计）。

## 其它指南

- [`docs/AUDIT_DECISION_MODEL.md`](docs/AUDIT_DECISION_MODEL.md) — 审计编排单一权威源
- [`docs/SKILL_AUTHORING_GUIDE.md`](docs/SKILL_AUTHORING_GUIDE.md) — 写 / 改 skill
- [`docs/INTEGRATION_GUIDE.md`](docs/INTEGRATION_GUIDE.md) — 集成
- [`AI_SETUP.md`](AI_SETUP.md) — 把 AQG 配进 CLAUDE.md / AGENTS.md（喂给 AI 自助装）
