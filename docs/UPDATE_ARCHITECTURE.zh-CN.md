# AQG 更新架构

[English](UPDATE_ARCHITECTURE.md) | 中文

> **状态**：设计稿，尚未实现。这是实现必须满足的规格说明。
>
> 配套文档 —— 本文不重复它们的论证：
> - [`INSTALL_VERSIONING.md`](INSTALL_VERSIONING.md)（暂仅英文）—— 今天如何安装 / 钉住一个已评审的 ref。
> - 产品的去向是薄客户端 + 云端 verdict —— 那个**终局**里，大多数更新根本不再触碰客户端。本文覆盖的
>   是那次迁移之后依然存在的一层：只要还有东西装在机器上，就还得在机器上更新它。

## 0. 范围，以及那条承重的不变式

**在范围内**：一次更新必须在一台已安装的机器上改变什么，以及如何在不弄坏现有安装的前提下应用它。

**刻意排除在外**：由**什么来触发**检查。§10 会盘点候选方案并给出推荐，但应用路径不得知道、也不得关心是谁触发了它。

> **不变式**：更新逻辑是一个**只有单一入口的库**，可以被任何触发器调用，幂等、并发安全、可崩溃恢复。
> 触发器唯一的职责就是调用它，然后让开。

下面的一切都由此推导而来。如果某个设计选择会让应用路径依赖于"我是被 hook 调起来的"，那这个选择就是错的。

## 1. 一次更新到底要传播什么

已核实的安装形态（2026-09-02，本 checkout）：

- `AQG_ROOT` = `~/.deeppattern/agent-quality-gates`，一个 **git checkout**。
- Skills 以**符号链接**路由进各宿主的 skills 目录。[`scripts/aqg_client_registry.py`](../scripts/aqg_client_registry.py)
  里 20 个客户端全部默认 `link` 模式。
- Hooks 是**宿主自有的配置条目，其命令串指回 `AQG_ROOT`**。Claude Code 的实际形态，取自 `~/.claude/settings.json`：

  ```
  if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}" \
    bash "$AQG_ROOT/agent-packs/claude-code/hooks/sessionstart_preflight.sh" "$CLAUDE_PROJECT_DIR"
  ```

  `aqg_root` 是**刻意不烘进**命令串的（见
  [`install_aqg_hooks.py:138`](../scripts/install_aqg_hooks.py)），运行时才解析。
- Rules 是写在宿主自有文件里的一个受管块（`CLAUDE.md`、`AGENTS.md`、`.cursor/rules/aqg.mdc` 等）。

由这个形态出发，一次更新的载荷可以拆成五类，成本差异是真实的：

| # | 变更 | 光靠 `git pull` 够吗 | 还需要什么 | 动宿主配置吗 |
|---|---|---|---|---|
| 1 | hook **脚本正文**改了 | ✅ 够 —— 命令串指向 checkout 内部 | 无 | 否 |
| 2 | skill **内容**改了（link 模式） | ✅ 够 | 无 | 否 |
| 3 | **新增** skill | ❌ | 每个宿主建一个符号链接 | 否 |
| 4 | **删除 / 重命名** skill | ❌ | 每个宿主 prune 掉自己拥有的符号链接 | 否 |
| 5 | **hook 集合成员变了** —— 新 hook 文件、新生命周期事件、参数变了 | ❌ | 重写宿主的 hook 配置 | **是** |

> **由此得出的核心规则**：*只有第 5 类需要修改宿主自有的配置。* 第 1–4 类都是仓库内部的事加上符号链接的账目管理。
> 第 5 类是唯一的有状态合并，唯一有真实爆炸半径的，也是唯一可能让某个宿主停在半配置状态的。
>
> 这一点可以从 release diff 机械地判定（某个宿主的 `hooks/` 目录下有没有文件被新增、删除或改名？某个适配器的
> 事件列表变了没有？）。**release 流水线必须计算并发布这个分类**，不要让客户端去推断。

Copy 模式（`--copy`）会打破这张表：第 1、2 类不再免费，需要重新拷贝。copy 是受支持的安装选项，所以应用路径必须
两种都能处理，而且必须从**记录的状态**里读某个宿主用的是哪种模式，不能假设。

## 2. 三层，以及本文规定的是哪两层

| 层 | 职责 | 位置 |
|---|---|---|
| **触发** | 决定**什么时候**去看 | §10 —— 有多种，都可选，可互换 |
| **规划** | 决定这台机器**需要什么** | 本文 §4–§6 |
| **应用** | 做到，或者不留下任何半成品 | 本文 §5、§7、§8 |

## 3. 前人怎么做的

### 3.1 Superpowers / Claude Code 插件 —— 它把第 5 类整个绕开了

"superpowers 怎么处理需要改 `settings.json`"这个问题，诚实的答案是：**它从来不改。**

- 插件把自己的 hooks 声明在**插件包内**的 `hooks/hooks.json` 里。Claude Code 直接加载插件 hooks，
  **不会把它们写进用户的 `~/.claude/settings.json`**。于是第 5 类退化成"包里换个文件"—— 和第 1 类成本一样。
- 路径用 `${CLAUDE_PLUGIN_ROOT}`，一个宿主提供的变量，所以版本升级挪动安装目录不会让任何命令串失效。
  （`${CLAUDE_PLUGIN_DATA}` 是它的对应物 —— 跨更新**存活**的目录，是那些不能被抹掉的状态该放的地方。）
- 多宿主差异用**并排的声明式文件**处理：superpowers 同时发 `hooks/hooks.json` 和 `hooks/hooks-cursor.json`。
- 版本来自 `plugin.json`；宿主在会话启动后**随机延迟至多十分钟**检查更新，**只更新磁盘**，绝不打扰正在运行的会话，
  然后提示 `/reload-plugins`。

**能抄的三条**：(a) 把每个宿主的 hook 集合表达成**声明式数据**，而不是安装器代码；(b) 绝不把绝对根路径烘进命令串
—— AQG 已经通过 `$AQG_ROOT` 做到了；(c) 更新磁盘，绝不动运行中的会话。

> **决定（2026-09-02，Owner）：不走 marketplace 这条路。** 它只覆盖 Claude Code 一个宿主，
> 而更新库为另外 19 个宿主、存量用户迁移、状态迁移这三件事仍然必须存在 —— 它加一条腿，消不掉任何工作量。
> 已评估但未采纳的代价：明文暴露方法论、`AQG_ROOT` 与宿主管理的插件根二选一的两难、
> 失去安装期表达用户选择（warn-only / 阻断集）的能力、存量用户 `settings.json` 里既有条目的一次性清理、
> 以及放弃自有签名验证。若将来要做**公开社区版**可重新评估，届时应把插件包做成**从同一注册表渲染的生成物**，
> 而不是一份分叉。

