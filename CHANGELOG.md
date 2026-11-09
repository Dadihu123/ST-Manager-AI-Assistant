# CHANGELOG

## 未发布

基于 0.4.0。这一轮修的是三个**长期运行才会显形**的问题：用久了变卡、历史记录
无限积累、以及 `ui_data` 命名空间顶到宿主配额后静默丢数据。诊断过程与实测数字见
`tmp/audit/ANALYSIS.md`（含可复跑脚本）。

### 修复：用久了每次保存都变慢（问题 3）

- **`sync_card_meta_ui()` 的 O(N²) 裁剪**：原实现在超出体积自限后「每删一条就
  `json.dumps` 整个字典」，成本 ∝ 条数 × 200KB。改为按 `updated_at` 倒序**一次
  遍历**累加体积、装不下即停（保留的永远是最新的那批）。
  实测缓存 800 条时单次同步 **787ms → 2ms**；1200 条时 **699ms → 9ms**。
  注意：体积必须按 UTF-8 **字节**算而不是字符数（概览以中文为主，3 字节/字，
  用 `len(str)` 会低估约 1.5 倍，热缓存反而超出预算）。

- **批量写入不再逐条重写命名空间**：新增 `store.batch_card_meta()` 作用域，
  `update_card_meta()` 在块内只追加 jsonl，退出时统一同步一次热缓存。
  `ai.analyze` 作业与 `/jobs/<id>/apply` 已接入 —— 宿主每次
  `private.update()` 都是「深拷贝 + 序列化测配额 + 4 代备份轮转 + fsync」，
  300 张卡的作业原来是 300 次全量写盘。实测「落地 20 张卡」
  **14.2s → 155ms**（缓存 1000 条时）。

- **读接口不再写 `ui_data`**：`GET /profiles` 每次调用都会
  `sync_profile_index()`（全量重写命名空间）。索引由 `save_profile` /
  `delete_profile` / `reset_profiles` / `import_profiles` 维护，读路径不需要写。
  打开一次面板就多一次全量写盘（超配额时还会每次刷一行「写入被拒绝」）。

- **标签分类索引在作业内复用**（0.4.0 的性能放大器）：`analyze_one()` 原来每卡
  跑两次全量 `classify_tags()`（`tagpool.protected_tags()` 内部又算一次），成本
  随标签数近似平方增长。现在整批算一次并传下去
  （`protected_tags(..., index=…)`）。实测 3000 个标签 / 300 张卡：
  **118ms/卡 → 62.8ms/卡，省 16.6 秒（46.8%）**。
  注：剩余主要成本 `build_pool_text()`（62ms/卡）是 0.3.0 就存在的固定开销，
  不在本轮范围内。

### 修复：历史记录无限积累（问题 1）

用户原话：「最近的作业记录貌似会不断积累，希望能够添加个手动清除，如果当前会无限
积累的话，请也增加以下上限，到了上限后自动老化旧的记录」。

审计结论：`runs` 台账本来就有 50 条上限；真正会无限涨的是另外三处。

- **`usage.jsonl` 按条数裁尾**（`USAGE_JSONL_MAX_RECORDS = 2000`）。原来只有
  「8MB 压缩」，而用量记录没有 `ui_key` → `_compact_jsonl` 退化成 `_anon_i`，
  **一条都删不掉**（实测 37117 条追加后压完还是 37118 条）；越过 8MB 后每次写入
  都要整文件重读重写（244ms）。
- **`pending_reviews` 加上限 + TTL**（50 条 / 14 天）。原来只在 apply「有落地」时
  清，取消 / 失败 / 一直没审的作业永久残留；宿主还会自己清理终态作业，被清掉的
  run 再也等不到 apply（1000 条 = 79.1KB，2528 条就吃掉 200KB）。
- **`tag_suggestions` 的键加上限**（`TAG_SUGGESTIONS_MAX = 200`）。原来只限
  每个标签的 `cards` 列表长度，键无上限（2000 个标签名 = 242.9KB）。
- **手动清除入口**：
  - `DELETE /jobs/<run_id>`（复用已实现但零调用的 `forget_run`，连带清掉该 run
    的待审批登记）+ hub「最近作业」每行的「删除」；
  - `POST /jobs/clear` + hub「最近作业」标题旁的「清除」；
  - `POST /usage/clear`（默认保留累计台账，`reset_ledger=true` 才归零）；
  - `POST /pending/clear`（可定点清 `run_ids`）+ 待审批卡上的「清空待审批登记」；
  - 设置页「用量台账」区新增「清空用量明细」与「连同累计一起归零」。

- **顺带修掉一个界面 bug**：设置页的 `usageBox` / `suggestionBox` 从未挂进各自的
  section（孤儿节点）——「用量台账」与「新标签建议」两区在界面上一直是空的，
  用户既看不到用量也点不到清空。前端冒烟测试抓到的。

### 修复：`ui_data` 顶到配额后静默丢数据（问题 2）

用户看到的日志：`插件 stm-ai-assistant 的 ui_data 命名空间已达 232245 字节
（上限 262144），即将被拒绝`。实测构成 = `card_meta` 顶在 200KB 自限 + 其余子键
约 27.7KB。

- **热缓存预算改为动态**（`card_meta_budget()`）：不再固定占 200KB，而是
  「宿主配额 − 其它子键实际占用 − 24KB 安全垫」，下限 16KB。
  原来一个子键固定吃掉配额 78%，剩下 57KB 要装 8 个子键，吃穿后宿主**整笔拒绝**
  所有写入（连待审批角标都刷不动）。
- **写入失败不再静默**：`_mutate()` 失败时记 warning 并累计到
  `last_write_state()`；`/status` 新增 `ui_data` 字段；hub 与设置页在失败时显示
  提示与出路。原来所有调用方都丢掉返回值，`jobs.py` 外层 `try/except` 包的是
  **不会抛异常**的调用，永远不触发 —— 用户只能看到「界面不动了」。
- `/jobs/<id>/apply` 在本地记账写失败时回传 `warnings`：卡片确实写了，但
  `applied_runs` / 待审批清除没落地，不说的话下次同一批会被重复落地。

### 回归用例（新增 55 条）

`tests/test_card_meta_scaling.py`（性能守卫 + 保留最新）、
`tests/test_card_meta_batching.py`（批量只写一次 / 异常也 flush / 嵌套）、
`tests/test_read_paths_do_not_write.py`、`tests/test_retention_and_clear.py`、
`tests/test_clear_endpoints.py`、`tests/test_ui_budget_and_visibility.py`、
`tests/test_status_write_state.py`、`tests/test_clear_entries_frontend_contract.py`、
`tests/test_tagpool_reuse.py`；
`tests_frontend/smoke.mjs` 新增 5 条清除入口用例。
合计 **1011 passed / 前端冒烟 34 项全过**。

### 修复：打标作业的单卡本地 CPU（3000 标签 129.8ms → 10.4ms）

`ai.analyze` 每张卡的**本地**开销（LLM 网络之外）在 3000 个标签的库上实测
**129.8ms/卡 → 10.4ms/卡（-92%）**，300 卡作业的本地 CPU **39s → 3.1s**。
这段 CPU 跑在宿主进程里（Flask 单进程 + GIL），所以省下的是**宿主卡顿**
（打标时整个软件卡住），不是作业等待 API 的时间。

- **`_to_str_list()` 的 O(N²) 去重**：`text not in result` 是 list 上的线性
  查找。改为 set 记账，语义完全等价（保留首次出现的顺序、先 strip 再去重）。
  实测 3000 个标签单次 **28.85ms → 0.28ms**；6000 个 **109ms → ~1ms**
  （`tests/test_tagpool.py` 有边界等价用例 + 6000 标签的线性守卫）。
- **`resolve_candidates()` / `build_pool_text()` 接受现成分类索引**：
  `classify_tags()` 每卡原本被调 4 次（每次 `build_pool_text` 内部两次）。
  作业开头算一次，顺着 `analyze_one(tag_index=…)` 传下去。
  每卡调用 **4.0 → 0 次**。
- **`collect_variables()` 接受现成的 `tags.library` 文本**：`analyze_one()` 为了
  硬过滤本来就算过一次 `build_pool_text()`，`tag_variables()` 又用同样的入参
  算第二遍，只为取那段文本。每卡 `build_pool_text` **2.0 → 1.0 次**；
  `/prompt/preview` 同样接入。
