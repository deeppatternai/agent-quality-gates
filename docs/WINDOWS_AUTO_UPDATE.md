# Windows 安装与自动更新

安装成功、更新检查成功和新版本已生效是三个不同结果。卸载重装后，应验证完整链路：安装入口调用统一 wrapper，固定入口连接到版本目录，客户端加载更新触发器，签名发布通过验证，固定入口切换到新版本。

## 安装入口

| 来源 | AQG 安装步骤 |
|---|---|
| DE 的 AI_SETUP.md | DE install.sh 调用 install_aqg_clients.py |
| DE 一行脚本 | de-aqg-install、dp-install.sh 或 dp-install.ps1 最终调用同一 wrapper |
| AQG 自身 AI_SETUP.md、README 命令 | install_aqg_clients.py --apply |

DE 必须能复用 AQG 的受管符号链接和 Git worktree 的 `.git` 文件；不能在活动版本中执行 checkout/pull。底层单客户端安装脚本只负责客户端文件，不负责把普通 checkout 转为可自动更新的布局。

Windows 需要原生 Python、Git Bash，以及创建目录符号链接的权限（开发者模式或管理员权限）。默认位置为 `%USERPROFILE%\.deeppattern\agent-quality-gates`，它连接到同级 `versions/<commit>`。skills、hooks 和 rules 中的路径应保留固定入口，不应固定到某个 commit。复制模式的 skill 不会随入口更新。

本功能的系统基线为 Windows 10 1709 或更新版本、Windows 11，安装在支持目录符号链接的本地文件系统上。原子替换依赖 `FileRenameInfoEx` 的扩展重命名语义；Microsoft 将对应的 `FileRenameInformationEx` 操作列为从 Windows 10 1709 起提供。旧版 Windows、各 Windows Server 版本和网络文件系统尚未验收，不能仅凭安装成功判断支持自动切换。[Microsoft API 说明](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/wdm/ne-wdm-_file_information_class)

初始 clone 必须使用 `--config core.autocrlf=false --config core.eol=lf`。这是该 checkout 的设置，不修改用户全局 Git 配置；否则 Windows 的换行转换会让安装时记录的 hook 摘要与以后签名版本的字节不一致。避免把安装放进很深的自定义用户目录：Git for Windows 的 worktree 内部路径仍有长度限制。

wrapper 在所有安装命令成功后转换目录。若其他应用占用目录，转换会拒绝并保留安装；输出 `automatic updates are NOT enabled` 时，不能把此次重装报告为已具备自动更新能力。

## 自动更新的边界

- SessionStart 触发器在后台检查签名 stable 发布，默认检查间隔为 30 分钟；失败、中断和已回滚的尝试通常在 5 分钟后重试，也可通过 `AQG_NO_UPDATE_CHECK` 关闭。人工执行更新模块时可用 `--force-check` 仅跳过时间冷却，签名、发布序号、事务、宿主证据和冒烟检查仍然执行。客户端须实际加载并允许执行对应 hooks。
- skill 使用时的 context helper 只检查版本，不切换；没有 SessionStart 更新触发器的客户端不能仅凭此检查宣称能自动应用更新。
- 已安装的 hook 配置完整、skill 名单无需调整时，普通内容更新可以直接切换，并记录 commit 与发布序号。Claude Code 按目标树验证且要求安装定义不变；Codex 的固定入口命令还要求摘要覆盖的文件字节不变。
- hook 定义或 Codex 校验摘要变化、skill 新增/删除、配置异常时仍返回 `pending`，需要协调客户端配置。下一次更新不会因历史 pending 直接拒绝，而会针对新目标重新检查真实状态并生成新计划；仍然存在且无法安全协调的配置差异不会被强行绕过。
- 手动升级从根目录切换到宿主配置完成期间持有独立协调锁；失败时同时恢复旧根目录和旧安装状态，避免自动更新与手动 Hook 重装交错。
- 旧 Codex 安装若命令直接指向真实版本目录，可以继续运行旧 hooks，同时允许其他客户端接收更新；这些旧目录受清理保护。经目录链接访问的命令不能使用此例外，否则版本切换会使旧摘要失效。
- 签名、文件清单、发布序号及回滚检查保持有效；开发分支提交不会自动成为用户可收到的 stable 发布。

## hooks 如何跟随更新

客户端配置保存的是固定路径，例如 `agent-quality-gates/scripts/cursor_aqg_hook.py` 和固定的 `--aqg-root`。更新下载并校验新的 `versions/<commit>` 后，原子切换 `agent-quality-gates` 这一个目录链接。同一条已保存命令下一次执行就会访问新版本，无需每次修改每个客户端的 JSON/TOML 配置或 Pi 扩展。