**为什么不能直接照搬**：宿主只在 *Claude Code 内部*拥有插件生命周期。AQG 的注册表里有 20 个客户端，大多数根本没有
插件系统，而且它们的 hook 面互不兼容（§6）。此外插件内容是明文的，这恰恰是
发布姿态中所说的"泄漏源"。**因此插件化最多是若干分发渠道之一
—— 绝不能当作更新机制本身。**

### 3.2 Decision Engine 的 `installer/` —— 真正该借鉴的自家前作

DE（`~/.deeppattern/decision-engine`）已经在跑一套生产形态的托管自更新。它是我们手上最接近参考实现的东西，
而且是在同一套工程纪律下建起来的。

| DE 模块 | 做什么 | 借鉴吗 |
|---|---|---|
| `updater.py`（2135 行） | **只读**检查。docstring 写得很明确："`UpdateInspection` 是临时诊断数据，**永远不是授权凭据**"；执行变更的一方必须在自己的锁下重跑每一项检查。 | **直接借** —— 读/授权分离是这里最有价值的一个想法。 |
| `update_transaction.py`（2598 行） | 可崩溃恢复的应用：拿锁 → 拒绝有活跃会话 → 重新检查 → 恢复副本 + 预写日志 → reset → 冒烟 → 提交或回滚。journal 相位：`prepared`、`reset_started`、`candidate_applied`、`smoke_started`、`rollback_started`、`retry_pending`、`repair_required`。 | **相位模型直接借**；具体机制需改造（§5）。 |
| `update_coordination.py`（560 行） | 跨进程准入。"**文件存在永远不是权威**" —— 每把锁都是持在打开句柄上的 OS advisory lock；活跃会话持有租约。 | **直接借** —— AQG 的并发会话比 DE 只多不少。 |
| `release_contract.py` + `release-trust.json` | 对确定性 manifest 做 detached RSA-PKCS1v15-SHA256 签名，用**钉死的公钥集**验证，带 `revoked` 标记和 release **序号**（防回滚）。无网络、无 git、无文件系统。 | **直接借** —— §9 要的东西它已经写好并测过了。 |
| `client_hosts/`（`contract.py` + `registry.py` + `hosts/*.py`） | `AgentHostSpec` 冻结数据类 + 静态注册表 + `validate_host_specs` 在 import 时 fail-closed。"分发器拥有词汇表和校验。宿主适配器只拥有各自格式相关的变更机制。" | **模式直接借**；但 AQG 自己的注册表已经存在，应当扩展而非复制（§6）。 |
| `install.py` 的 `_route_skills` / `_prune_stale_routes` | 符号链接（POSIX）/ 目录联结（Windows）路由；只 prune 指向**本组件自己**的 skills 目录的链接；遇到真实目录抛 `SkillRouteConflict` 而不是覆盖。 | **直接借** —— §8。AQG 的 `install.sh` 已有等价的 prune，应当统一。 |
| `launcher.py` | MCP 启动时的有界更新闸门；若 HEAD 变了，**一个全新的子解释器继承同一条 stdio 连接**，所以不需要重启 agent，新旧模块也永不混用。 | 作为**触发器**（§10.3），以及"绝不混用模块世代"这条规则。 |

**DE 唯一没有的东西：hooks。** DE 路由 skills、写 MCP 注册项，但它从不往宿主的生命周期 hook 配置里合并条目。
第 5 类 —— AQG 问题里最难的部分 —— 没有 DE 先例，必须在这里设计（§7）。

## 4. 更新状态：机器上必须记下什么

今天，一台已安装的机器**什么都没记**：没记配置过哪些宿主、路由过哪些 skill、hook 集合是什么形状、link 还是 copy。
每次操作都靠探测重新推导，因此**一次重命名和一条无关的陈旧链接是无法区分的**。

定义一个我们自有的状态文件，原子写入，`0600`：

```
$AQG_STATE_ROOT/install-state.json
默认：   ~/.deeppattern/aqg-state/install-state.json
Windows：%USERPROFILE%\.deeppattern\aqg-state\install-state.json
```

> **决定（2026-09-02，Owner，暂定）：`state_root` 放在仓外。**
> 硬约束：**它绝不能位于 `AQG_ROOT` 之内。** §5.1 的原子切换会把 `AQG_ROOT` 整个换成另一棵树，
> 放在里面的状态会随切换消失或退回旧版本 —— 那正是回滚时最需要它的时刻。
>
> 选 `~/.deeppattern/aqg-state/`（与 `agent-quality-gates`、`decision-engine` 平级）的理由：与既有安装
> 布局一致；DE 的设备配置也在 `~/.deeppattern/decision-engine/` 下，同一套心智模型。命名用 `aqg-state`
> 而不是 `.aqg`，是为了不和**仓内**已有的项目级 `.aqg/`（ledger / adjudication）混淆 —— 那是另一回事。
> 支持 `AQG_STATE_ROOT` 环境变量覆盖。标注"暂定"是因为 Windows profile 布局尚未实测（§13）。

| 字段 | 为什么需要 |
|---|---|
| `schema` | 让未来的 updater 能迁移这个文件本身 |
| `channel` | `stable`（签名 tag）\| `edge`（main，仅手动）—— §9 |
| `installed_version`、`installed_commit`、`release_sequence` | 当前真正活着的是什么；序号用于阻断回滚攻击 |
| `installed_at`、`applied_by` | 溯源：哪个触发器、什么时候应用的 |
| `hosts[]` | **按宿主**：`client_id`、`scopes`、`skills_dest`、`skills_mode`（link/copy）、`routed_skills[]`、`hook_config_path`、`hook_set_hash`、`rules_block_hash`、`last_applied_version`、`status` |
| `pending[]` | 被推迟或失败的规划项，带原因 —— `doctor` 报的就是它，下次运行重试的也是它 |

有两条性质比字段清单更重要：

1. **`routed_skills[]` 是机器侧的花名册。** [`skills.list`](../skills.list) 是*仓库侧*的花名册。
   两者做 diff，才使得新增 / 删除 / **重命名**成为可判定的 —— 一次重命名，唯一可观测的形式就是"针对已记录的前一状态"
   的（删除 + 新增）。没有这个文件，就没有"前一状态"可以 diff。