- **`tags.library` 逐字节不变**：两条路径在同一组输入下（含作业注入的
  `_isolated` 键）`text` / `listed` / `dropped` / `excluded` / `outline` 全等，
  用测试钉住 —— 这是发进提示词的标签清单，改一个字节就会改变模型行为。
- **索引只在作业内复用**：`tag_library` 本来就是作业开头 `read_tag_library()`
  读一次的快照，索引随它同生共死；没有任何模块级缓存，跨作业不会串味。
- 小标签库不回退：76 个标签 **1.7ms/卡 → 1.2ms/卡**。
- 剩余 10–11ms 已是「一次建池」的固有成本（3000 个标签 × 词面重叠打分 +
  逐行 token 估算），不再是重复计算。再往下压要改排序/估算算法本身，
  不在本轮范围（本轮只做重复开销）。

### 回归用例（本轮新增 21 条）

`tests/test_tagpool.py`（列表规范化边界 13 条 + 6000 标签线性守卫）、
`tests/test_tagpool_reuse.py`（`index=` 复用与两条路径输出相等）、
`tests/test_prompt.py`（`tags.library` 逐字节相等 / 不重算 / 默认行为不变）、
`tests/test_jobs.py`（整个作业只 classify 一次、每卡只建池一次）、
`tests/test_analyze_local_cost.py`（端到端单卡本地开销守卫）。
合计 **1032 passed / 前端冒烟 34 项全过 / 插件契约 0 error**。

## 0.4.0（2026-11-08）

基于 v0.3.0。这一版主要是「让设置真的生效」：修掉配置页改了不生效、标签建议里出现
已有标签、以及几处会静默改变行为的回归。

> **升级注意**：全局「标签置信度下限」（`confidence_min`）已改为**按分类配置**。
> 旧的全局值会被抬成「未设置分类」的默认门槛并给出一次提示，不会静默丢失。
> 如需更细的控制，到「AI 助手设置 → 标签池」里按分类调整。

### 分发相关

- `manifest.license` 由 `MIT` 改为 **`Apache-2.0`**，与随附的 `LICENSE` 文件对齐
  （原文件与 manifest 声明不一致）。
- 新增 `FORK.md`：说明本副本是**非官方修改版**、基于上游 v0.3.0、列出改动清单与
  安装/许可注意事项（Apache-2.0 第 4(b) 条要求声明修改）。
- 分发时不包含 `data/`（插件私有数据与密钥不随包走），目录名必须等于 `manifest.id`。

### 新增

- **标签池 token 预算可调**（`tag_pool_budget_tokens`，默认 1200）：
  「AI 助手设置 → 标签池」新增输入框（200–20000，步进 100），决定给 AI 列多少标签。
  调大能让模型看到更多已有标签、减少「把已有标签当新标签」；代价是 prompt 更长更贵。
  实际值会被 `modelcap.context_available()`（上下文 − 输出 − 余量）夹住；
  档案自己的 `tag_pool.budget_tokens` 仍可单独覆盖。
  - 新键是 `config_schema` 已声明的整数键（宿主 `config.set` 只接受声明键）。
  - 回归用例：路由 3 条（保存/非法值/上下文夹取）、设置页契约 1 条。

- **每分类置信度 + 每分类数量（取代全局「标签置信度下限」）**：
  - 全局 `confidence_min` **删除**，不再作为所有标签的统一门槛；
  - 「AI 助手设置 → 标签池」里每个标签分类可展开设置：**标签置信度**（低于它的标签不打）
    与**设置数量**（该分类最多保留几个，按置信度从高到低取）；
  - 语义：未单独设置的分类默认置信度 **0.5**、数量**不限**；数量填 **0 = 该分类一个都不选**；
    「最大总数量」（`max_tags`）仍是硬上限；
  - **保存校验（只提醒、不拦保存）**：各分类「设置数量」之和超过「最大总数量」时，
    弹窗列出明细（总上限 / 当前合计 / 超出多少 / 每个分类占的额度）并问「仍要保存吗」，
    确认后照常保存；**实际打标按总上限按置信度截断**，此时各分类配额只作参考。
    总上限大于分类之和是允许的。
  - 规则会**同时**用于两处：拼进 system 提示词（模型知道该给几个、置信度要多高），
    以及解析后的确定性过滤（`tagpool.apply_rules()`：分类阈值 → 分类配额 → 总数截断）；
  - 存储：新增已声明的字符串键 `tag_category_rules`（JSON），`max_tags` 沿用；
  - 预览/CLI 与真实作业同口径（`prompt.append_system_note()` 注入同一段规则文本）。
  - 回归用例：`tagpool` 7 条、`parsing` 2 条、`jobs` 2 条、`routes` 4 条、设置页契约 1 条、
    前端冒烟（含「超限拦截 + 成功保存」完整流程）1 条。

- **冻结分类的标签不再进入「建议删除」**：`categories_exclude` 与主程序的「隔离分类」
  现在被当作**冻结**内容 —— 分析阶段（`tag_removals`）与落地阶段（`/jobs/<id>/apply`）
  都会跳过这些分类里的标签：审批页不会出现「要不要删掉你自己写的那条」，
  自动模式也不可能删掉它们。落地阶段的兜底同时保护**修复前就已存在的老作业**
  （它们的 `tag_removals` 是旧的、未过滤的）。
  - `tagpool.protected_tags()` 只认「明确排除 / 隔离」；「只取」白名单之外的标签
    不算冻结（那只是这次不参与挑词）。`respect_isolated=false` 时不冻结隔离分类。
  - 回归用例：分析阶段 1 条、落地阶段 1 条、`protected_tags` 1 条。

- **标签合并也受冻结分类约束**：`/jobs/<id>/apply` 执行 `tag_merges` 时会
  `remove=[source]`，等于从每张卡上删标签，此前完全不看冻结集合 —— 一次合并就能把
  用户明确排除的分类整体搬空。现在跳过冻结标签并回传 `skipped_frozen`，
  审批页显示「未合并 X：属于排除/隔离分类」，整条都被挡住时 `error` 为 `all_frozen`。
  - 回归用例：排除分类 1 条、隔离分类 1 条、无冻结时行为不变 1 条。

- **标签池分类过滤（设置页「标签池」，带 `?` 说明）**：两个输入框
  `categories_include`（只取这些分类）/ `categories_exclude`（排除这些分类），
  标题旁的 `?` 点开就是逐条填写说明（留空等于什么、多个怎么分隔、名字从哪来、
  会不会误伤、和隔离分类的关系）。下方列出「现有标签分类」并给出「只取 / 排除」按钮，
  点一下自动填进对应输入框（两边互斥），**不用手打分类名**。
  保存后显示生效效果：可用标签数 / 被排除数与示例，分类名拼错会明确提示。
  - 新端点：`GET /tag-pool`（当前值 + 分类清单 + 未分类数）、
    `POST /tag-pool`（保存 `tag_pool.categories_*`，接受数组或中英文逗号/顿号/换行分隔的字符串）。
  - 设置页新增区块「标签池」；`frontend/api.js` 增加 `tagPool()` / `saveTagPool()`。
  - 回归用例：路由 4 条、`tagpool` 1 条、设置页静态契约 2 条、前端冒烟 1 条。

### 修复

- **「建议新增标签」里出现你已经有的标签**：模型只看得到**预算内**被列出的标签
  （`listed ⊆ allowed`，标签池按 token 截断），预算外的真实标签它没见过，就会当新标签
  提出来；它也可能把「库里已有、但没写进 tags」的名字直接塞进 `suggested_new_tags`。
  之前只在**本次响应的 tags** 里做去重，没有拿标签库核对。
  现在两处都补上：
  - 作业侧：`filter_new_suggestions()` 先做别名还原，再用真实标签库（+ 排除/隔离分类）
    核对，已有的直接丢掉，并在作业警告里写「已经存在/属于排除分类」以及「原名→规范名」；
  - 队列侧：`GET /suggestions` 按**当前**标签库过滤历史脏数据（只隐藏、不删数据），
    返回 `hidden_existing`，设置页显示「另有 N 条建议其实已经在标签库里，已隐藏」。
  - 回归用例：`jobs` 2 条（别名/排除、端到端）、`routes` 2 条（隐藏与清空）。

