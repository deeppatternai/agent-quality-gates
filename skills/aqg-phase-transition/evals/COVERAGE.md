# aqg-phase-transition Eval 覆盖矩阵

该套件使用 `skill-up` 的确定性 `expect` 与 `rule_based` judge，验证 Agent
安装 `aqg-phase-transition` 后是否能正确解释并应用 `SKILL.md` 的规范性契约。
它与 `tests/test_phase_transition.py` 分工如下：

- Agent eval：验证触发判断、路由解释、边界意识和 caller 动作。
- Python test：验证解析器、dedup、状态文件和 CLI 实现细节。

## 规范条款覆盖

| `SKILL.md` 契约 | Agent eval | Python 实现证据 |
|---|---|---|
| 三个 phase boundary、timing signal、signal-only、caller 负责审计 | `phase-boundary-purpose` | `test_matrix_full_coverage` |
| preflight / phase transition / closeout 分层；显式 artifact audit 与 per-edit 排除 | `alternative-tools-and-per-edit` | N/A，属于调用边界 |
| AQG root 解析、本地 emit/query/override、cloud thin-client 命令 | `cli-command-contract` | CLI smoke/self-test |
| verdict 的 mode、reason、matrix、override、floor、confirmation 与 dedup 字段；caller 按 mode 行动 | `verdict-field-contract`、`workflow-actions-by-mode` 及各 flag 场景 | emit/override tests |
| depth 只来自 policy，phase 不参与 depth；trivial/moderate/high 基本路由 | `phase-does-not-set-depth`、`plan-moderate-standard`、`implementation-high-deep`、`tests-trivial-skip` | policy single-source tests、matrix tests |
| high 默认直接为 deep，不应误标 floor | `high-default-deep-no-floor` | `test_high_column_is_deep_directly_not_floor_rescued` |
| high 的较浅非 skip 请求被 floor 提升；moderate 不受 floor 影响 | `high-quick-scan-safety-floor`、`moderate-quick-scan-no-floor` | safety-floor tests |
| high 显式 opt-out 需要二次确认；moderate opt-out 与 dedup skip 不需要 | `high-skip-second-confirmation`、`moderate-skip-no-confirmation`、`dedup-high-no-confirmation` | high-skip / dedup-skip confirmation tests |
| dedup 三元组、5 分钟窗口、rank 条件、记录值语义 | `dedup-rank-window` | dedup unit tests |
| 同级或更浅请求命中；更严格请求绕过；key/window 任一不匹配不命中 | `dedup-hit-same-or-lower`、`dedup-stricter-bypass`、`dedup-key-or-window-miss` | dedup hit/miss/rank tests |
| 全部英文 user signals 与中英文等价映射 | `english-user-signals`、`bilingual-user-signals` | parse-user-signal tests |
| trivial / moderate / high 分类与全部 sensitivity / complexity gates | `stakes-classification`、`trivial-classification-examples` | acceptance matrix tests |
| 分类不明确时读取 policy、caller 保守选择、server hint 非权威 | `unclear-stakes-caller-classifies` | N/A，属于调用责任 |
| canonical phase 顺序无 warning；跳阶段 fail-loud、非 fail-stop | `canonical-phase-order`、`phase-order-warning` | canonical / skip / regression transition tests |
| 状态位于项目根目录、per-task、短 hash 防碰撞、query、本地不上传 cloud | `state-persistence-and-query` | state roundtrip / isolation / collision tests |

## 27 个 Agent 用例

| 能力面 | 用例 |
|---|---|
| 核心职责与三个 phase | `phase-boundary-purpose` |
| moderate 计划路由 | `plan-moderate-standard` |
| high 实现路由 | `implementation-high-deep` |
| trivial 测试路由 | `tests-trivial-skip` |
| phase 不决定 depth | `phase-does-not-set-depth` |
| high quick scan floor | `high-quick-scan-safety-floor` |
| moderate quick scan 无 floor | `moderate-quick-scan-no-floor` |
| high 默认 deep 无 floor 标记 | `high-default-deep-no-floor` |
| high opt-out 二次确认 | `high-skip-second-confirmation` |
| moderate opt-out 无二次确认 | `moderate-skip-no-confirmation` |
| dedup high skip 无二次确认 | `dedup-high-no-confirmation` |
| dedup 规则说明 | `dedup-rank-window` |
| dedup 同级/更浅命中 | `dedup-hit-same-or-lower` |
| dedup 更严格请求绕过 | `dedup-stricter-bypass` |
| dedup key/window 不匹配 | `dedup-key-or-window-miss` |
| stakes 全量分类 gate | `stakes-classification` |
| trivial 全量示例 | `trivial-classification-examples` |
| 模糊 stakes 的保守分类 | `unclear-stakes-caller-classifies` |
| 本地与 cloud 命令 | `cli-command-contract` |
| 非法 phase 顺序 warning | `phase-order-warning` |
| canonical phase 顺序 | `canonical-phase-order` |
| 替代工具与非 per-edit 边界 | `alternative-tools-and-per-edit` |
| verdict 字段契约 | `verdict-field-contract` |
| caller 按 mode 行动 | `workflow-actions-by-mode` |
| 本地状态与 cloud 边界 | `state-persistence-and-query` |
| 中文 user signals | `bilingual-user-signals` |
| 英文 user signals | `english-user-signals` |

## 判定策略

- 每个用例只覆盖一个主要行为面，失败时可直接定位到对应契约。
- 每个 prompt 明确要求加载已安装 Skill，并仅禁止读取工作区其他项目文件、运行审计或
  修改文件；不得使用“不要调用工具”阻止 Agent 读取 Skill 本身。
- 场景用例采用固定字段作答，但不在 prompt 中给出正确值；正确答案只保留在
  `expect` 中，避免把复述答案误当成推理通过。
- 对 mode、布尔 flag 和路由动作同时设置正向与负向断言，减少包含多个候选值时的
  假阳性。
- 使用 `expect.must_contain` / `must_not_contain` 约束稳定字段，并用
  `rule_based.output_matches` 接受连字符、下划线、同义展开和中英文单位等价形式；避免
  不必要的 `agent_judge` 成本与波动。
- 真实模型通过率、耗时和波动必须来自 `skill-up run` 产物；`validate`、
  `list-cases` 与 `--dry-run` 只证明配置、发现链和执行计划正确。
- 单用例 timeout 设为 600 秒，用于容纳本机 Codex 启动、插件探测和偶发重试；超时仍记为
  runner error，不得当作 Skill 行为失败或通过。

## 非 Agent-eval 范围

以下属于脚本实现细节，由 Python 测试负责，不重复消耗模型评测：artifact 内容规范化、
空 artifact 拒绝、未知或否定 user signal 解析、状态原子写入、并发 task 隔离、legacy
记录兼容、override 与 dedup cache 同步。它们不构成 `SKILL.md` 对调用 Agent 的额外
行为契约。