2. **按宿主分别记录，因为部分成功是常态而非例外。** 某个宿主的配置可能被锁、缺失或被手改过，而另外五个干净地应用了。
   DE 的 `managed_activation` 明确接受这一点（"wiring 失败可以让部分客户端保持不变，但绝不会让一个已改动的客户端
   指向半成品的 launcher"）。状态文件必须能表达"12 个宿主在 0.15.0，1 个卡在 0.14.0，原因如下"。

## 5. 应用流水线

相位，改造自 DE 的 journal 模型。每个相位在开始前先落 journal；崩溃后依据 journal 续做或回滚。

| # | 相位 | 做什么 | 失败时 |
|---|---|---|---|
| 0 | **准入** | 拿 OS advisory 安装锁；若被别的应用进程持有则拒绝；检查节流；解析 channel | 静默退出（不算错误 —— 有别人在做） |
| 1 | **获取** | 拉取目标 ref；对照钉死的公钥集验证签名与序号；**绝不做任何变更** | 中止、记录，安装保持原样 |
| 2 | **规划** | 用 `install-state.json` 与目标做 diff → 一份有序的、按宿主的动作清单；每项按 §1 的 1–5 类归类 | 中止，并打印可读的规划清单 |
| 3 | **暂存** | 把目标物化成活动根旁边的 `versions/<commit>/`；在那里跑仓库内部检查 | 丢弃暂存树；什么都还没上线 |
| 4 | **应用-仓库** | **原子地**把 `AQG_ROOT` 符号链接切到 `versions/<commit>` | 切回去 —— 一次 `rename(2)`，没有中间态 |
| 5 | **应用-宿主** | 按注册表顺序逐个适配器，每步幂等、单独落 journal：路由/prune skills，然后（仅第 5 类）合并 hook 配置，再是 rules 块 | 回滚**该宿主**；继续下一个；记入 `pending[]` |
| 6 | **冒烟** | 对新根跑 `aqg_doctor.py --no-cli` | 回滚相位 4–5 |
| 7 | **提交** | 写 `install-state.json`，清 journal，按保留策略清理旧的 `versions/` | — |

### 5.1 双 checkout 切换，而不是原地 reset —— 以及 AQG 为何必须不同于 DE

DE 是**原地** reset 它的 checkout，靠一棵恢复树兜底。AQG 不能照抄，理由很具体：**AQG 的 checkout 会被别的进程
在任意时刻读取。** 每一个并发运行的会话里，每一次工具调用都在解引用
`$AQG_ROOT/agent-packs/.../*.sh`。`git checkout` 跨文件不是原子的，所以原地 reset 存在一个窗口 ——
此刻正好触发的 hook 会 source 到一棵半更新的树。DE 没有这种暴露面：只有它自己的 launcher 读它的 checkout，
而 launcher 是持锁的。

所以：暂存到 `versions/<commit>/`，然后对符号链接做一次 `rename(2)`。

**磁盘布局**（用 `git worktree` 共享同一个对象库，避免把 26 MB 的 `.git` 复制 N 份）：

```
~/.deeppattern/
├── aqg-repo/                                       ← 唯一的 .git（约 26 MB）
├── aqg-versions/
│   ├── 0.15.0-a1b2c3d/                             ← 约 17 MB
│   └── 0.15.1-e4f5a6b/                             ← 约 17 MB（当前）
├── agent-quality-gates -> aqg-versions/0.15.1-e4f5a6b   ← AQG_ROOT，切换只动这一个符号链接
└── aqg-state/install-state.json                    ← 仓外状态（§4）
```

> **决定（2026-09-02，Owner）：保留 2 棵 —— 当前 + 前一个。** 合计约 60 MB。一次坏更新最多回退一步；
> 更早的版本对象都还在 `aqg-repo/` 里，需要时重新 checkout 即可。
> 清理只在相位 7 提交成功后进行，且**绝不清理**当前 `AQG_ROOT` 指向的那棵，也**绝不清理** journal 仍在引用的那棵。

自动 `check(apply=True)` 在记录 `applied` 成功结果后执行历史清理。保留实际更新前、后的版本，
并额外保护 hooks 引用的目录和承载共享 Git 对象库的目录，因此可能多于两份。
清理期间同时持有检查锁和更新锁；有未完成 journal、待处理安装事项或期间又发生版本切换时跳过。
仅通过 Git 删除登记完整、干净且未锁定的 worktree，不使用 `--force`，不降级为递归强删。
检查和启动更多清理工作共享 5 秒预算，已启动的单次删除独立限时 30 秒。
文件系统和 host 读取在前后检查预算，不强行中断读取。一个目录被拒绝不会阻挡其他干净目录。
被 Git 忽略的本地数据会保留，仅允许清理有对应已跟踪 Python 源文件的 `__pycache__` 字节码。
已删除目录和被拒绝目录分别记录，部分失败不丢失已经完成的删除记录。
清理失败不改变更新结果、pending 状态或下次更新准入，只尽力写入
`aqg-state/update-cleanup-last-result.json`，下次自动更新成功后再次尝试。
手动 `apply_commit` 不触发清理，因为其调用方可能尚未完成 host 配置刷新。
备份和非版本目录不会被清理；已记录的 hooks 物理路径受到保护，任意会话缓存的更老路径尚未被追踪。
每次删除前重新读取 hooks 引用。AQG 事务共用更新锁，绕过该锁的外部配置改写不受原子同步保护。

### 5.2 "有人正在用" 时怎么办：既不停，也不强制

触发时刻**永远**和使用重叠 —— SessionStart 触发时这个会话正要开始用，skill 脚本触发时用户正在用。
而在一台常年开着几个会话的机器上，等"没人在用"等于**永不更新**。

所以设计不是去"错开"，而是**让重叠无害**：我们从不原地覆盖任何文件。新版本落在新目录，旧的那棵原封不动留在盘上，
最后只把一个符号链接 `rename(2)` 一下。于是：

| 切换发生的时机 | 结果 |
|---|---|
| 在 hook 解析路径**之前** | 跑新版。正常 |
| 在 hook 已打开文件**之后** | POSIX 下打开的文件句柄保住旧 inode，跑完旧版。正常 |
| 在 bash **读到脚本一半**时 | bash 是边读边执行的 —— 这在原地覆盖下是灾难；但我们没覆盖，它读的 inode 一字节未变。**安全** |