- **标签池保存后不生效（真机实测）**：根因是宿主的 `api.config.set()` **只接受
  `config_schema` 里声明过的键** —— 写内部结构 `tag_pool` 会被拒绝（`set()` 返回
  False，前端报「配置未写入」）。现在把两个分类名单存成**已声明**的字符串键
  `tag_pool_categories_include` / `tag_pool_categories_exclude`（逗号分隔），
  `normalize_config()` 再折进 `tag_pool`。宿主「插件配置」页也会直接显示这两个输入框。
- **「未整理」标签数量与宿主界面对不上**：没有显式分类映射的标签现在归入
  `taxonomy.default_category`（宿主标签页的「未整理」）。此前它们被算成「未分类」，
  于是「未整理」只统计显式映射的那部分（实测宿主 287、插件 12），
  且「排除 未整理」也挡不住它们。
- **`categories_include` 现在也会挡下「没有分类」的标签**：此前白名单只作用于
  有分类的标签，未分类标签会漏进候选集 —— 用户设了「只取 题材」仍看到别的标签，
  表现就是「设了跟没设一样」。
- **配置页的「最大标签数 / 概览字数上限 / 标签置信度下限」只能调小，调大不生效**
  （用户原话：「配置中，保存并应用后并未生效」）。根因是三处叠在一起：
  1. 内置档案的 `output_schema` 写死了 `tags.max_items=8` / `summary.max_chars=300` /
     `tags.min_confidence=0.5`；
  2. `prompt.build_limits()` 按「配置是全局上限，与档案规则取更严格者」取
     `min(全局, 档案)`，于是配置 20 / 600 永远被压回 8 / 300；
  3. 生成约束（`schema.describe_constraints`）、事后校验与超量截断（`parsing`）
     读的都是**档案**里的那份数字，与 `{{ limits.* }}` 渲染出来的值分叉。

  改法：
  - `output_schema` 里写 **0 = 跟随全局配置**（与 `generation.max_tokens = 0` 同一约定），
    内置档案改为 0，`build_limits()` 把 0 视为「未声明」；用户自建档案写具体数字时
    仍按「更严格者」生效；
  - 新增 `prompt.apply_limits(profile, limits)`，把生效值回写进
    `output_schema.properties.{tags.max_items, tags.min_confidence, summary.max_chars}`，
    并在 `ai.analyze` / `ai.classify` / 预览 / 估算 / CLI 的调用点统一应用
    —— 提示词约束、校验、截断从此同源。
  - 回归用例：`test_zero_in_schema_means_follow_global_config`、
    `test_custom_profile_can_still_be_stricter_than_global`、
    `test_apply_limits_rewrites_schema_used_by_constraints_and_parsing`、
    `test_saved_limits_reach_prompt_and_truncation`。

- **配置里的非有限值不再让插件整体不可用**：`int(float('inf'))` 抛的是
  `OverflowError`（不是 `TypeError`/`ValueError`），会穿透 `load_config()`
  让所有接口 500。`1e400` 与 `"Infinity"` 都是写入配置的合法 JSON。
  加固 `tagpool._to_quota` / `_to_confidence` 与 `config._to_int` / `_to_float`。

- **档案自己的 `output_schema.tags.min_confidence` 重新生效**：分析作业恒传
  `tag_rules`（哪怕空对象），解析必走按分类的新分支并清零全局门槛，导致用户自建
  档案里的门槛被完全绕过（与 README 的承诺矛盾）。现在与分类门槛取**更严格者**。

- **缺 `confidence` 的标签不再被静默丢弃**：模型偶尔漏写该字段，或直接返回字符串
  数组。原实现按「缺少置信度」丢弃，等于一个格式偏差就清空全部标签；
  现在保留并写告警，分类配额与总数上限仍然照常执行。

- **标签池来源 `categories` 不再退化成 `library`**：没有显式分类映射的标签会被
  回填进 `default_category`，使「没有分类」这个判断永远不成立，未归类标签全部漏进
  「只取已归类」模式。现在单独记录哪些是回填的，来源过滤与「未整理」计数共用同一份。

- **分类改名/删除后遗留的规则可被清理**：渲染遍历分类清单、保存遍历规则表，
  孤儿规则看不见也删不掉，却仍会被写回并计入配额求和，导致「已分配 / 超出」
  永久失准。现在渲染前摘出孤儿规则并提示，同时守住「分类清单尚未加载时不动」。

- **标签合并也受冻结分类约束**：`tag_merges` 会逐卡 `remove` 标签，此前完全不看
  冻结集合，一次合并就能把用户明确排除的分类整体搬空。现在跳过并在审批页显示
  「未合并 X：属于排除/隔离分类」。

### 已知未处理

- `batch_size`（每批张数）、`concurrency`（并发数）仍无消费方（改了不起作用）；
- `default_profile_id`（默认提示词档案）后端仍未读取，前端读的 `config.task_profiles`
  这个键也不存在。

## 0.3.0（2026-10-25）

首个公开发布：插件契约 v3 + 模型能力设置。以下 2026-10-19 ~ 2026-10-24 各批次均随本版本发布。

### 2026-10-24 模型能力设置 + H 轮真机验证修复

#### 新增

- **模型能力设置**（设置面板「模型能力」+ `GET/POST /settings`）：
  上下文长度（默认 **256K**）、输出长度上限（默认 **32K**）、
  推理开关、思考等级（`auto/low/medium/high`）；四项都写进 `config_schema`，
  默认值见 `backend/core/modelcap.py`。
- 档案的 `max_tokens` 支持写 **0 = 跟随全局输出长度上限**；内置档案全部改为 0
  （`builtin-agent` 从 2048 → 32K，修掉 finalize JSON 被截断成空响应的问题）。
- 推理设置按协议注入：OpenAI Chat/Responses 发 `reasoning_effort`（关推理 → `none`）；
  Anthropic 不注入（它的思考是 `thinking`，语义不同）。
- 提示词/世界书预算按「上下文 − 输出 − 余量」夹紧（`modelcap.resolve_prompt_budget`）。

#### 修复

- **归类弃权被判失败（C4）**：`CLASSIFY_OUTPUT_SCHEMA.required` 去掉 `category`
  —— 模型留空 `category` 是合法弃权，原来会被 schema 判成「缺少必需字段」，
  作业直接 `failed`，`undecided` 分支成了死代码。弃权时 `confidence` 一并置空。
- **弃权条目被当成根目录移动**：`_apply_classify` 跳过 `category==""` /
  `skipped=undecided` 的条目，即使被「接受」也不会移出分类。
- **Agent 轮次自适应不生效（P5）**：提交时保留**原始** budget 给作业层，
  缺省 `max_iterations` 才按 `max(4, min(12, ceil(卡数/6)))` 计算；
  `context_budget` 缺省按上下文长度推导。UI 改发 `max_iterations`（原来发
  `max_rounds`，后端不读，等于死控件），留空 = 自适应。
- **多书世界书作业静默丢条目**：宿主按 item key 去重，多书同 uid 会丢第二条；
  多书作业的 key 改为 `uid@文件名#短hash`（单书仍是裸 uid，兼容旧链路）。

#### 测试

- 新增 `tests/test_modelcap.py`；补 C4/弃权落地/多书 key/设置路由/Agent 原始 budget
  的回归用例。`pytest` **885 passed**，前端冒烟 **28/28**。

### 2026-10-22 迁移到插件契约 v3

#### 变更

- `api_version` / `manifest_version` 升到 3；`permissions` → `capabilities`
  （同名 9 项，未缩水）。
- `frontend.entries[]`（12 条）→ `frontend.contributions`：7 个根菜单节点
  （展开 14 个，含 `ai-helper-context` / `ai-helper-batch` 两个显式 submenu）
  + 4 条命令（`open-workbench` 带建议快捷键 `Ctrl+Shift+A`，其余走命令面板）；
  `frontend.render_mode: "declarative"`（界面全部是宿主 surface，不自绘）。
- 运行期状态回报改走 `api.contributions.update(id, {status|badge|progress|busy})`
  并订阅 `contributions:changed` 重放；v2 的入口运行期 API 与 `entries:changed`
  已随宿主删除。进度由 `{done, total}` 归一化为 0–1 比值（宿主 dock 渲染进度环）。