因此，安装器不能用 `resolve()` 把持久化命令写成 `versions/<旧 commit>/...`。运行中的单次 hook 可以固定到它开始执行时的版本；这与配置文件应保留固定入口是不同阶段的需求。

目前更新引擎的专用 hooks 校验/协调适配器只有 Codex 和 Claude Code，另外有无 hooks 客户端的通用适配器。其余客户端虽然有安装器和下表中的触发器，但尚不能承诺自动识别并修复它们的配置结构变化。自动检查不会直接重写客户端配置；新增事件、命令格式变化和 Codex 摘要变化仍需显式协调。

## 当前 AQG 实现的 hooks 支持

以下为仓库安装器和 registry 的支持范围，不代表已在所有厂商客户端版本中做过 UI 验收。

| Agent / 配置 ID | hooks 交付 | SessionStart 更新触发器 | 限制 |
|---|---|---|---|
| Codex | hooks.json | 有 | 命令带摘要校验；变化时需刷新信任 |
| Claude Code | settings.json | 有 | 共享 shell hooks |
| Cursor | hooks.json | 有 | 通过 Cursor adapter 转换事件 |
| CodeBuddy、WorkBuddy AI | settings.json | 有 | 两个独立配置目录 |
| Kimi Code | config.toml | 有 | 部分支持，hook 执行失败默认放行 |
| Qoder CLI、Qoder CLI CN | settings.json | 有 | CLI 事件集 |
| Qoder、Qoder CN 桌面版 | settings.json | 无 | 无 SessionStart、PreCompact 和 WIP hooks |
| TRAE、TRAE CN | hooks.json | 有 | 无 PreCompact |
| Devin | CLI hooks 配置 | 有 | 只能在压缩后提醒 |
| QoderWork | settings.json | 无 | 仅 PreToolUse、PostToolUse、Stop、UserPromptSubmit |
| Pi | TypeScript 扩展 | 有（session_start） | 通过扩展交付，部分支持 |

共 15 个带 hooks 的配置。WorkBuddy（`workbuddy`，不是 `workbuddy-ai`）、TRAE Work、TRAE Work CN、Kimi Work、Zed、QoderWake 共 6 个配置没有 AQG 已验证的 hooks 交付。没有 SessionStart 的客户端可以通过共享安装入口看到其他客户端触发的更新，但不能独立依赖会话启动自动应用更新。

三个计数应分别理解：15 个配置交付 hooks，12 个配置交付会话启动更新触发器，2 个配置具有更新引擎的专用 hooks 校验/协调适配器。其余 13 个 hooks 配置尚未接入该协调器，这是现有支持边界；本次修复让它们保存的命令随固定入口切换，并未新增这些适配器。

## 发布验收

修复必须进入实际使用的 AQG 和 DE 安装源，并按 AQG 发布流程更新签名 stable 元数据，之后才能让用户卸载重装。仅修复机器上的旧 checkout 不满足这个要求。

`tests/behavior/test_reinstall_auto_update.py` 使用临时用户目录、真实安装器和本地测试签名源，覆盖卸载重装后实际收到下一版本，以及 hook 摘要变化时保留旧版本。测试不替换 planner、签名验证或版本切换，也不证明客户端 UI 已加载 hooks。

`tests/behavior/test_hook_update_roots.py` 覆盖另外 13 个 hooks 配置：保持已生成命令不变，切换入口后实际执行无副作用的 adapter 探针，确认程序路径和根目录参数均访问新版本。

Windows 发布前应在隔离配置中运行以下关键回归；统计来自不同测试集合时不能相加为互不重叠的覆盖数量。

| 测试文件 | 关键行为 |
|---|---|
| `tests/behavior/test_reinstall_auto_update.py` | 两个真实生命周期场景：重装后签名更新及后台再更新；摘要变化保持 pending |
| `tests/behavior/test_hook_update_roots.py` | 13 个客户端配置保存的命令跨版本执行新 adapter 和新根目录 |
| `tests/behavior/test_windows_update_runtime.py` | 原生目录迁移、切换与回滚、文件占用拒绝、迁移与回滚双失败诊断、后台环境 |
| `tests/behavior/test_update_skills_route.py` | 路由所有权、原始链接文本及外来链接拒绝 |

示例：`python -X utf8 -m pytest tests/behavior/test_windows_update_runtime.py tests/behavior/test_update_skills_route.py -q`。这些用例直接覆盖 Windows 文件系统行为，不能替代各厂商客户端 UI 加载 hooks 的验收。

公开发布采用逐文件白名单。新增的 `scripts/aqg_update/hosts/hook_inputs.py`、三份回归测试（`test_hook_update_roots.py`、`test_reinstall_auto_update.py`、`test_windows_update_runtime.py`）和本文均须包含在 AQG 发布投影中；DE 的两份新增安装回归也须加入其发布白名单并更新内容摘要。开发源码中存在文件，不代表公开安装源会包含它。