**这才是双 checkout 更根本的价值**：它把"更新"从破坏性操作变成了累加操作。

**唯一真实的坑是世代混用**：一个脚本在开头解析了 `$AQG_ROOT`，一秒后再去调 `$AQG_ROOT` 下的另一个文件，
就会拿到新版本 —— 同一次逻辑操作跨了两个世代。**修法：`scripts/_aqg_context.sh` 导出的必须是 `realpath`
解析后的绝对路径，而不是符号链接本身**，这样一次 skill 运行从头到尾钉在同一棵树上。这是十行改动，
但没有它就会出间歇性的怪 bug。同一条规则借自 DE 的 launcher：**同一个进程内绝不混用模块世代。**

**宿主配置那一步**：宿主是在会话**启动时**读 hook 配置的，跑起来之后不再读。所以更新期间写 `settings.json`
不影响正在运行的会话，下个会话生效 —— 与插件"改磁盘、下次加载"是同一套语义。

**我们唯一会停下来等的，只有另一个 updater**（安装锁）。一个 hook 触发时若发现锁被持有，直接 no-op 退出，
**绝不阻塞用户会话去等待**。

### 5.3 失败与回滚

| 失败点 | 用户看到什么 | 恢复 |
|---|---|---|
| 1 获取（网络断 / 验签不过 / 序号倒退） | **什么都没发生** | 记录，下次再来；用户无感 |
| 3 暂存（磁盘满 / checkout 失败） | 什么都没发生 | 删掉暂存树 |
| 4 切换（rename 失败） | 什么都没发生 | `rename` 原子，要么成要么不成，无中间态 |
| 5 宿主（某宿主写失败） | **部分成功** | 用写前字节回滚**该宿主**，其余宿主保持新版，该项进 `pending[]`，doctor 报出 |
| 6 冒烟失败（新版跑不起来） | 回到旧版 | 符号链接切回 + 回滚所有已改的宿主配置 |
| 进程被杀 / 断电 | 取决于停在哪个相位 | 下次触发读 journal：能续则续，不能续则回滚 |
| **回滚本身失败** | 会话里提示 | 置 `repair_required` 并打印备份路径。**绝不重试成死循环** |

三条贯穿规则：

1. **相位 5 的部分失败不是 bug，是预期情况。** 12 个宿主成功、1 个卡住是正常结果，状态文件必须能表达它（§4）。
2. **每个文件都原子写**（临时文件 + rename），因此部分失败的粒度是"某宿主的某个文件没写"，
   **永远不会出现半个文件**。
3. **默认结果是"什么都没发生"。** 更新失败不是异常路径，它就是最常见的正常路径（网络不通、验签不过、锁被占）。
   任何不确定的情况一律中止并保持现状 —— **updater 绝不阻塞用户**。

### 5.4 崩溃重入

- 整个应用过程一把持在打开句柄上的 OS advisory 锁。**文件存在永远不是权威。**
- hooks 和 context helper 必须读取"正在应用"的租约，在其被持有期间直接 no-op。
- 崩溃后重入：读 journal，校验活动树仍与 journal 记录的一致，然后要么续做要么回滚。若两者都不匹配，
  状态置为 `repair_required`，由下一个会话浮出来 —— 绝不静默猜测。

## 6. 宿主适配器契约 —— 已有什么，缺什么

**先回答"到底需不需要做模块化"：需要，而且大约 80% 已经存在了。**
[`scripts/aqg_client_registry.py`](../scripts/aqg_client_registry.py) 已经定义了 `ClientAdapterSpec`，
带 `installer_command`、`verify_command`、`uninstall_command`、`is_installed_command`、`skills_source`、
`skills_install_mode_default`、`hooks_surface`、`rules_surface`、`adapter_actions`，覆盖 20 个客户端。
**不要另建一套平行子系统，扩展这一套。**

缺口很具体：**`hooks_surface` 和 `rules_surface` 是散文。** 当前的实际值：

```
claude-code   hooks=('~/.claude/settings.json via scripts/install_aqg_hooks.py',)
codex         hooks=('~/.codex/hooks.json via scripts/install_aqg_codex_hooks.py',)
cursor        hooks=('.cursor/hooks.json via scripts/install_cursor_support.py',)
kimi-code     hooks=('Kimi Code config.toml hooks; official docs describe fail-open hook failures',)
pi            hooks=('Pi TypeScript extension for session_start/tool_call/...',)
qoder-cli     hooks=('Qoder settings.json with CLI lifecycle hooks, SessionStart, PreCompact, WIP save/recover',)
zed           hooks=('no managed hooks: official lifecycle-hook surface not verified',)
```

这些是给人看的句子，updater 没法据此做规划。四种截然不同的配置**格式**（Claude `settings.json` 合并、
Codex/Cursor `hooks.json`、Kimi `config.toml`、Pi TypeScript 扩展），外加一批完全没有 hook 面的宿主 ——
这种异构性正是需要一个类型化适配器边界的论据。

### 6.1 需要补的结构化字段

| 字段 | 取值 | 用途 |
|---|---|---|
| `hook_config_format` | `claude-settings-json` \| `hooks-json` \| `toml-table` \| `ts-extension` \| `none` | 选择渲染器 / 合并器 |
| `hook_config_path` | callable → `Path` | 绝不接受调用方传入的可变路径（DE 的规则） |
| `hook_delivery` | `managed-merge` \| `plugin-manifest` \| `none` | 合并进宿主配置 / 发一个文件 / 不支持 |
| `hook_entry_ownership` | 标记策略 | 如何在一堆第三方条目中识别出 AQG 自有的条目 |
| `hook_event_support` | 事件的 frozenset | 让规划能跳过该宿主根本没有的事件 |
| `skills_dest`、`skills_mode` | callable → `Path`，`link`/`copy` | 精神上已存在；把它变成机器可读的 |
| `rules_surface_spec` | 路径 + 块标记 | 与 hooks 同等对待 |
| `update_actions` | 下面四个动词 | `adapter_actions` 的更新专用扩展 |

### 6.2 每个适配器必须实现的四个动词

```
plan(state, target)   -> [Action]        # 纯函数。除读宿主配置外无 I/O。
apply(action)         -> Outcome         # 幂等。journal 由分发器落，不由适配器落。
verify(state)         -> Evidence        # doctor 消费的东西。
rollback(action)      -> Outcome         # 把该宿主配置恢复到应用前的字节。
```