- 新增 `frontend/commands.js`（4 个命令处理器）。
- 测试迁移：`test_manifest_contract.py` 改为用主程序真实
  `contract.validate_manifest(..., strict=True)` 交叉验证（不再复刻白名单）；
  `test_entry_matrix.py` 重写为 v3 贡献 × 落点矩阵；`test_css_scope.py` 的入口
  图标检查改读 `contributions`；`tests_frontend/smoke.mjs` 的假 API 改用
  `api.contributions.update`。
- 新增 `scripts/stm_plugin_check.py`（从主程序生成并复制的独立严格校验器）与
  `.github/workflows/plugin-check.yml`（CI 按 `manifest.id` 摊目录后跑严格校验）。
- 版本 `0.3.0`。

### 2026-10-21 入口收进二级菜单

宿主新增 `entries[].group` 后，右键菜单 / 批量菜单里的 7 条 context/batch 入口
全部声明 `"group": "AI 助手"`，不再各占一行。

#### 变更

- **入口声明 `group`**：7 条 `kind: context` / `kind: batch` 入口统一加
  `"group": "AI 助手"`。宿主据此把它们折叠成一个父项，点开是二级浮层；
  菜单里不再出现「AI 打标此分类全部卡片」「AI 归类…」「AI 精简…」三条并列。
- `tests/test_manifest_contract.py` 复刻的 `KNOWN_ENTRY_KEYS` 同步加入 `group`
  （宿主 `core/plugins/entries.py` 是严格白名单，漏了会拒绝加载整个插件）。

### 2026-10-20 对齐宿主新契约

宿主 6 条补丁全部合入后，插件侧同步验收并修掉实测暴露的问题。

#### 变更

- **`ctx.target` 成为唯一形状**：删掉 `scope.js` 里对 `targetKind` / `targetPath` /
  `targetIds` 的兼容垫片。宿主已在 `buildOpenContext()` 主动丢弃这些字段，留着
  读取路径只会让契约重新分叉。
- **入口进度**：`plugin.js` 新增 `context.setTaskProgress(taskId, {done,total})`，
  把「正在跑 3/12」同时写到该任务的**全部入口**（批量菜单 / 右键菜单都会画进度条）。
  审批面板改为只认 `ctx.runId`（`ctx.targetId` 已被宿主删除）。
- 新增 `open-profiles` 入口（命令面板），此前 `profiles` surface 没有任何入口，
  用户永远打不开。
- 世界书精简支持**多本世界书**：`/wi/entries` 接受来源数组并按书分组返回，
  作业项自带 `worldbook_source`，落地按条目来源分组（旧版只处理第一本，且会串书）。
- 卡片分类作用域改走插件 HTTP `/cards`（`category` / `limit=0` / `ids`），
  不再调用并不存在的 `api.cards.*` 前端命名空间。

#### 修复

- **`/cards` 与前端作用域解析完全不通**：`scope.js` 读 `api.cards.list`，而前端
  API 门面没有 `cards` 命名空间 → 「分析此分类」必然报
  `Cannot read properties of undefined`。实测右键分类才暴露。
- **提示词档案下拉全是「undefined · 通用」**：`/profiles` 返回的是
  `{index:{id,name}, system_preview}`，代码按扁平字段读，选项 value 全为空。
- **`jobs.py` 缺 `CODE_INVALID_PAYLOAD` 导入**：世界书校验失败时抛 `NameError`
  而不是可读错误。
- **世界书变量校验过严**：通用档案（builtin-default）里的 `card.name` /
  `limits.*` 被算成「缺失」，导致「按任务自动匹配」在精简任务上直接失败。
  现在只校验本任务负责注入的 `wi.*`。
- **`[hidden]` 被组件布局覆盖**：进度条在提交前就占一块空白（`display:flex`
  覆盖了 `display:none`）。加作用域内的 `[hidden] { display:none !important }`。
- 作用域横幅的占位字符 `◎` 换成 CSS 圆点（字体缺字时显示成方框）。
- 任务面板补一句预览占位说明，避免固定高度面板下半屏全空。

### 2026-10-19 任务化重构

按任务化重构方案落地：把「4 个能力塞在 1 个
7 段长表单里」改成「一个能力 = 一个任务 = 一个面板」。入口从 4 条扩到 12 条，
全部覆盖用户会说的词（打标 / 精简 / 归类 / 提议）。

#### 新增

- **任务注册表**（`frontend/tasks.js`）：四个任务各自声明 `scopes` / `surface` /
  `jobTypes` / `writes`，面板与入口从它派生。主按钮文案、面板标题、任务卡都读同一份。
- **任务中心 hub**（`frontend/views/hub.js`）：坞入口首屏。四个任务卡 + 待审批卡 +
  最近作业（可点回审批面板）。
- **通用任务面板**（`frontend/views/task.js`）：作用域横幅 → 参数（≤4 项）→ 预览 →
  主按钮 → **同面板内联进度** → 结果入口。四个任务共用，行为一致。
- **作用域解析层**（`frontend/scope.js`）：`ctx.target` → 具体条目。分类不存在、
  选中集无法识别等情况**抛错**，面板显示红条 + 重试，不再是「0 张」。
- **作业台账**（`backend/store.py` `record_run`/`update_run`/`list_runs`）与
  `GET /jobs`：关掉面板后还能找回上次作业（F10）。
- **审批面板按 `job_type` 渲染**（`frontend/views/review.js` 重写）：
  世界书并排 diff、归类「现分类 → 目标分类 + 置信度」、提议可勾选方案树，
  打标才有「写什么」开关。
- **落地前确认**：列出「将写 N 条概览 / M 个标签 / 移动 K 张 / 创建 X 目录」，
  取消即不写（此前 `api.ui.confirm` 的 Promise 没 await，取消也会写）。
- 前端冒烟测试（`tests_frontend/smoke.mjs` + `tests/test_frontend_smoke.py`）：
  最小 DOM 桩 + 假插件 API，真实执行 `activate()` 与各面板首屏。

#### 变更

- `manifest.json`：12 条入口；坞入口指向 `hub`；`label` 全部用用户词；
  `description` 覆盖四个能力。
- **面板不再有页签**：`hub` / `analyze` / `polish` / `classify` / `propose` /
  `review` / `profiles` / `settings` 各是一个 surface，入口直接定位。
- 状态胶囊从「面板自绘」改为 `frontend/headerStatus.js`，只挂在任务中心标题行
  （其余面板不再重复拉 `/status`）。
- 打标去掉「分析目标（概览/标签/两者）」提交参数：提交时永远生成概览 + 标签候选，
  写什么在**落地时**用 `write: {note, tags}` 决定 —— 消除 review 模式下无效的假控件。
- 世界书改写结果回传 `original`，审批页因此能做并排 diff。
- 提示词档案页的「预览」不再依赖全局选中集：优先宿主当前选中项，没有就取最新一张。

#### 修复

- **世界书精简只处理第一本书**：作用域是「一个分类下的 N 本世界书」时，旧实现取
  `books[0]`，只改第一本，且回写会把候选写进第一个来源（串书）。现在：
  `/wi/entries` 接受来源数组并按书分组返回（读不到的书单独标记，不让整个分类失败）；
  每个作业项自带 `worldbook_source`；落地按条目自带来源分组。

- **`_apply_propose` 缺审批门禁**：提议跑完就能直接创建目录/标签，与其余三类作业
  （都要求 `status == done`）不一致。现在未审批返回 409 并说明先去审批。
- **世界书改写不检查提示词变量缺失**：档案引用了 `wi_rewrite` 任务不提供的变量时，
  旧版带着空值调用模型（浪费一次调用且产出无法校验）。现在提前失败并列出缺什么。
- **作用域跨入口残留**（F16）：删掉全局 `context.selectedCardIds` / `openTarget`，
  作用域只活在当前任务面板里；坞打开时必定是空作用域。
- **角标只写第一个入口**：`entryIds.some(...)` 会在第一个成功处短路，
  `open-review` 永远拿不到待审批数。改为逐个写入。
- **进度永远 0%**：SSE `snapshot` 的计数在顶层（`total`/`completed`），旧代码读
  `job.counts`。新增 `normalizeProgress()` 兜住两种形状。

#### 删除

- `frontend/views/workbench.js`（7 段长表单，拆进 4 个任务面板）
- `frontend/selection.js`（全局选中集耦合，改用 `api.host.selection()` + `scope.js`）
- `panel.js` 的 `TABS` / `createPanelBody` / `createPanelBody` 页签体系

