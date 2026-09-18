# Agent Quality Gates — 新机器装机指南

[English](ECOSYSTEM_BOOTSTRAP.md) | 中文

> Scope: macOS（Linux 适配 OK；Windows 不在 scope）
>
> 本文档是在新机器上**装起 Agent Quality Gates (AQG)** 的 single source of truth。AQG 自身版本管理见 [INSTALL_VERSIONING.md](./INSTALL_VERSIONING.md)。推荐的互补工具栈（SAST、生产反馈、确定性检查）见 [ECOSYSTEM_MUST_INSTALL.md](./ECOSYSTEM_MUST_INSTALL.md)。

---

## 目录

- [前置依赖](#前置依赖)
- [装机流程](#装机流程)
- [Secret 管理原则](#secret-管理原则)
- [验证清单](#验证清单)
- [常见故障](#常见故障)

---

## 前置依赖

新机器上必须先有：

| 工具 | 验证命令 | 说明 |
|---|---|---|
| macOS 14+ | `sw_vers` | Linux 适配可用，Windows 不在 scope |
| 宿主 coding agent | — | Claude Code（`claude --version`，从 [claude.ai/download](https://claude.ai/download)）和/或 Codex |
| git | `git --version` | 一般预装 |
| Python 3.11+ | `python3 --version` | AQG helper 脚本要它 |
| GitHub 账号 | 浏览器登录 / `gh auth login` | 用于 clone `deeppatternai/agent-quality-gates` |

可选（仅当你采用 [ECOSYSTEM_MUST_INSTALL.md](./ECOSYSTEM_MUST_INSTALL.md) 里对应的互补工具时才需要）：

| 工具 | 验证命令 | 说明 |
|---|---|---|
| Homebrew | `brew --version` | 本机装 semgrep / 其它工具要它 |
| Node.js 20+ / npm | `node --version` | 本机装 pyright 要它 |

---

## 装机流程

### Step 1：克隆 AQG repo

本地稳定安装路径统一用 `$HOME/.deeppattern/agent-quality-gates`。把 `AQG_ROOT` 设成它：

```bash
export AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"
mkdir -p "$(dirname "$AQG_ROOT")"
gh repo clone deeppatternai/agent-quality-gates "$AQG_ROOT"
cd "$AQG_ROOT"
git checkout main   # 或指定 release tag（cat "$AQG_ROOT/VERSION" 查看）
```

### Step 2：装 AQG skills

按你用的宿主 agent 跑对应 installer（一个或两个都跑）：

```bash
# Codex skills
"$AQG_ROOT/scripts/install.sh" --force

# Claude Code agent pack
"$AQG_ROOT/agent-packs/claude-code/install.sh" --scope user --mode link --force
```

默认安装把 skills symlink 进宿主 agent 的 skill 目录；之后在本 repo 里 `git pull`（或 `scripts/upgrade.sh`）即可更新全部。装完后重启宿主 agent 或开新 session 让 skill 列表 reload。

### Step 3：装常驻 hooks

AQG 把关键 gate（session-start preflight、handoff 强制、pre-commit skill 校验、完工 closeout 提醒等）设计成**常驻 hooks** — 没有它们，skill 只在模型"想起来"时才触发。一条命令装全套（改 `~/.claude/settings.json`，支持 `--uninstall`）：

```bash
python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --apply
```

hooks 在运行时读 `AQG_ROOT`，**未设置时静默跳过（装了=no-op）** — 所以务必在宿主 agent 运行的环境里 export `AQG_ROOT`（见 Step 4）。要 never-blocking 起步版，改 copy 或 adapt `agent-packs/claude-code/hooks/settings.warn-only.example.json`。

> **用 hooks 前，先把 `.aqg/` 加进每个目标项目的 `.gitignore`**：warn-only hook 会读 `<project>/.aqg/pr-body.md` 当本地 "PR body" markdown；若含 secret-like 内容，secret scan 会 surface，但 raw text 仍落盘。可以直接 `cat examples/aqg-gitignore.example` 进目标项目的 `.gitignore`。

### Step 4：为宿主环境 export `AQG_ROOT`

`settings.json` 的 env 字段喂 hook 子进程，`~/.zshrc` 喂交互 shell；**两处都配避免 GUI 启动 vs terminal 启动行为不一致**（GUI 启动 app 不读 `~/.zshrc`）。

加到 `~/.claude/settings.json`（绝对路径 — settings.json 不展开 shell 变量）：

```json
{
  "env": {
    "AQG_ROOT": "<上面 Step 1 的绝对路径>",
    "AQG_METRICS": "1"
  }
}
```

加到 `~/.zshrc`（双保险）：

```bash
cat >> ~/.zshrc <<'EOF'

# Agent Quality Gates
export AQG_ROOT="$HOME/.deeppattern/agent-quality-gates"
export AQG_METRICS=1
EOF
source ~/.zshrc
```

### Step 5：用 doctor 验证

```bash
python3 "$AQG_ROOT/scripts/aqg_doctor.py" --no-cli
```

然后退出当前 session，启动新 session，跑一个快速全景自检：

```bash
bash -c '
cd "$AQG_ROOT"
echo "=== AQG skills installed ===" && ls "${CODEX_HOME:-$HOME/.codex}/skills" 2>/dev/null | grep -c "aqg-" | xargs echo "  aqg-* skills:"
echo
echo "=== 本机 binary ==="
for cmd in python3 git gh claude; do
    p=$(which $cmd 2>/dev/null) && echo "  ✅ $cmd → $p" || echo "  ❌ $cmd 未装"
done
'
```

### 可选：互补工具

互补工具栈（semgrep SAST、拿 Actions logs 的 GitHub 官方 MCP、Sentry 生产反馈、pyright 类型反馈）单独记在 [ECOSYSTEM_MUST_INSTALL.md](./ECOSYSTEM_MUST_INSTALL.md)，按工作流需要选装。**semgrep 本地 SAST 免注册** —— 纯本地对本地 / OSS 规则集跑;`SEMGREP_APP_TOKEN` 仅用于可选的 Semgrep 托管平台。

---

## Secret 管理原则

| Secret | 跨机器复用？ | 为什么 |
|---|---|---|
| GitHub OAuth token | ❌ 每台机器自走授权 | OAuth 流程绑定设备；不要复制 `~/.claude.json` 中的 OAuth |
| API keys（如 `ANTHROPIC_API_KEY`）| ⚠️ 视情况 | 个人开发可复用；团队部署用 IAM 隔离 |
| 任何可选工具 token（如托管平台的 `SEMGREP_APP_TOKEN`）| ❌ 每台机器自建 | 追溯不同设备访问；revoke 时只影响一台 |

**绝不要把任何 secret paste 到宿主 agent 的 conversation 里** —— 用 `pbpaste` 或 `read -s` 走临时变量，写进 `~/.claude/settings.json` 的 `env` 字段，绝不进 conversation。

---

## 验证清单

装完后，新机器应满足：

- [ ] 宿主 agent 的 skill 目录里能看到 `aqg-*` skills（Step 5 自检）
- [ ] `python3 scripts/aqg_doctor.py --no-cli` 通过
- [ ] 常驻 hooks 已装（或 warn-only 示例已就位）
- [ ] `AQG_ROOT` env 在 hook 子进程可见（不靠 `~/.zshrc` 侥幸继承）
- [ ] `~/.aqg/metrics-ledger.jsonl` 在新 session 启动后有新 entry（说明 hook 真的跑了）
- [ ] 每个目标项目的 `.gitignore` 里有 `.aqg/`

---

## 常见故障

### Q: AQG hook 报 "AQG_ROOT not set"？
A: 检查 `settings.json` env 字段（不只是 `~/.zshrc`）。GUI 启动宿主 agent 不读 `~/.zshrc`，hook 子进程必须从 `settings.json` env 字段拿 `AQG_ROOT`。hook 设计成 `AQG_ROOT` 未设时静默 no-op，所以漏了 export 就等于 gate 从不触发。

### Q: 装完 skill 不出现？
A: 重启宿主 agent 或开新 session 让 skill 列表 reload。Claude Code 也可以 `/reload-plugins`。确认安装 link 进了预期目录（Codex 是 `${CODEX_HOME:-$HOME/.codex}/skills`；Claude Code 是 agent pack 安装路径）。

### Q: `aqg_doctor.py` 报 install 陈旧或 drift？
A: 跑 `scripts/upgrade.sh` 把本地 checkout 拉到 `origin/main`（ff-only）并刷新 skills；有 tracked 本地改动挡住更新时它会非零退出。

### Q: 想要 warn-only 但 hook 在 blocking？
A: 默认 hook 集会 enforce 关键 gate。要切 never-blocking，从 `agent-packs/claude-code/hooks/settings.warn-only.example.json` 安装（把所有 precondition 检查交给 `run_warn_only.sh`，它打印提示到 stderr 且总是 exit 0）。

---

## 引用

- [ECOSYSTEM_MUST_INSTALL.md](./ECOSYSTEM_MUST_INSTALL.md) — 推荐互补工具栈（SAST、生产反馈、确定性检查）
- [INSTALL_VERSIONING.md](./INSTALL_VERSIONING.md) — AQG 自身版本管理（branch / tag / sha pinning）

---

## 维护

- AQG 每次 release 后，更新 Step 1 的 tag 引用（或直接跟 `main` 用 `scripts/upgrade.sh`）
- 升级后重跑 `scripts/aqg_doctor.py --no-cli` 确认 install 健康