严格沿用 DE 的分工：**分发器拥有词汇表、顺序、journal 和校验；适配器只拥有格式相关的变更机制。**
并且沿用 `validate_host_specs`：在 **import 时对整个注册表做 fail-closed 校验**，让"声明的元数据与可执行标志
自相矛盾"成为一个启动期错误，而不是某个用户机器上的运行时惊喜。

## 7. 第 5 类的细节 —— 合并进宿主自有的 hook 配置

这是唯一真正危险的操作。要求：

1. **所有权标记。** 一个 AQG 条目必须无需启发式即可识别。
   [`install_aqg_hooks.py`](../scripts/install_aqg_hooks.py) 已经通过匹配规范脚本路径片段做到了这一点，
   而且已经能**把一个偏离的 AQG 自有命令就地收敛回规范形态**（它的 "C6" 路径）—— 两者都复用，不要重造。
2. **绝不碰不属于我们的东西。** 第三方 hook 条目，以及我们不管理的事件下的条目，逐字节保留。
3. **保留用户的显式选择。** `upgrade.sh` 已经拒绝把显式的 warn-only opt-in 提升为阻断集。这个先例可以推广：
   一处被记录在案的、用户有意为之的偏离是**数据，不是漂移**。规划必须能把"跳过，用户 opt-out"和"失败"
   区分开来表达。
4. **写前备份**，用现有的 [`scripts/_aqg_backup.py`](../scripts/_aqg_backup.py)，并把写前字节留给 `rollback()`。
5. **四种操作**是：加条目、删条目、改目标名、改命令形状。改名和改形状这两种，正是需要已记录的 `hook_set_hash`
   才能判定的。
6. **与 tamper guard 的关系（已核实，不需要豁免）。** `pretooluse_aqg_tamper_guard.sh` 是一个 **PreToolUse
   hook，只拦 agent 的 `Write`/`Edit`/`MultiEdit` 工具调用**；updater 是 Python 进程、用普通文件 I/O 写盘，
   根本不经过 PreToolUse。**但威胁模型上有一层真实关系**：该 guard 防的正是"某个 agent 伸手进 `$AQG_ROOT`
   改掉一个 gate 文件（hook 脚本 / `_secret_patterns.py` / redaction 核心），先弄坏闸门再做闸门本会拦住的事"。
   **自动 updater 恰恰就是这个能力，只是被合法化、自动化了** —— 它有权写那些受保护文件，且 guard 看不见它。
   guard 自己的注释已经写死："这是纵深防御，不是边界；完全的防篡改需要**只读 / 已签名的安装路径**。"
   因此 updater 要么成为那条已签名的安装路径（把 guard 从"部分"补成真正的边界），要么成为绕过 guard 的
   最高价值攻击目标 —— **取决于 §9 的验签做没做。这使 §9 不是可选项。**

**第 5 类自动应用的策略。** 在全自动通道里没有人类确认这一步，所以**签名就是同意**（§9）。因此第 5 类
**仅在**下列条件全部满足时自动应用：release 在 `stable` 通道上通过签名验证、写前字节已捕获、该宿主适配器实现了
`rollback`、且该项被记入 `install-state.json` 以便 `doctor` 能报告"你的 hook 集合在 0.15.0 变了"。
任一条不满足，该项进 `pending[]` 等待，而不是强行执行。

> **次序，以及为什么清单里不需要带 hook 集合摘要。** 客户端判断 hook 集合成员是否
> 变化，靠的是比较已装树与新树——这之所以安全，完全取决于流水线的次序：**先验签名，
> 再拿清单验树，最后才读取或执行其中任何东西**。没有任何一步是从尚未认证的代码里
> 推导 class-5 成员的。写在这里，是因为 `internal/release/manifest.py` 的审计
> （aud_ifGNyyl_I7UTFOEJ）指出：按 YAGNI 砍掉每宿主摘要时，这个问题被留白了。

## 8. Skills：新增、更新、删除、重命名

- **所有权规则**（[`scripts/install.sh`](../scripts/install.sh) 里已实现，原样保留）：只删除**符号链接**，
  且其目标必须指向**本 checkout 自己的 `skills/` 目录**内部。绝不删真实目录，绝不删第三方链接，
  绝不删指向外部的链接。DE 的 `_prune_stale_routes` 用 `commonpath` 对自己的源根做同样的保证。
- **绝不覆盖真实目录。** DE 抛 `SkillRouteConflict` 并上报，而不是覆盖用户自己的同名 skill。照做 ——
  静默替换用户的目录是不可恢复的。
- **重命名**就是由状态文件 diff（§4）驱动的（prune，route）。载荷里没有重命名信号，也不需要有。
- **已经正确的路由保持不动**，不要重建。每次检查都churn 一遍符号链接会和并发会话竞争，且毫无收益
  （DE 在 `_route_skills` 里明确说了这一点）。
- **Copy 模式**在内容变化时必须重新拷贝；模式从按宿主记录的状态里读。
- **Windows**：用目录联结，并且受管 checkout 里必须钉住 `core.symlinks` / `core.autocrlf` ——
  DE 的 `updater.py` 把它们当作安装期不变量，这是对的。

## 9. 信任 —— 用什么顶替人类确认

全自动应用意味着"要不要升级？"这个提示没有了。而那个提示是唯一一处人类会检视"即将运行什么"的地方。
必须有东西顶上，因为 AQG 的 hooks 以用户身份在每次工具调用时执行任意 shell，**而且它们本身就是护栏** ——
一个拿下更新通道的攻击者，同时也拿下了报警系统。

启用任何自动通道之前必须具备：

1. **自动通道只跟签名 tag**（`stable`），绝不跟 `main`。`edge` = `main`，仅手动调用。
   `upgrade.sh --ref` 已经支持钉版本；缺的是默认值和签名本身。
2. **应用前验证**，对照**安装时钉死**的公钥集，不是现拉的。DE 的 `release_contract.py` +
   `release-trust.json` 是一份可用实现，带密钥吊销和单调 release **序号**（防回滚）。
3. **失败即关闭。** 签名无效、密钥被吊销、序号倒退 → 不应用、记录、下个会话浮出来。
4. **溯源。** 每次自动应用都把 tag、commit、key id、序号、触发器记进 `install-state.json`。
5. **保护 `main` + release 走评审**，因为一个 tag 的可信度取决于谁能创建它。