#### 宿主侧（需主程序配合，见补丁清单）

插件已按目标契约实现并保留降级路径，以下补丁可独立应用：

1. `ctx.target` 语义化（宿主不再在右键瞬间拉 10 万张卡片）
2. `api.host.selection()`
3. `api.worldbook.sources()`
4. 批量菜单移动端渲染 + 角标/进度渲染
5. 命令面板过滤 `context` 类入口、`batch` 传选中集、`kindLabel` 补 `batch`
6. `api.jobs.review()` / `reviewBatch()`

---

### 2026-10-19 用户验收反馈

用户报了 3 个问题：两个 UI 缺陷（下拉框外观不统一、选中态被容器边界裁掉）和一个功能
问题（拉取模型列表必须先保存 provider）。三个都在插件侧修复，**不改宿主代码**。

#### 修复

- **模型候选下拉已与其它下拉框统一**：此前它写成
  `<select class="plugin-stm-ai-assistant-select">` —— 这个类名其实是自定义下拉
  `div` wrapper 的类名，原生 `select` 套上它只拿到「透明底 + 无边框」，拿不到 trigger
  那套外观，和同一张表单里的「接口协议」并排时明显不是一套控件。现在改用
  `createUi().select()`（`dom.js:styledSelect`），拉取结果用 `setOptions()` 填充，
  字段加 `--wide` 跨满栅格行（模型名长 + 右侧并排放「拉取模型列表」按钮）。

- **下拉菜单不再被面板滚动区裁掉**：菜单是 `position: absolute`，会被最近的可滚动
  祖先在裁切边切掉——设置页下半部分的下拉一打开只剩两三行可见（实测菜单 16rem、
  滚动区只剩 165px）。`styledSelect.open()` 现在按滚动区可用空间决定朝下/朝上
  （`.is-dropup`）并把高度钳到可用空间内部滚动，朝上时 7 个候选一次看全；
  仍用 `absolute`（层级/定位归宿主，不做 `position: fixed`）。

- **选中态不再被弹层内容区边界裁掉**：插件自建的内部滚动区
  `.plugin-stm-ai-assistant-body` 原先 inline padding 为 0，而它的
  `overflow-x: hidden` 会在 padding box 处裁切。宿主给输入框的焦点态是
  `box-shadow: 0 0 0 3px`（外扩 3px）与 `outline: 2px + offset 2px`（外扩 4px），
  且**不产生可滚动溢出**——因此贴边的输入框（设置页 id / 读取超时 / API Key /
  默认模型、提示词档案优先级、工作台采样张数等）左右各被吃掉 3~4px。
  现在主体行内 padding = 0.5rem（8px ≥ 4px 最大外扩），页签条同步 0.5rem 保持左右对齐；
  纵向也补了底部 padding（滚到底时最后一个控件的焦点环同样会被裁）。
  窄屏 `padding: 0.85rem`（13.6px）绝对值更大，修复不会被覆盖失效。

- **拉取模型列表不再要求先保存 provider**：`POST /providers/<id>/models` 只认已落盘
  的密钥，且 provider 不存在直接 404 —— 新 provider 必须先保存、再重新打开才能拉。
  现在新增草稿型 `POST /models`：请求体带表单当前的 `base_url` / `api_key` /
  `extra_headers` 即可，provider 未保存也能拉。Key 优先级「表单刚输入的 → 已保存的」，
  明文 Key 只在这一个请求里用（不落盘、不回显）。旧路径保留并共享同一实现，
  provider 不存在时不再 404 而是走草稿逻辑。

#### 测试

- 新增 `tests/test_settings_view_contract.py`（5 用例）：锁定「模型候选必须是
  styledSelect」「字段跨行」「拉取请求带 provider_id / base_url / api_key」
  「客户端指向草稿型 `/models`」「下拉菜单按滚动区可用空间翻向/钳高」——
  这些问题只在浏览器里显形，用静态契约钉住。
- `tests/test_routes.py`（+9 用例）：草稿型拉取、表单 Key 覆盖已保存 Key、未带 Key 时
  回退已保存、`extra_headers` 覆盖/回退、缺 Key / 缺 base_url / 非法 base_url 一律
  200 + `ok=false`（不阻塞配置）、响应不含明文 Key。
- 全量：`python -m pytest -q` → **713 passed**。
- 真实宿主验证（127.0.0.1:5000，重启后台以加载新路由）：`POST /models` 用已保存
  密钥拉到 6 个 deepseek 模型；未保存 provider + 表单 Key 也发出了请求（401 来自
  上游，说明请求确实到达）；缺 Key / 缺 base_url 都是 200 + `ok=false`。
  浏览器侧：设置页模型候选已是 styledSelect 并列出 6 个模型、朝上完整展开；
  四个页签的输入框左右各留 8px（无横向滚动），提示词档案「优先级」焦点环完整可见。

## 0.2.0（内部版本，未公开发布）

**破坏性变更：`api_version` 1 → 2。** 需要主程序 `PLUGIN_API_VERSION >= 2`
（`core/version.py`）。旧主程序会以 `PluginApiVersionError` 拒绝加载本插件。

本次是「适配新插件系统地基」的重写：插件从「自绘 `position: fixed` 浮层 + 自建模态框
骨架」改为「**插件声明入口与 surface，宿主提供容器**」。后端业务逻辑（LLM 调用、
提示词工程、作业、幂等、成本记账）**一行未改**。

### 变更

- **`manifest.json`**：`api_version: 2`、新增 `manifest_version: 2`、
  `version: 0.1.0 → 0.2.0`；新增 `frontend.entries` 声明 4 个入口：

  | id | kind | 用途 | 条件 |
  |---|---|---|---|
  | `workbench` | `dock` | 主面板（四页签） | — |
  | `analyze-folder` | `context` | 右键**分类** → 分析该分类全部卡片 | `when: {mode:["cards"], target:"folder"}` |
  | `analyze-selected` | `batch` | 批量操作菜单 → 分析选中卡片 | `requires_selection`，`when: {mode:["cards"], selection:"any"}` |
  | `assistant-settings` | `settings` | 设置入口 | — |

  本插件已登记为 **`official`**（主程序 `static/plugins/catalog.json`，`tier` 只能由
  宿主仓库清单决定），因此 dock 入口默认进坞槽位。尽管 official 允许全部 7 类 kind，
  本插件**刻意只用 community 也允许的 5 类**（`dock` / `context` / `batch` /
  `settings`），不声明 `nav` / `status`——降级后入口也不会被静默忽略。

- **入口落点收敛（P2）**：旧版 4 个入口**全部指向同一个 surface**（`workbench`），
  且「分析选中卡片」被放在 `context`（右键菜单）里——右键一个分类时作用域应该是
  那个分类，插件却只能读到全局选中集，语义错位。本次收敛为：
  - **删除 `open-review`**（与 `workbench` 落点重复，唯一区别只是初始页签）；
    待审批角标从 `open-review` 改挂到 `workbench`（否则 `setBadge` 查不到条目会
    静默返回 `false`，用户再也看不到待审批数）；
  - **新增 `analyze-folder`**（`context` + `when.target: "folder"`）：右键**分类** →
    分析该分类（含子分类）的全部卡片；
  - **`analyze-selected` 由 `context` 改为 `batch`**：注册到**批量操作菜单**，
    作用域 = 勾选的卡片；
  - 4 个入口的 `kind` **两两不同**，各自落在真实的宿主位置。

- **`workbench` 由 `panel` 改为 `modal`**：主工作台承载批量分析表单与审批列表，
  需要宿主提供遮罩 + 焦点陷阱；旧 `panel`（右侧抽屉 `min(420px, 92vw)`）会把长表单
  挤在窄栏里。manifest 与 `frontend/plugin.js` 的注册同步修改。

- **入口作用域由宿主提供**：新增消费 `ctx.targetKind` / `ctx.targetPath` /
  `ctx.targetIds`（`panel.js:applyEntryContext`）。分类 → 卡片 id 列表由**宿主**解析
  （插件拿不到卡片列表接口，也不该读宿主的 `viewState.selectedIds`）。
  带作用域时不再回读全局选中集（`workbench.js:refresh`），并在面板顶部显示本次作用域。

- **`onOpen(ctx)` 处理作用域切换**：surface 已打开时宿主不重新 `mount`（会丢用户
  已填的表单），只把新 `ctx` 交给 `onOpen`。没有它，「先右键 A 分类、再右键 B 分类」
  会一直显示 A 的作用域。

- **入口不再自绘**：删除可拖拽浮动按钮、`POSITION_KEY` 位置记忆、`clampPos` /
  `restorePos` / `savePos` / `DRAG_THRESHOLD`。坞、命令面板、右键菜单、批量操作菜单
  与设置页由宿主渲染。

- **面板容器归宿主**：`createPanel` → `createPanelBody`。删除自绘 overlay / shell /
  顶栏 / 关闭按钮、`document.addEventListener('keydown')`（ESC）、遮罩点击关闭。
  宿主的 `pluginSurfaceHost.js` 提供定位、尺寸、滚动、`role="dialog"`、`aria-modal`、
  焦点陷阱、ESC 关闭与遮罩合成。新增 `prompt-preview`（`type: 'modal'`）surface 承载
  Prompt 全文预览。

- **UI 原语委托宿主**：`icon` / `button` / `badge` / `section` 改为 `api.ui.*` 的薄封装
  （`frontend/dom.js` 的 `createUi(api)`）。`icon()` 不再硬编码
  `/static/icons/ui.svg`，名字 → sprite 的映射归宿主。
  `styledSelect` / `numberInput` / `checkbox` / `textarea` 保留自绘（宿主原语无等价物）。

- **API 形态迁移**：`api.toast` → `api.host.toast`（62 处）、`api.refresh` →
  `api.host.refresh`；`window.confirm` / `window.prompt` → `api.ui.confirm` /
  `api.ui.prompt`。

- **状态回报宿主入口**：启动自检结论 → `api.entries.setStatus('workbench', ok|warn|error)`；
  作业进度 → `setProgress` / `setBusy`；待审批数 → `setBadge('workbench', n)`。
  新增 `api.onConfigChange()` 订阅（替代轮询），设置页改完立即反映。

- **选中集合读取集中化**：新增 `frontend/selection.js`，把
  `api.store.viewState.selectedIds` 这个 Provisional 耦合收进单一模块
  （设计文档 §8.3 已登记；宿主提供稳定 API 后只改这一个文件）。
  P2 之后它**只剩坞 / 快捷键路径**使用——右键菜单与批量菜单的作用域已由
  `ctx.targetIds`（Stable）提供。

- **声明快捷键**：只保留 `workbench` 的 `Ctrl+Shift+A`（`open-review` 已删除，
  其 `Ctrl+Shift+R` 一并移除）。宿主侧 `shortcut` 的语义是「建议快捷键，宿主可覆盖」
  ——用户没改时直接生效，改绑后以用户配置为准。入口总览页按 **surface 去重**，
  `context` 类不提供快捷键设置。

- **`activate()` 返回值**：清理逻辑收进函数并 `return`（D9.1）；`deactivate` 保留为兜底。

### 删除

- `frontend/plugin.css`：`.plugin-stm-ai-assistant-launcher*`（含 `z-index: 2140`）、
  `.plugin-stm-ai-assistant-overlay`（`position: fixed` / `inset` / `z-index: 2150` /
  `backdrop-filter` / `background: var(--surface-scrim…)`）、
  `body:has(.plugin-stm-ai-assistant-overlay.is-open) .main-container { backdrop-filter: none }`
  兜底规则，以及自绘的 `.btn` / `.badge` / `.section*` 规则（已委托宿主原语）。
- `frontend/plugin.js` 的 `api.setFullscreenScrim()` 调用——宿主现在自己管遮罩合成。
- 自绘的面板顶栏（标题 + 副标 + glyph + 关闭钮）——标题与关闭钮是宿主 DOM
  （`.plugin-surface__title` / `.plugin-surface__close`）。

### 保留

- **`frontend/assets/icon.svg`（插件品牌图标）**：**保留**，并在 P3 之后成为
  **全部图标位的唯一来源**（见下）。

  P2 及更早的选择是「入口用宿主图标名 + 品牌图标挂在插件自己的状态条」，
  同一个插件在不同落点显示四个不同图标（`wand` / `folder` / `tags` /
  `settings-gear`），看起来像四个插件。

  P3 起：`entries[].icon` 与 `surfaces.register().icon` 全部写
  `frontend/assets/icon.svg`，由宿主解析成
  `/api/plugins/stm-ai-assistant/assets/frontend/assets/icon.svg` 并用
  `<span>` + CSS `mask` 渲染（跟随主题前景色）。宿主侧 `resolveIcon()` 的三态
  返回（host / asset / missing）让这条路径成为 `Stable` 契约。

  插件 CSS 不再需要自绘 glyph —— 图标归宿主的 surface 标题行。

## P3 可用性修复（2026-10-11，用户验收反馈）

用户在实际使用中报了 5 个问题，其中前两个是功能性 bug。

### 修复

- **找不到「固定入口」（坞不可见）**：坞的自动收起规则用了
  `body:has(.modal-overlay)`。`:has()` 只看元素**是否存在于 DOM**，不看可见性，
  而主程序 28 个模态框全部用 `x-show`（常驻 DOM，仅 `display: none`）——
  该选择器**几乎永久成立**，坞被永久压成 `width: 0`。
  现在只对**真正与坞重叠**的 surface 类型收起
  （`body:has(.plugin-surface--panel)` / `--sheet`）：这两个类型在关闭时会把
  shell 从 DOM 摘掉，所以「元素存在」恰好等价于「surface 打开」；
  `modal` 居中且层级远高于坞，天然盖住它，不需要收起。

- **勾选卡片后批量项仍显示「请先选中至少一张卡片」**：注册表只**监听**
  `selection-changed`，但全仓没有任何地方派发它（`cardGrid` 直接写
  `viewState.selectedIds`，6 个写入点）。现在宿主用 `Alpine.effect` 直接观察
  `viewState.selectedIds`，值变化才广播 `entries:changed`。
  （插件侧无需改动——`when` 一直由宿主求值。）

- **弹层顶部有两行**：图标与状态胶囊现在都在**宿主标题行**（图标左上、
  胶囊置顶、关闭用宿主通用关闭图标），插件内容区只剩「页签 + 主体」。
  插件的 `headerExtra(container)` 把胶囊注入宿主的附加槽。

- **切换页签时弹层大小跳动**：`workbench` 改用宿主的新尺寸档位
  `size: 'lg'`（固定 1080×760，即旧版自绘 shell 的尺寸）。
  宿主 modal 现在给的是确定的 `height`，不是 `max-height` + 内容自适应。

- **同一个插件应使用同一个图标**：见上方「保留」段落。

- **批量入口只应在角色卡视图出现**：`refreshPluginBatchEntries` 只列
  `enabled` 的入口。`when.mode: ["cards"]` 已在 manifest 里声明，因此世界书 /
  会话 / 预设视图下该菜单项自动消失，而不是留一条永远点不动的灰项。
  （右键菜单相反：不适用时**可见但置灰**并给出原因。）

### 变更

- `frontend/panel.js`：删除自绘状态行与品牌 glyph；新增
  `mountHeaderExtra(container)` 把胶囊槽注入宿主标题行。
- `frontend/plugin.js`：`workbench` 的 surface 注册补 `description` /
  `icon` / `size` / `headerExtra`。
- `manifest.json`：四个入口的 `icon` 统一为 `frontend/assets/icon.svg`。
- `frontend/plugin.css`：删除 `.plugin-stm-ai-assistant-status-row` 与
  `.plugin-stm-ai-assistant-glyph*`；胶囊槽改为不换行的单行布局。
- `tests/test_css_scope.py`：三条旧契约用例改为锁定新设计
  （图标统一、胶囊在宿主标题行、面板根节点只含页签 + 主体）。

> **关于旧版的 backdrop-filter 兜底规则**：那条规则是对旧宿主
> `.main-container { backdrop-filter: blur(var(--bg-blur)) }` 退化成恒等滤镜的绕行
> （详见 0.1.0 段落里那份实测记录）。主程序已从根源修复（`.main-container` 的
> `backdrop-filter` 整条删除，壁纸模糊改挂 `body::before { filter: blur(...) }`），
> 且宿主 surface 容器**刻意不提供** `backdrop-filter`（Chromium backdrop root 会画出
> 黑块/平坦灰）。因此插件侧的两条规则都已删除，`tests/test_css_scope.py` 锁定
> 「`plugin.css` 里 `backdrop-filter` 出现次数为 0」。