> 这把 [`INSTALL_VERSIONING.md`](INSTALL_VERSIONING.md) 里"签名 release tag 是计划中但非必需"
> 从"锦上添花"升格为**硬前置条件**。没有签名验证的自动更新，就是一条通往所有已安装机器、
> 且同时生效的静默远程代码执行通道。

## 10. 可以用什么来触发检查

下列方案全都调用同一个入口、共用同一个节流文件。它们可互换，且可以同时装多个而不冲突 ——
准入锁会让重复触发变成 no-op。

| 机制 | 覆盖宿主 | 何时触发 | 新增安装面 | 结论 |
|---|---|---|---|---|
| **`scripts/_aqg_context.sh`** | **全部 20 个** —— 任何能跑 skill 的宿主都会 source 它 | 首次调用 skill 时 | **无** —— 已被迁移过的 SKILL.md "How To Run" 块 source，且 `install.sh` 已经在它缺失时硬失败 | **基线方案。覆盖面最广，成本为零。** |
| **生命周期 hook**（`SessionStart`） | 有真实 hook 面的宿主（claude-code、codex、cursor、codebuddy、qoder-cli、kimi-code、trae、devin、pi） | 会话启动 | 无 —— 在已装的 hook 里多一行 | **有 hook 的地方的快路径。** 必须 spawn detached 并立即返回；SessionStart 是同步的。 |
| **MCP server 启动** | 任何支持 MCP 的宿主 | server 进程启动 | 一个需要用户配置的 server | **仅当我们本来就要发一个时才做。** DE 正是这么干的（`launcher.py` 的有界闸门）且效果很好 —— 但 MCP server 无法主动向模型推消息，所以它在这里的价值纯粹是"宿主会拉起这个进程"。不值得单为更新去建。 |
| **OS 调度器**（launchd / systemd timer / 计划任务） | 与宿主无关 | 真正每天一次，与是否使用无关 | 每个 OS 一个后台任务；DE 在 `stopper_launch_agent.py` 有先例 | **以后再说**，如果"不用也要每天更新"确实重要的话。安装面最重，要写三套 OS 实现。 |
| **`scripts/upgrade.sh`** | 任何有 shell 的地方 | 人手动跑 | 无，今天就存在 | **永久保留**，作为手动逃生通道和恢复路径。 |

**推荐：先做 context helper 触发，其次加 hook 触发，另外两个暂时不建。** 前者免费拿到完整宿主覆盖；
后者只是在有 hook 的宿主上改善延迟。两者都是同一个库的十行调用方 —— 这正是 §0 那条不变式的意义所在。

一条影响路线图排序的说明：context helper 触发在**完全没有 hook 面**的宿主上照样有效
（zed、trae-work-cn、qoderwake、workbuddy、kimi-work）。而这些恰恰是不做它就永远不会更新的机器。
**先做 hook 触发会把它们落下。**

### 10.1 hook 触发实际覆盖到哪些宿主

"有真实 hook 面的宿主"和"AQG 真的为它装了会话启动事件的宿主"不是同一个集合。有三个 managed-merge
宿主有 hook 面，但拿不到 hook 这一路的触发。这个缺口写在这里、而不是留给下一个读安装器的人去发现，
因为这个特性第一版只接了一个宿主，另外十二个是**静默**缺失的。

每个安装器各自持有自己的 hook 表，所以这个触发器接在 6 处、覆盖 12 个宿主。这个集合由
`tests/behavior/test_update_check_host_coverage.py` 钉住：某个宿主新拿到 hook 面而没人决定它该怎么办时，
该测试会失败。

| 宿主 | 安装器 | 挂在 | 覆盖 |
|---|---|---|---|
| claude-code | `install_aqg_hooks.py` | `SessionStart` | 是 |
| codex | `install_aqg_codex_hooks.py` | `SessionStart` | 是 |
| cursor | `install_cursor_support.py` | `sessionStart` -> `cursor_aqg_hook.py` | 是 |
| codebuddy、workbuddy-ai | `install_aqg_work_clients.py`（JSON） | `SessionStart` -> `cursor_aqg_hook.py` | 是 |
| kimi-code | `install_aqg_work_clients.py`（TOML） | `SessionStart` -> `cursor_aqg_hook.py` | 是 |
| qoder-cli、qoder-cli-cn | `install_aqg_qoder.py` | `SessionStart` -> `qoder_hook_adapter.py` | 是 |
| trae、trae-cn、devin | `install_aqg_agent_clients.py` | `SessionStart` -> `agent_client_aqg_hook.py` | 是 |
| pi | `install_aqg_pi.py` | `session_start` 扩展 handler | 是 |
| **qoder、qoder-cn** | `install_aqg_qoder.py` | —— | **否**：Desktop（`cli=False`）没有已验证的 `SessionStart` 面，AQG 根本不为它装会话启动事件 |
| **qoderwork** | `install_aqg_work_clients.py` | —— | **否**：`SessionStart` 与 Qoder CLI 的一致性未经证实，它的事件集止于 `PreToolUse` / `PostToolUse` / `Stop` / `UserPromptSubmit` |

cursor 家族的四个宿主是"每个生命周期事件挂一条 adapter 命令"，而不是在配置里点名 hook 脚本，所以对它们
来说触发器住在 `cursor_aqg_hook.py::_trigger_update_check` 里，而不是某一行配置。它是**启动后不等待**的：
那个 adapter 是整个事件唯一的一条命令，本身已经要花掉最多 20s 的 preflight 加 15s 的 WIP 恢复，而宿主给的
预算是 45s（Cursor）和 30s（work clients）—— 在这里等多久都是从一个本就吃紧的预算里扣；而 adapter 被中途
杀掉时，宿主一个 JSON 都拿不到。

有两个限制值得写下来，而不是留给别人去撞：

- **Windows 宿主装得上但跑不起来。** 启动器的前置条件是 `command -v python3`，而 Windows 的 Python
  装出来的是 `python.exe` / `py.exe`。context-helper 那一路卡的是同一个条件，所以 Windows 宿主两条路都
  够不着，只有跑 `scripts/upgrade.sh` 时才更新。