### 测试

- `tests/test_manifest_contract.py`（+27 用例）：`api_version == 2` 与
  `manifest_version == 2`；`entries` 结构校验（子键白名单 / id 正则 / kind 集合 /
  `surface.type` 集合 / 16 项上限 / 无旧三字段）；`when` 表达式校验；
  `when.config.requireKeys` 必须指向已声明的 schema 键；`requires_selection` 必须有
  `disabledHint`；**快捷键格式匹配宿主折算规则且不重复、不占用 `Ctrl+K`**；
  **`entries[].icon` 与 `api.ui.icon()` 引用的宿主图标名全部存在**
  （问主程序 `core.plugins.icons`）；official / community 两种 tier 下 4 项全保留
  （降级兜底）；本插件在主程序 catalog 里确实登记为 `official`；
  manifest 的 `surface.id` 与 `plugin.js` 的 `surfaces.register` 静态对齐；
  前端不得再出现 `api.setFullscreenScrim` / `registerModal` / `createRoot` /
  裸动词别名 / `localStorage` / `position: fixed` / 全局 keydown。
- `tests/test_css_scope.py`（+11 用例）：新增声明层自检（无 `position: fixed`、
  无硬编码宿主 sprite、`var(--x)` 无回退时必须在宿主令牌清单内、`!important` 只在
  作用域选择器上）；无 `backdrop-filter` / `body:has(` / `z-index: 2140|2150`；
  无十六进制颜色字面量；插件 CSS 不得试图选中宿主 `.plugin-surface*` DOM；
  品牌图标必须是插件自有资源 URL（不被 F15 误判）、且 glyph 在状态刷新后仍存活。
  并用主程序**真实的** `scan_css_declarations` 做交叉验证 + 变异测试。
- `tests/test_css_scope.py` 删除了 4 条已作废的用例（它们断言
  `body:has(...) .main-container { backdrop-filter: none }` 必须存在——那是旧宿主的
  绕行方案，现已随根因修复一起移除）。
- 全量：`python -m pytest tests/ -q` → **341 passed**。
- 主程序侧：`tests/test_plugin_manifest.py` + `test_plugin_installer.py` +
  `test_plugin_css_scope.py` → **122 passed**。

### 仓库清理

- 删除诊断脚本与产物（`tools/`，含 637MB 截图）、根目录调查产物
  （`_all_rules.json` / `_bf_all.json` / `_fs_out.txt` / `_gaps_out.txt` /
  `_probe_*.py` / `recon_backdrop_filter_report.md`），以及本地缓存目录
  （`.pytest_cache/` / `.superdesign/` / `design/` / `__pycache__/`）。
  这批 backdrop-filter 调查已随主程序根因修复而作废。

---

## 0.1.0（内部版本，未公开发布）

首个可用版本（P0 最小垂直切片）。

> ⚠️ 本段落里关于 `body:has(...) .main-container { backdrop-filter: none }` 兜底规则的
> 全部内容已在 **0.2.0 移除**（主程序从根源修复了退化的 backdrop-filter，
> 且宿主 surface 刻意不使用 `backdrop-filter`）。保留于此仅作历史记录。

### 修复

- **导航样式与角色卡详情页对齐**：页签改为详情页 workbench 的生效样式——透明底 +
  透明描边、圆角 `0.55rem`、`min-height 2.35rem`、`padding .45rem .7rem`、
  字号 `.78rem` / 字重 `800`；选中态是「填充 `--action-surface` + `inset 0 -2px 0
  var(--accent-action)` 底条」，**无边框、无位移**。旧版那套「向下突出的胶囊」
  （`border-radius: 999px` + `translateY(0.22rem)` + 负 `margin-bottom`）与详情页
  完全不符，已删除。页签上的数字改为详情页 `.detail-tab-meta` 的等宽小字，不再是胶囊角标。
  导航容器本身逐项对齐 `detail-card-workbench.css:1986-2004`（`gap .2rem`、
  `padding .42rem .85rem .35rem`、`min-height 3.2rem`、下边框 `--border-divider`、
  `--surface-container-raised` 底色、可横向滚动）。
- **「提示词档案」页签的全屏黑色阴影（真正根因）**：此前那一版把原因归给了遮罩自身的
  半透明黑，方向是**反的**。实测（`tools/bisect*.py`，headless Chromium 145，
  1532x899 视口 100% 复现）表明：
  - 黑区颜色就是 `--surface-scrim` 自身的 `rgb(15,23,42)`，把遮罩改成
    `rgba(255,0,255,.5)` 后黑区读出的是**纯 `rgb(255,0,255)`** —— 说明该区域的
    scrim 被按 `alpha=1` 绘制，**它下面的 backdrop 根本没有参与合成**（把页面背景
    改成纯红也照不出来）。
  - 真正的触发者是**主程序 `.main-container` 上那个退化的 `backdrop-filter`**：
    `layout.css:12-24` 写的是 `blur(var(--bg-blur))`，而 `variables.css:15-16`
    定义 `--bg-blur: 0px`，于是它是一个**恒等滤镜**。Chromium 仍然为它建立
    backdrop root 与独立合成层（CDP `LayerTree` 可见
    `reasons=['Has a backdrop filter.']`）；一旦上面再叠一个「整屏 fixed 层 +
    内部高滚动内容」（本插件的 overlay + 2093px 高的档案编辑器），合成器在面板以外
    的区域就解析不出 backdrop，整片塌成 scrim 原色。
  - 触发条件是**退化取值本身**，而不是「有没有 backdrop-filter」：`blur(0)`、
    `blur(0px)`、`brightness(1)`、`saturate(1)`、`contrast(1)`、`opacity(1)` 都触发；
    `blur(0.01px)` 及以上、`brightness(2)`、`none` 都不触发。
  - 因此**给遮罩加 `backdrop-filter` 完全无效**（去掉它反而更糟：整个 scrim 区域
    都变黑）。
  - 焦点相关性只是**重绘的诱因**：`focus()` 会触发 scroll-into-view 与重新合成，
    所以表现为「跟着焦点走」「偶尔闪一下」；实测黑区与滚动位置无关（scrollTop
    0→1450 全程恒定）。
  - 这**不是插件特有**的问题：主程序自带 `.modal-overlay` + 一个 4000px 高的滚动
    容器、零插件元素即可复现（`tools/bisect9.py` 的 `A2`）。

  修法是把 `.main-container` 的 backdrop-filter 在整个面板打开期间**彻底关掉**
  —— 与主程序自己两处先例（`detail-card-workbench.css:9-31`、
  `automation-workbench.css:12-32` 的 `body.<x>-open .main-container
  { backdrop-filter: none }`，注释记录的是同一个 Chromium 缺陷）完全一致。
  现于 `frontend/plugin.css` 增加（`:has()` 无需 JS 配合，且选择器含插件 token，
  通过安装时的作用域检查）：

  ```css
  body:has(.plugin-stm-ai-assistant-overlay.is-open) .main-container {
    -webkit-backdrop-filter: none !important;
    backdrop-filter: none !important;
  }
  ```

  **注意：不能只把退化的 `0px`「抬离恒等值」**——这是本次返工推翻的中间结论。
  `tools/check_backdrop_render3.py` / `check_backdrop_render4.py` 对
  `.main-container` 的候选取值做了逐候选的像素级对比（判据：面板外区域的
  `pct_navy`＝落在 `--surface-scrim` 原色 ±14 的占比，以及与该视口"遮罩隐藏"基准
  的逐像素最大差）：

  | `.main-container` 取值 | 1532x899（bug 现场） | 1920x1080 | 壁纸场景 |
  |---|---|---|---|
  | `none` | **ok**（sd 4.774 / navy 0.00%） | ok（navy 0.22%） | **ok** |
  | `blur(0.02px)`（曾交付） | sd 3.774、与基准差 39 | ok（该视口本不复现） | **navy 49.58% 回归黑块** |
  | `blur(1px)` / `blur(2px)` | 同样丢内容。与 `blur(0.02px)` 完全同值 | — | navy 48.59% |
  | `brightness(1.02)` / `brightness(1)` | **navy 63.19%、sd 45.172** | navy 33.05% | navy 63.19% |

  也就是说：**只要 `.main-container` 还带着任何非 `none` 的 backdrop-filter**
  （哪怕是 `blur(2px)` 这种"正常"取值），遮罩下面的 backdrop 内容就整层丢失，
  只是"黑块"变成了"均匀的平坦灰"（内容保留率 `retention = sd / raw_sd` 从正常
  的 0.21 掉到 0.0068）。手工规范合成（raw 帧 → `GaussianBlur(5)` → 58% scrim）
  与 `none` 的差是 11（属 5px 模糊的正常容差），与 `blur(0.02px)` 的差是 39/101。

  代价：面板打开期间用户设置的页面背景模糊（`--bg-blur`，`state.js:951-969`，
  仅在配置了管理端背景图时生效）会暂时失效——**主程序自身两处先例同样是这个
  代价**；遮罩自己的 `backdrop-filter: blur(5px)` 不受影响，面板外的背景仍然
  是模糊的。
- **提示词档案无法切换当前选中项**：`openProfile()` 只重绘了编辑器，没有重绘列表，
  所以高亮要等下一次切页签才更新。现在 `openProfile()` / `newProfile()` / `load()`
  都会同步重绘列表，并且档案行整行可点即切换编辑对象（按钮区域除外）。
- **只能导出全部档案**：`/profiles/export` 本来就支持 `?profile_id=`，但前端没有把
  参数传下去。现在每个档案行都有「导出」按钮（单档，文件名带 id），顶部保留「导出全部」。
- **档案列表新增「设为当前」**：把某个档案写入主程序 `ui_data.active_profile_id`，
  并在列表里用左侧 3px 强调条标出当前使用的档案（与「正在编辑」的填充态区分）。

### 测试

- 新增 4 个路由回归用例：单档导出、未知档案导出 404、active 档案往返、未知档案 404。
- 新增 3 个样式契约用例：选中页签必须无位移/无边框/带 2px 底条、页签圆角必须是
  `0.55rem`、遮罩必须用语义 scrim token。已用变异测试确认这些断言都能捕获旧实现。
- 改写并加固「主程序退化 backdrop-filter」的契约用例：
  - 删掉那条**方向反了**的旧断言（它要求遮罩必须有 `backdrop-filter: blur(`，
    等于把错误结论固化）；
  - 新增 `test_main_container_backdrop_filter_never_degenerate`，用
    `_assert_shell_blur_is_neutralized()` 断言该规则存在、同时写 `-webkit-`
    前缀，且两个 `backdrop-filter` 的值都必须是 `none`、不得出现 `blur(`；
  - 新增 `test_scoped_selector_passes_installer_token_check`，把该选择器喂给
    主程序真实的 `installer.scan_css_scope()`，确保它仍能通过作用域检查；
  - 配套两个变异测试：把值写回旧的"抬离恒等值"写法、或漏掉 `-webkit-` 前缀，
    都必须被拒绝。

  合计 13 passed（`tests/test_css_scope.py`）。
- `tools/accept_fix.py` 做真实服务验收（**不注入任何 CSS**，只用服务端交付的文件）：
  5 视口 × 21 场景（4 次全新加载 / 10 个控件焦点 / 滚到底 / 页签往返 / 面板开关×3 /
  用户 `--bg-blur` 12px 与 0px）= **BUG 0/105，未测 0/105**。判据同时抓「黑」
  （面板外 `pct_navy > 3%`）与「平」（内容保留率 `retention < 0.10`），阈值由
  `tools/calib_accept_bands.py` 在 5 视口 × 4 场景上标定：正确实现 `min_ret` 0.201–0.213、
  `max_navy` 0.00–0.97%；旧交付（`blur(0.02px)`）plain 下 `min_ret` 0.007–0.012、
  壁纸场景 `max_navy` 35–99.8%——两侧间隔足够宽。每个视口先拍一张「遮罩隐藏」基准算
  `raw_sd`；该带内本无纹理时（1920/2560 的 R 带 `raw_sd = 0`）retention 无定义，
  记为 `unmeasured` 而非放行。开跑前先断言服务端交付的那条规则值就是 `none`
  （`ruleValue: "none"`），否则直接以退出码 2 拒绝验收——避免拿旧方案跑出假通过。
- `tools/verify_user_dpr.py` 在**用户真实显示条件**下再验一次：Windows 125% 缩放
  ⇒ `device_scale_factor = 1.348`，视口 `1532x899`（即用户截图的 2065x1212 物理像素）。
  分数 DPR 会改变图层光栅化尺度，所以不能只靠 DPR=1 的结论。结果：修复态
  **BUG 0/58，未测 0/58**（用户点名的 12 个控件逐个覆盖 + 其余 38 个控件全扫 +
   5 个档案行 + 页签往返 + 滚到底 + resize 往返）。
  该脚本同时带一个**对照组**：注入修复前的退化值，必须仍然复现黑块——
  实测 `L: retention 0.0394`（对照正常值 0.201–0.213），证明本脚本在该 DPR 下
  确实有检出能力，"修复后不黑"才是有效结论。

  **对照组踩的坑（务必保留）**：注入必须用**与修复规则完全相同的选择器**。
  修复规则是 `body:has(.plugin-stm-ai-assistant-overlay.is-open) .main-container`
  且带 `!important`；若对照组写成裸 `.main-container{...!important}`，特异性
  (0,1,0) 会被修复规则压掉，**对照组根本没生效**，于是读出一个假的"ok"——
  第一版脚本正是如此，差点得出"该 DPR 下不复现"的错误结论。同选择器 + 同为
  `!important` 时后注入者胜。脚本现在还会打印 `.main-container` 的 computed
  `backdrop-filter` 自证注入成功。

  **假覆盖踩的两个坑（务必保留）**：
  1. 用「选择器 + 序号」定位控件（如 `('.…-profile-editor input', occ)`）时，同一族控件
     会反复命中**同一批前几个元素**：id/名称/模型匹配/优先级 四组全落到同一批 input，
     各 textarea 标签全落到同样的前三个 textarea——用户点名的「用户模板 / 用户后缀 /
     Assistant 预填」**根本没被测到**，报告却显示"全绿"（排查时是从文件名与字节数重复看出的）。
     改为按**实测索引**固定定位；索引由 `tools/_probe_fields.py` 一次性探针打印真实 DOM 得到。
     编辑区共 50 个控件，关键索引：`0` id、`1` 名称、`2` 模型匹配、`3` 优先级、
     `9` 变量缺失策略、`10` 系统前缀、`11` 系统提示词、`12` 系统后缀、`13` 用户前缀、
     `14` 用户模板、`15` 用户后缀、`16` Assistant 预填。
  2. 档案行选择器写成 `.plugin-stm-ai-assistant-profile-row`——**这个类名不存在**，
     于是该场景被静默跳过（场景数从 53 涨到 58 才暴露）。真实类名是
     `.plugin-stm-ai-assistant-profile-item`（`frontend/views/profiles.js:73`）。
     教训：定位不到元素必须报错而不是跳过；场景总数变化要核对。

### 新增

- 多 provider 管理：`data/providers.json` 存地址/模型/超时，密钥按 provider 隔离在 `secrets.json`（`provider__<id>__api_key`），支持连通性自检。
- 提示词子系统：按模型匹配的档案（`model_match` + `priority`）、破限段（`system_prelude` / `system_suffix` / `assistant_prefill`）、格式要求与字数要求（同时作为生成约束与后置校验）、变量插值（未知变量在保存时即报错）、渲染预览与 token 估算、导入导出与重置。
- `ai.analyze` 作业：批量生成概览与标签建议，支持 SSE 进度、取消、暂停、恢复、只重跑失败项。
- 逐条审批：`apply_mode=review` 下条目置 `awaiting_review`，前端逐条接受/拒绝后由插件 `apply` 路由统一写回（带内容哈希陈旧检测）。
- 幂等：`content_hash`（卡片关键字段 + 世界书条目名集合）未变且档案/模型一致时跳过，避免重复扣费。
- 数据落点：概览写 `ui_data[ui_key].summary`（内部 bridge，失败降级到插件命名空间）；标签经 `apply_tags(replace=False)` 合并；新标签只进待审批队列。
- 成本记账：作业级 token 用量汇总进 `ui_data`，明细追加 `data/usage/usage.jsonl`。
- 前端工作台：卡片选择、模式与档案选择、prompt 预览、运行、SSE 进度、结果审批、provider 与档案设置。
- CLI：`python app.py plugin stm-ai-assistant <command>`。