- **Codex 现在会自己触发那个让它自身信任 pin 失效的更新。** 每条 Codex hook 命令里都嵌了对
  `run_aqg_codex_hook.py` 和该 hook 脚本的 sha256，在 runner 加载前校验；一次成功的 apply 会换掉这些路径
  所指向的树，于是下一次 hook 会在还没运行时就 exit 3。该宿主上所有 AQG 闸门（包括四个 blocking 的
  `PreToolUse`）都会停止判定，直到重跑 `install_aqg_codex_hooks.py --apply` 并在 `/hooks` 里重新批准。
  这是 apply 流水线的性质，不是触发器的性质 —— 同一台机器上的 Claude Code 会话一样会打断 Codex 的 pin，
  而且从触发器上线那天起就能 —— 但把它挂到 Codex 上，意味着 Codex 现在能在无人看管时对自己做这件事。
  能真正堵上它的两件事都不在本节范围内：`pending[]` 目前没有任何地方呈现（§11 规定了那一行，
  `sessionstart_preflight.sh` 并没有带上），而 host reconciliation 把 hook 配置变更留给人处理、不会自动
  重新 pin。`test_codex_bundle_digest_matches_installed_pin` 能抓到过期的 pin，但只在装了 Codex 的机器上
  —— 它在 CI 里是 skip 的。
- **这三个未覆盖的宿主是在 skill 调用时被覆盖的，不是在会话启动时。**
  `scripts/_aqg_context.sh` 里的 `_aqgctx_nudge_update` 调的是同一个 `scripts.aqg_update.run` 入口，
  因此共用同一个节流文件，两条路不可能重复干活。它们不共用的是**时机**：一个从头到尾没调用过 AQG skill
  的 `qoder` / `qoder-cn` / `qoderwork` 会话，就永远不会检查。这比 hook 那一路弱，不是等价物。

## 11. 提示词面：这套机制需要改动什么（答案：几乎不需要）

AQG 是提示词密集型产品，任何新增文本都在和其它 skill 的 description 抢同一份注意力预算。
这里的纪律是："**当一切都是 proactive，proactive 便不再是信号。**"更新机制必须遵守同一条纪律。

| 面 | 需要改吗 | 说明 |
|---|---|---|
| 16 个 `SKILL.md` 正文 / description | **否** | 触发点加在 `scripts/_aqg_context.sh` 里，那是**被 source 的 shell**，不是提示词。SKILL.md 的 How To Run 块一个字都不用动。 |
| `CLAUDE.md` / `AGENTS.md` 受管规则块 | **否** | 更新是基础设施行为，不是 agent 需要遵守的纪律。 |
| hook 注入的 `additionalContext` | **仅条件性一行** | 见下。 |
| `aqg_doctor` 输出 | 是 | 但那是 CLI 报告，不进模型上下文。 |

**唯一需要新增提示词文本的地方**：当 `install-state.json` 的 `pending[]` 非空，或状态为
`repair_required` 时 —— 也就是**有事需要人处理**时 —— 在已有的 SessionStart preflight
`additionalContext` 里追加一行。成功的自动更新**不输出任何东西**（Owner 已定：全自动、不提示）。

这条规则值得写死成不变式：**提示词只在需要行动时出现。** 更新成功是无事发生，无事发生就不该占用上下文。

## 12. 仓库边界：什么留在本仓，什么不留

本仓是**开发仓**；实际打包发生在 B 仓。据此划线：

| 归属 | 内容 | 理由 |
|---|---|---|
| **本仓（A）** | 更新**客户端**：状态文件读写、规划器、事务/journal、宿主适配器、四个动词、触发器调用点 | 这些是要随产品装到用户机器上的代码 |
| **本仓（A）** | `install-state.json` 的 schema、宿主适配器契约 | 是契约，两边都要按它对齐 |
| **仓外** | **打包 / 签名 / 发布流水线**：manifest 生成、tag 签名、密钥保管、载荷分类计算（§1 第 5 类判定）、镜像分发 | 只在发布侧运行，永不下发给用户；且涉及私钥，不能进开发仓 |

建议放在 `../aqg-release/`（与本仓平级的独立目录），最终归属 B 仓。

> **决定（2026-09-02，Owner）：发版签名 skill 放在本仓内，但不推到 B 仓。**
> 位置 **`internal/release/skills/aqg-release/`**，理由是双重私有：
> ① 它不在 `internal/carve/allowlist.txt` 里（该白名单是显式的
> "a path here ships public"，省略即私有）；② 它在 `internal/` 下，而 `internal/` 目前在白名单里有
> **0 条**，是既定的私有区。不是只靠"记得别加进白名单"。
>
> 放 `internal/` 而不是 `skills/` 还避开三件事：`skills.list` 的花名册守卫、`scripts/install.sh` 里
> 那个 16 个 skill 的硬编码数组、以及误装给用户。Owner 自己机器上手动建一条符号链接进 `~/.claude/skills/`
> 即可使用 —— 只有一个人用，不值得为它改安装器。
>
> 做法与 DE 的 `permanent_setup.py` 一致：掩码弹窗索取**口令**（不是密钥材料本身），口令不进 argv、
> 环境变量、shell history 或任何持久化配置，签完立即清内存。签两样东西 —— `git tag -s`（给人看，
> `git verify-tag` 一眼可查）和 detached manifest 签名（给客户端 `trust.py` 用，防回滚的序号只能靠它）。

**必须留在本仓的一样东西**：钉死的**公钥集**（DE 的 `release-trust.json` 对应物）。它随产品下发，是客户端验签的依据 —— 只含公钥，不含私钥。

## 13. 实施计划：分 6 个 PR

依赖关系：PR1 → PR2 → PR3 → PR4 → PR6；**PR5 独立，可与 PR1–4 并行**，但 PR6 依赖它。

| PR | 内容 | 交付后能做什么 | 改变现有行为吗 | 审计档 |
|---|---|---|---|---|
| **1** | `state.py`（`install-state.json` 读写 / 原子 / 0600 / schema 自迁移）+ `aqg_client_registry.py` 结构化字段（含已排期的 D1 `rules_surface`） | 能记录和读取安装态；`doctor` 能报各宿主版本 | **否** —— 只加字段、加文件 | **deep** |
| **2** | `hosts/base.py` 四动词契约 + `claude_code` / `codex` / `cursor` / `generic` 四个适配器，**包住**现有安装脚本 | 能用统一接口问"这个宿主要改什么"（`plan`）、"现在装的是什么"（`verify`） | **否** —— 现有 CLI 全部保留 | **deep** |
| **3** | 版本树布局（`git worktree`）+ `stage.py` + 原子符号链接切换 + `lock.py` + `_aqg_context.sh` 导出 `realpath` | 能手动把安装切到另一棵版本树并切回 | 轻微 —— `aqg_root` 变为解析后路径（§5.2） | **deep** |
| **4** | `plan.py` + `transaction.py`（journal / 相位 / 回滚）+ `dispatch.py` + `skills_route.py` | **完整 dry-run**：算出这次要改什么、归第几类；`--apply` 需显式传入，尚未自动 | 否 | **deep** |
| **5** | `internal/release/skills/aqg-release/` + manifest 格式 + 公钥集落地本仓 | 能打出带签名的 `stable` tag | 否 —— 不下发用户 | **deep** |
| **6** | `trust.py` + `acquire.py` + 触发器接入（`_aqg_context.sh` / SessionStart）+ `doctor` 报 `pending` + `upgrade.sh` 内部改调 | **自动更新上线** | 是 —— 这是唯一真正打开自动的一步 | **deep** |

### 每个 PR 的验收线

- **PR1** —— 状态文件写坏 / 被截断 / schema 是未来版本时，读取端 fail-closed 且不崩；注册表新字段的
  fail-closed 校验在 import 时触发；现有安装 / 校验 / 卸载测试全部与基线一致。
- **PR2** —— 一套**契约测试跑遍所有适配器**（同一组断言，每个宿主各跑一遍）；`plan` 是纯函数、不写盘；
  `rollback` 能把宿主配置还原到写前字节。
- **PR3** —— 切换期间并发跑 hook 不读到半更新的树（这条要有实测，不能只靠推理）；`rename` 失败时状态不变；
  清理绝不删当前指向的那棵和 journal 引用的那棵。
- **PR4** —— **崩溃注入测试**：在每个相位杀进程，验证下次重入要么续做要么回滚，且都不留下半配置状态；
  部分失败（某宿主写不进去）能正确落进 `pending[]` 且其余宿主保持新版。
- **PR5** —— 口令不出现在 argv / 环境变量 / shell history / 任何落盘文件（用本仓已有的 secret-scan 验）；
  签完自验一遍（验不过即打包错误）；`internal/release/` 不在 carve 白名单里（加一条测试钉住）。
- **PR6** —— 验签失败 / 序号倒退 / 密钥被吊销时**一律不应用**；离线时静默降级；
  `AQG_NO_UPDATE_CHECK=1` 生效；触发器不阻塞会话启动（有超时实测）。

> **更正（2026-09-02，实施 PR1 时发现）：六个 PR **全部**是 `deep`，没有 `standard` 的切片。**
> 初稿把 PR1/PR2 标成 `standard` 是错的 —— 按
> [`docs/policies/audit-trigger.md`](policies/audit-trigger.md) 的 Gate A，
> "**data model**, schema, or migration" 和 "install / integrity machinery" 各自都直接路由到 `deep`，
> **且与改动行数无关**。`install-state.json` 两条都命中；宿主适配器命中后者。
> 这不是流程繁琐，而是这个特性的性质：**它整体就是安装完整性机器**，没有便宜的部分。
> 把某个切片标成 `standard` 只会诱使实施时跳过本该做的审计。

### 排序理由

PR1/PR2 **不改变任何现有行为**，可以先合入并在真实使用中跑一段时间，验证状态记录和 `plan` 的判断是否与
现实一致 —— 这两步是后面所有判断的事实来源，错了后面全错。

PR3/PR4 之后系统**已经能完整算出并执行一次更新**，只是必须手动触发。这是个有价值的中间状态：
`upgrade.sh` 可以先切到新引擎，拿到事务和回滚能力，而**自动通道仍然关着**。

PR6 是唯一打开自动的一步，且它**硬依赖 PR5** —— 没有签名就不能开自动通道（§9）。这个依赖是刻意的：
把"能自动"和"验得了签名"绑死在同一个前置条件上，避免出现一个临时的、无签名的自动路径。

## 14. 待决事项

- [ ] 签名密钥的**轮换与紧急吊销**。保管与打 tag 权限已定（见下）；**未定**的是：
  一台已安装的机器，怎么知道它已经信任的某个密钥被攻破了。钉死的公钥集能保证
  「不在名单里的密钥永远签不动发布」，但 `revoked` 标记只能保护**已经收到吊销
  信息**的那台机器——而吊销信息本身，要走的正是被攻破的那把密钥所控制的那条
  更新通道。要堵住它，需要一样发布密钥自己伪造不了的东西：一把离线的根密钥专门
  给公钥集签名，或者要求两把密钥的门限签名。这里写成具体形态，是因为
  `scripts/aqg_update/trust.py` 的审计（aud_EqcBMQUXLJFRgS3o）把它评为 critical，
  也因为它是唯一一个「密钥泄漏那天，会从一段文字变成一次事故」的缺口。
- [ ] 代码组织：新增的更新层按宿主拆分成 `hosts/<client>.py`（DE 的形状），还是并入现有单文件注册表。
      倾向拆分，但硬约束是**不得影响现有安装 / 校验 / 卸载功能**，因此现有 `aqg_client_registry.py`
      只加字段、不改结构，拆分只发生在新增的 update 层。
- [x] ~~`state_root` 的位置~~ —— **已定（2026-09-02，暂定）**：仓外，`~/.deeppattern/aqg-state/`，见 §4。
      仍需在 Windows 上实测该路径可写、且不被漫游配置同步。
- [x] ~~updater 的 tamper-guard 豁免~~ —— **已澄清**：不需要豁免（guard 只拦 PreToolUse 工具调用），
      但它使 §9 的验签成为必需项而非可选项。见 §7.6。
- [x] ~~保留策略~~ —— **已定（2026-09-02）**：保留 2 棵（当前 + 前一个），约 60 MB。见 §5.1。
- [x] ~~签名密钥保管~~ —— **已定（2026-09-02）**：Owner 本人保管，`stable` tag 只由 Owner 打，
      签名动作封装为 `internal/release/skills/aqg-release/`。见 §12。
- [ ] `edge` 是否允许在内部机器上自动。**建议：只手动。** 签名验证的价值全在于"没有例外" —— 一旦存在
      一条合法的无签名自动路径，它就是信任模型上的一个洞，而"这台机器是内部的"只是一个配置字段，
      没有任何技术强制力。内部同步慢半拍，跑一条 `upgrade.sh --ref main` 就能解决。
- [ ] `_aqg_context.sh` 导出 `realpath` 解析后的路径（§5.2 的世代混用修法）—— 十行改动，但要确认
      不破坏现有 16 个 SKILL.md 的任何假设。
