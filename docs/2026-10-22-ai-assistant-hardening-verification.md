# stm-ai-assistant 加固：实施与验证记录（2026-10-22）

> 配套文档：[`2026-10-22-ai-assistant-hardening.md`](./2026-10-22-ai-assistant-hardening.md)（实现规格）。
> 本文汇总四批实施的内容、判定口径与验收结果，供后续维护与复验时查阅。

## 1. 最终数字

| 项 | 起点 | 结束 |
|---|---|---|
| 插件单测 `python -m pytest tests -q` | 744 passed | **861 passed** |
| 前端冒烟 `node tests_frontend/smoke.mjs` | 17/17 | **27/27** |
| 宿主单测（`ST-Manager`） | 3954 passed | **3961 passed / 1 skipped** |
| 前置过滤命中率（2,543 条真实条目） | 51.4%（宏口径） | **24.0%（结构化口径）** |

## 2. 提交记录

- `9df15ed docs: 归档加固方案与四批实施记录到 docs/`
- `de779b2 fix(wi): uid=0 的条目不再被当成「没有 uid」`
- `41819f9 fix(profiles): 让「改提示词」真的生效（副本能盖过内置档案）`
- `e4e4815 feat(wi): 预览列表标出会被前置过滤跳过的条目（§7 UI）`
- `31e8e1d feat(propose): tag_merges 一键执行；C24 宿主令牌已验证（第四批）`
- `6ab7292 feat(analyze): 自定义字段放行、删标签、指纹与归类提示词（第三批 C11-C14/C22-C23）`
- `cba0538 feat(wicheck): 六个结构闸 + 检查器三选一策略（第二批 C15-C21）`
- `ff7dfe8 refactor(prefilter): 宏口径收窄为「结构性宏」，放开 695 条普通散文`
- `10febcd fix(review): 审批用宿主真实 item key（P2-9）`
- `7866a91 feat(wi): 前置过滤留出强制处理开关；任务卡文案去掉 YAML 说法`
- `0dbc3dc fix: 世界书精简判定与格式、审批链路、分类落地`
- `adcc51f feat!: 迁移到插件契约 v3（contributions 三原语 + capabilities）`

## 3. 四批实施详情

下面四节原文来自实施期间的批次报告，标题层级已下移一级。
它们记录了每批**为什么这么改**（含与计划不同的地方及其理由）、逐条验收结果、以及
当时的沙盒实测量。

---

## 第一批（C1–C10）实现与验收报告

> 依据 `2026-10-22-ai-assistant-hardening.md`（§0–§13），批次按 §14「第一批（止血）C1–C10」。
> 插件仓库 `ST-Manager-AI-Assistant` 已提交 `fix: 世界书精简判定与格式、审批链路、分类落地`；
> 宿主仓库 `ST-Manager` 的 H1–H4 改动**未提交**（见 §5）。

---

### 1. 单测

| 项 | 改动前 | 改动后 |
|---|---|---|
| `python -m pytest tests -q` | 744 passed | **799 passed** |
| 前端冒烟 `node tests_frontend/smoke.mjs` | 17/17 | **23/23** |

先改测试再改代码：计划里点名的 5 处旧断言全部按新预期改完，另外 2 处
（`test_instruction_keeps_guardrails`、`test_run_wi_rewrite_rejects_failed_validation`）
因为依赖被删掉的旧行为（格式分支、压缩过度判失败）一并改成新语义。

### 2. 逐条落点

| # | 位置 | 做了什么 |
|---|---|---|
| C1 | `backend/core/wicheck.py` | 删 `COMPRESS_TARGET` 判定与 `MIN_LENGTH_RATIO` 硬判；`>95%` → note「精简不明显」，`<50%` → note「删得偏多」。`issues` 只剩「内容被破坏」 |
| C2 | 同上 | HTML 配平改成先看 `html_balance(src)`：原文不配平就不再拿它判候选 |
| C3 | 同上 | `build_rewrite_instruction()` 无参，返回固定 `REWRITE_OUTPUT_RULE`（§0.2 那段，禁止外层键） |
| C4 | `jobs.py`、`routes.py`、`task.js`、`tasks.js`、`profiles.py` | 删「改写格式」下拉与 `target_format` 传递（旧客户端传了也忽略）；档案改名「世界书精简」；§2.1 的 system/user_template 替换 |
| C5 | 新增 `backend/core/prefilter.py`；`jobs.rewrite_entry` | §7 四类信号（名字/结构化树/INI/旧标记+代码构造）命中即 `skipped`，`force` 可绕过；**宏口径收窄为「结构性宏」**（见 §4.1），保护面 51.4% → 24.0% |
| C6 | `profiles.py`、`store.py`、`routes.py`、`jobs.py`、`preview.py`、`cli.py` | `task` 字段贯通；`select_profile(profiles, model, task='')`；删掉归类里硬编码 `builtin-classify` 的兜底 |
| C8 | `frontend/scope.js` | `as='embedded-books'` 补到 selection / card 分支（只转 card 模式的作用域） |
| C9 | `routes.py` `_apply_propose` | 有分类建失败 → `success: false` + 写明哪几个失败，并带回已建成的清单 |
| C10 | `core/classify.py`、`routes.py`、`jobs.py`、`task.js` | 新增 `normalize_category_items`：宿主字符串数组 / `{path,name,count}` 对象数组都认 |
| P2-9 | `frontend/views/review.js` | 宿主条目字段是 `key`，前端统一读 `item_key` → 审批发出的是 `undefined`。加 `itemKeyOf()` 单一出口（两种形状都认） |

### 3. 端到端（沙盒 `:5001`，真实模型 `deepseek-v4.1-flash`）

样本与 §0.2 的实测同一张卡：`未分类/宝可梦-战斗MVU-无印2.5.png`。

#### T1 前置过滤：数据类条目不送模型

| 条目 | tool 结果 | 是否调用模型 | prompt/completion |
|---|---|---|---|
| `[MVU] InitVar宝可梦初始设定`（277 token） | `skipped: protected:name` | 否 | 0 / 0 |
| `[InitVar]-MVU`（物华弥新） | `skipped: protected:name` | 否 | 0 / 0 |

**T1 通过**（§14.1 的验收口径是「不产生 LLM 调用」）。

> 期望值差异：计划 §13 的 T1 把期望写成 `protected:structured`，但 §7 的判定顺序是
> 「名字 → 结构化 → INI → 标记」，`InitVar` 名字先命中，实际返回 `protected:name`。
> 两者都表示「不送模型」，实现按 §7 的顺序。若要严格对齐 T1 的字面值，需要把
> 结构化检查提到名字检查之前。

#### T2 `[角色] 小遥（丰缘劲敌）`：走默认路径（不传 `force`）

| 指标 | 值 |
|---|---|
| token | 256 → **234**（−22，ratio **0.9141**） |
| 外层键（名称/关键词/原文） | **无** |
| YAML `\|-` block scalar | 无 |
| `<小遥>` 结构 | 保留 |
| `{{user}}` 宏 | 逐字保留 |
| issues / warnings | `[]` / `[]`（比例落在 0.5–0.95 之间，连 note 都没有） |

对比计划 §0.2 的旧结果（同一张卡、同一批条目）：

- 旧：短条目被加三层壳 → 比例 0.95 以上且 token 变多 → 判失败；
- 新：无壳、−22 token、`issues=[]`，直接进待审。

**T2 通过**（计划给的比例区间 0.90–0.97，实测 0.9141，与计划里小遥的 0.9188 基本一致）。

#### 卡面完整性

E2E 全程 `apply_mode=review`（不写盘）。两张卡改动前后 sha1 一致：

```
DE6E46AC3135850031B52ACE9FD76A9C7E07263E  宝可梦-战斗MVU-无印2.5.png
5307A1BF45679199A04E3F3BC6288B6ED33A1256  物华弥新.png
```

证据：`g1-candidates.json`、`g2-t1.json`、`g3-t2-force.json`、`g3-t2-pair.txt`、
`g6-prefilter.json`、`g7-default-path.json`、`g7-t2-candidate.txt`。

#### 宿主链路（H1/H2/H4，零模型调用）

| 检查 | 结果 |
|---|---|
| `/categories` 返回对象数组 | 15 个分类，全部含 `{path, name, count}` ✔ |
| item_key 含 `/` 的条目走审批 | HTTP 无错（改前 404），状态 `awaiting_review → done` ✔ |
| 只发 `{status}` 时保留原 result | 19 个字段前后完全一致，`summary` 228 字 → 228 字 ✔ |

证据：`g5-host.json`。

### 4. 实现中发现的 4 个要点（计划里没写死，按实测取了保守解）

#### 4.1 前置过滤的口径：从「任意宏」收窄到「结构性宏」

§7 的原始写法把「任意 `{{...}}`」都算保护信号，于是 `[角色] 小遥` 这种只在散文里
写了 `{{user}}` 的条目默认被 `protected:macro` 跳过。按 §7 自己测的口径，
含宏条目占全库 44.8% —— **近一半条目用不了这个功能**。

用沙盒 40 张卡（2,543 条非空条目）离线重判两种口径（`g6_prefilter_calibration.py`）：

| 口径 | 保护条数 | 占比 |
|---|---|---|
| A 任意 `{{...}}` 都算（§7 原文） | 1,306 | 51.4% |
| **B 只算结构性宏（现行）** | **611** | **24.0%** |

- **B 比 A 多放开 695 条**，全部是「散文里只有一个 `{{user}}`/`{{char}}`」的普通条目；
- **没有新增误挡**（0 条 A 不挡而 B 挡）；
- §12.3 实测被改坏的 7 条**全部仍被保护**（按名字/结构信号共 41/41 命中）。

**为什么这样更合理**：`var_func` 本来就单独覆盖了 `{{getvar}}`/`{{setvar}}`/
`{{format_message_variable}}` 这类**改变程序行为**的宏；`macro` 那一条只是把
「文本替换」也一起挡了。而 §12.3 那 7 条损坏靠的是名字与结构，与宏无关。
纯文本宏丢了会怎样？`wicheck` 的「宏保真」是**硬闸**（`issues` 级），
所以这一层不必再挡——这正是「①尽量不送 / ②送出去也拦」的分工。

实现上就是把 `_LEGACY_MARKERS` 里的 `macro` 那条删掉，并把 `var_func` 的正则
收紧到「写/读变量与格式化」的具体宏名（不再用 `get|set|...` 的宽前缀）。

复核：口径调整后重跑默认路径（不传 `force`），`[角色] 小遥` 正常产出候选
（0.9141，见 §3 的 T2 表），`[MVU] InitVar` 仍然零 token 跳过。

「强制处理被过滤条目」的勾选框仍然保留——剩下 24% 里确实有需要强制处理的场景
（比如用户在「名字像垃圾但内容重要」的条目上明知有风险也要试）。

#### 4.2 `select_profile` 的「任务兜底」边界

`builtin-deepseek` 也绑了 `task='analyze'`，但它有 `model_match='deepseek-*'`。
如果任务兜底不加限制，**任何未知模型跑打标都会拿到 DeepSeek 的提示词**（实测把
`test_preview_*`、`test_submit_analyze_*` 打红了）。因此兜底只认
「`model_match` 为空」的功能型档案，`*` 仍是最后一级兜底。

另：`select_profile(..., task='')` 不做任务过滤（保持旧行为），否则
`test_select_profile_prefers_matching_family` 这类无任务调用会拿不到 deepseek 家族。

#### 4.3 宿主 H4 的 `count` 是累计值

宿主 `category_counts` 本身就是「含子分类」的累计语义（`core/data/cache.py`
对每级前缀 +delta）。契约里透传该值，与主 UI 的 `/api/list_cards` 数值一致，
但没有做「严格自身卡数」。要严格自身计数需要另算，未在本批做。

#### 4.4 宿主 H3 的两个副作用

`_create_category` 复用 `_is_valid_category_path` 后：

- `a\b` 现在等价 `a/b`（反斜杠归一，与 `parent` / `rename_category` 一致）；
- `name='根目录'` 现在被拒（校验器把它当根关键字），旧实现会建一个字面目录。

### 5. 宿主侧（H1–H4，未提交）

| # | 文件 | 改法 |
|---|---|---|
| H1 | `core/plugins/routes.py` | `<item_key>` → `<path:item_key>`（卡片 id 含 `/` 时不再 404） |
| H2 | `core/plugins/jobs.py` `review_item` | `result is None` 时沿用条目原有 result，不再覆盖成 `{}` |
| H3 | `core/plugins/api.py` `_create_category` | 接受 `a/b` 多级路径，自动补齐父级，幂等 |
| H4 | `core/plugins/api.py` `_list_categories` | 返回 `[{path, name, count}]` |

宿主仓库自测：全量 `3954 passed` → **`3960 passed`（0 失败）**，
`-k "plugin or categor"` 子集 `1494 → 1500 passed`。

> 计划 §8 把 H4 的位置写成 `core/api/v1/cards.py` 的 `/api/categories`，
> 但宿主里没有这个路由；真正的实现点是插件 API 的 `_list_categories`
> （插件调用 `api.cards.list_categories()`）。

### 6. 不在本批（按 §11/§14）

- C11–C14、C22–C23（第三批）；C15–C21（第二批）；
- **C25**：候选与原文逐字节相同时作业级 `skipped: unchanged`。
  本批只做到「校验器不再判失败」，作业级跳过留给第四批。
  因此 `test_no_compression_is_an_issue` 改成了校验器层面的断言。
- §6 的三选一 UI、issue 分级（block/note 作为对外字段）、四项回执（C21）；
- 预览面板里把前置过滤命中项置灰（§7 UI）。

### 7. 建议的下一步

1. 第二批 C15–C21：把 §12 的结构闸落进 `validate_rewrite`，并接上 §6 的
   `checker` 三选一与四项回执。**紧接本批做，价值最大**——现在 `force` 打开了
   前置过滤的口子，但后面没有闸。
2. 宿主 H1–H4 先在 `ST-Manager` 提交，再决定是否同步到真机。
3. `PLAN.md` 仍在 `tmp/`（`.gitignore:32` 的 `/tmp*/`），要留版本历史需要先挪到
   `docs/`（命名可参照 `docs/2026-10-19-task-centric-redesign.md`）。
---

## 第二批（C15–C21 + C25）实现与验收报告

> 依据 `2026-10-22-ai-assistant-hardening.md` §11 P1、§6、§12。上一批（C1–C10）见
> `g4-batch1-report.md`。
>
> 单测基线：第一批交付时 799 passed（含 23 条前端冒烟）。

---

### 1. 单测

| 项 | 第一批后 | 第二批后 |
|---|---|---|
| `python -m pytest tests -q` | 799 passed | **831 passed** |
| 前端冒烟 `node tests_frontend/smoke.mjs` | 23/23 | **24/24** |

§14.1 要求「C15–C20 每个闸 ≥3 条单测（正/反/边界）」：实际每个闸 3–4 条，
另有 C21 的 6 条作业级用例与 C25 的 2 条（T7/T8）。

### 2. 六个闸（C15–C20）

全部落在 `wicheck.validate_rewrite`，产出进 `issues`（block 级）。

| # | 闸 | 判据 | 为什么需要 |
|---|---|---|---|
| C15 | 代码/注释逐字保留 | `CODE_BLOCK_PATTERNS` 抽出的每个块（EJS / `<script>` / def-class-function / const-let-var / `z.*(` / `#`、`//` 注释）必须**逐字**出现在候选里 | 实测 EJS 块数 7→7 没变，但**块内注释被改**；只比块数量抓不到 |
| C16 | 结构同构 | 原文能被 `yaml.safe_load` 解析成 dict/list 时，按路径展开成 `{path: 容器类型}`，候选缺路径或把容器压平成字符串即 block | MVU 的 61 条路径被压成 14 个变量，结构已坏却判通过 |
| C17 | 必留标签 + 大小写 | 原文的 ASCII 标签名集合必须都在；大小写变了单独报（`<Analysis>` → `<analysis>`） | `<Analysis>` 在 4/4 配置里都被删，提示词改不掉 |
| C18 | 数字集合 | 原文所有数字（含小数）必须都在候选里 | `30 级` 变 `很高` 就是丢设定 |
| C19 | INI 段完整 | 原文的每个 `[section]` 行必须保留 | 状态栏/MVU 模板靠段定位，少一段整块失效 |
| C20 | 整篇翻译 | 原文英文占比 > 25% 且候选掉到它的 40% 以下、同时汉字占比 > 15% | 实测英文条目被整篇译成中文（59% → 8%） |

`code_block` 里**没有再放宏**：宏由第一批已有的「宏保真」按集合比对（更严，
连新增宏都提示），避免同一问题报两遍。

#### 52 条真实候选逐条对照（§14.1 的验收口径）

`g8_gate_52.py` 把 12 个作业档案里的 52 条候选同时喂给「落地版 wicheck」和
「§12.3 原型 `f25_gate.py` 的闸」：

| 指标 | 结果 |
|---|---|
| 候选总数 | 52 |
| 宿主放行 → 新闸拦下 | **7**（原型 7） |
| 宿主放行 → 新闸放行 | **11**（原型 11） |
| 与原型判定一致 | **52 / 52** |
| 不一致 | **0** |

### 3. C21 检查器策略（§6）

#### 3.1 后端

- 作业参数新增 `checker ∈ {advise, enforce, off}`（默认 `advise`），与
  `apply_mode` 正交；非法值静默回落到 `advise`（旧客户端不传也能跑）。
- 落地判定（`jobs.rewrite_entry`）按 §6.2 矩阵：

| apply_mode | checker | 候选无 issue | 候选有 issue |
|---|---|---|---|
| review | advise | 进待审 | 进待审（标红，默认不勾） |
| auto | enforce | **自动落地** | 进待审 + `held_by_checker`（不写） |
| auto | off | 自动落地 | 自动落地（问题只回传） |
| auto | advise | 自动落地 | 自动落地（只标记） |

- **校验问题不再判 `failed`**：以前 `not checked['ok']` 直接返回 `failed`，
  候选白跑一趟。现在候选永远留在结果里，用户可以在审批页手动采纳
  （§6.2 硬规则 1）。
- 回执：`summary` 增加 `held`（检查拦下）与 `checker`，消息行改成 §6.4 的四项
  回执：`自动落地 X · 检查拦下 Y · 待审 Z · 无需处理 S · 失败 F`。
- 每条结果带 `block_count` / `note_count` / `metrics{orig_tokens, cand_tokens, delta}`。

#### 3.2 前端

- 世界书面板新增 **「落地策略」三选一**（§6.1）：先看再落地 / 检查通过的自动落地 /
  全部落地不做检查 → 映射到 `(apply_mode, checker)`。默认按待处理条目数自动切：
  **>5 条走 enforce，否则 review**（§6.1 的「批量没人愿意逐条点」）。
- 审批页：
  - 有 block 问题的条目标 `is-blocked` + 徽章「检查拦下 · 需单独确认」；
  - **「全部接受」只接受没有 block 问题的条目**，其余要求逐条确认
    （§6.2 硬规则 2 / T11）。note 级（`warnings`）不拦；
  - 世界书结果改为 §6.4 的指标行：`原 token / 改写后 / 差值（−22 或 未改，已足够精炼）`，
    note 以「提示：」显示，block 以「检查器拦下（N 项，仍可手动接受）」显示。

### 4. 顺带修掉的两个链路缺陷

#### 4.1 C25（第四批的项，提前做了）

`[MVU] InitVar` 强制改写时模型**原样返回**（277 → 277 token），旧逻辑仍算
「自动落地成功」并写回同一份内容——世界书版本+1、备份+1，用户什么都得不到。
现在逐字节相同 → `skipped: unchanged`（「无需处理」计数），不写盘。

沙盒实测（`g9-enforce.json`）：`written=0`、`skipped=1`、`item_status=done`。

> 为什么提前：这是真机链路里现场踩到的（本想验 enforce，结果先踩到这个），
> 6 行改动，且 T7 本来就在本批的测试清单里。

#### 4.2 `strip_think` 会吃掉世界书正文里的 `<analysis>`（§12.3 的一个解释）

原实现无条件把 `<think>`/`<reasoning>`/`<analysis>` 段当「思考段」剥掉。
但世界书正文里**真的会**用这些标签做小节标题——剥掉后候选被截断，
「标签不丢」闸还会把这笔账算到模型头上（§12.3 的「`<Analysis>` 在 4/4 配置里
都被删」很可能就是这么来的）。

现在：**原文里含这些标签时不再剥**（`parsing.has_think_tag`）。仍有 2 条单测覆盖
（`test_has_think_tag_*`、`test_think_like_tag_in_original_is_not_stripped`）。

### 5. 沙盒端到端

| 检查 | 结果 |
|---|---|
| 默认路径（review + advise）重跑 `[角色] 小遥` | 0.9141、无外层键、`issues=[]` ✔（第一批已验，本轮未变） |
| `force=True` + auto + enforce 跑 `[MVU] InitVar` | 模型原样返回 → `skipped: unchanged`、`written=0` ✔ |
| 52 条候选结构闸对照 | 7 拦 / 11 放 / 52 全一致 ✔ |

> **没有制造的场景**：enforce 的「拦下」在沙盒里没有自然出现——这轮模型每次都
> 输出得很干净（`[MVU] InitVar` 直接原样返回）。该路径由 6 条单测覆盖
> （`test_enforce_holds_blocked_candidate` 等），加上 52 条真实候选的离线对照。

### 6. 还没做的

- C11–C14、C22–C23（第三批：动态字段放行、打标删标签、归类提示词、propose 落地）；
- C24（宿主 z-index 令牌）；
- §9 剩余断链里与本次无关的几条（分类列表 UI 已在第一批 C10 修）；
- 预览面板里把前置过滤命中项置灰（§7 UI，仍缺）；
- 宿主 H1–H4 仍未提交。

### 7. 证据

- `g8-gate-52.md` / `g8-gate-52.json`：52 条候选逐条判定与对照；
- `g9-enforce.json`：enforce 策略在真机链路上的结果（原样返回 → unchanged）；
- 单测：`tests/test_wicheck.py`（闸门 + C21 + T7/T8）、`tests/test_parsing.py`、
  `tests_frontend/smoke.mjs`（落地策略、全部接受过滤）。
---

## 第三批（C11–C14、C22–C23）实现与验收

> 依据 `2026-10-22-ai-assistant-hardening.md` §5（打标）、§4（归类）、§3.2（propose 细节）、
> §11 P1。上两批见 `g4-batch1-report.md`、`g10-batch2-report.md`。
>
> 本批把「打标 / 归类 / 提议」三条功能链补到可用。

---

### 1. 单测

| 项 | 第二批后 | 第三批后 |
|---|---|---|
| `python -m pytest tests -q` | 831 passed | **852 passed** |
| 前端冒烟 | 24/24 | **25/25** |

### 2. C11：自定义输出字段能落地（`jobs.run_analyze`）

`result_payload` 以前是**硬编码白名单**，profile 的 `output_schema` 新增任何字段都
不会进结果、也不会进审批页 —— 「用户自定义提示词 + 自定义输出字段」这条路直接死掉。

现在按 `output_schema.properties` 动态放行：

- schema 声明过的字段一律进结果（`result['mood']` 这类自定义字段可直接用）；
- 运行时字段（`usage`/`model`/`content_hash`…）用 `setdefault` 保护，不会被模型输出覆盖；
- 返回 `schema_fields` 便于前端/排查时看清「这次实际落了哪些字段」。

实测（`g11-analyze-removals.json`）：`schema_fields = [card_type, content_flags,
language, suggested_new_tags, summary, tag_removals, tags]`。

### 3. C12：分析档案补齐三个字段

| 问题 | 改法 |
|---|---|
| `content_flags` 零定义（只给了 `max_items: 8`，可塞任意文本） | 提示词里定义「3–5 个短语，描述玩法类型、尺度、结构特征，不含褒贬评价」 |
| `card_type` 枚举 `values: []` + `fallback: keep` → 校验空转 | 填成 `['OC', '同人', '工具卡', '群组卡']`（与提示词里的候选一致） |
| 没有「审查现有标签」的输出字段 | 新增 `tag_removals: [{name, reason}]`，`max_items: 3` |

`tag_removals` 的措辞是**提示式**的（§5.1）：先说不该删的情况、再给上限，
「拿不准就不要删——宁可少删，不要错删」；system 里另加一条「没有把握的字段留空」。

实测：同一张卡的 `content_flags` 稳定给出 5 条（「开放世界探索」「数值养成和战斗」
「多角色互动叙事」「MVU变量驱动」「回合制地图探索」），符合定义。

### 4. C13：删标签终于有落地通道

| 层 | 改动 |
|---|---|
| 作业（`run_analyze`） | `tag_removals` 只保留**卡片真的有的**标签（其余进 warnings）、封顶 3 条；结果额外给 `remove_tags` 便于前端回传 |
| 动作（`actions._action_apply_tags`） | 支持 `remove=`：`api.cards.apply_tags(..., remove=[...])`，回执带 `removed` |
| 路由（`apply_job` analyze 分支） | 读 `payload.remove_tags`，与「该条结果建议过」+「卡片确实有」取交集后一次写入；回执 `details` 带 `added`/`removed` |
| 前端（审批页） | 每条 `tag_removals` 前一个勾选框，**默认不勾**；没勾就不往 payload 里放 `remove_tags` |

安全边界：auto 模式默认**不删**（`apply_removals` 显式开启才删）；删标签不可逆，
所以「默认保守」这条在代码和 UI 两层都落实了。

### 5. C14：review 模式也记内容指纹

以前 review 模式什么都不写 → `skip_unchanged` 永不生效，同一张卡重复提交就重复烧钱。

现在只写 `content_hash` / `profile_id` / `model` / `analyzed_at`，
**不写** `applied_runs` / `applied_at` —— 那才是「已落地」的判据。
（`test_review_mode_records_fingerprint_but_not_applied`）

### 6. C22：归类提示词三补（`core/classify.py`）

| 补什么 | 具体 |
|---|---|
| 幂等 | 「若卡片已经在某个分类，且该分类在候选里，直接返回该分类」 |
| 允许说不知道 | 「拿不准就给低 confidence；如果不属于任何候选分类，category 留空并在 reason 说明」 |
| 描述注入 | 方案里带 `description` 时按 `- 路径：描述` 渲染（`format_scheme` 本来支持，缺的是调用方给数据） |

配套行为修正：

- **`category` 留空不再判失败**（§4.4 的 note 级）：返回 `skipped: undecided` 并进待审，
  让人指定。少了这一步，auto 模式会把卡移到空分类 = **根目录**。
- 「已在目标分类」的跳过原因从 `unchanged` 改成 `already_in_place`（§4.1 最后一行）。

> **没做到的部分（如实记）**：描述注入的数据源。宿主 `/categories` 只返回
> `{path, name, count}`，没有 description；当前 UI 也拿不到分类描述。
> 这条链要真正生效，需要「分类体系来自 propose 方案时把描述持久化」——
> 属后续工作，本批只保证「给了描述就一定用上」。

### 7. C23：propose/Agent 两处

#### 7.1 默认轮次按库规模自适应

固定 6 轮在几十张卡的库里会被 read 吃光（实测 `max_iterations=4` 时 4 轮全用于读卡，
第一轮作业就 `no_finalize`）。现在默认 `min(ceil(卡数 / 6), 12)`、下限 4；
调用方显式指定时不动。小库（8 张）实测给到 4 轮。

#### 7.2 轮次用尽后补一次 finalize（替代「整轮重试」）

计划写的是「捕获 `no_finalize` 自动重试一次并把轮次 ×1.5」。实现改了做法：

- 轮次用尽仍未 finalize 时，**追加一次**「不要再读卡，直接出提议」的请求；
- 成功则 `stop_reason = finalize_after_nudge`、`agent_meta.finalize_nudge = true`；
- 失败才报 `no_finalize`，并带上 `finalize_nudge: true` 让人知道已经补救过；
- **`iterations` 不把补救算进去**（「最多 N 轮」的语义不能被悄悄改掉），
  `agent_meta` 另外给 `max_iterations` 与 `finalize_nudge`。

为什么这样更合理：整轮重试要**再读一遍卡**（最贵的那部分），而模型多数时候其实
已经读够了；补一次调用成本有界（1 次）、不浪费读取配额，目标一致。

#### 7.3 没做：`tag_merges` 一键执行（P1-5）

按 §3.2 要在审批页给每条合并建议一个「执行」按钮（把 `from` 下的卡改挂 `to` 再删
`from`）。它需要「按标签列卡 → 批量 add/remove」的新路由 + 面板交互，本批未做，
留作下一批（见 §9）。

### 8. 沙盒端到端（1 次模型调用，review 不写盘）

| 检查 | 结果 |
|---|---|
| analyze 一张卡 → `schema_fields` 含 `tag_removals` | ✔ |
| `content_flags` 符合「3–5 个短语」定义 | ✔（实测 5 条，全是玩法/结构描述） |
| `tag_removals` 只列卡片真的有的标签 | ✔（该卡 **没有标签**，模型正确返回空数组，没有编造） |

> 这张卡恰好没有标签，所以「勾选后标签真的被移除」在沙盒里没有自然发生；
> 该链路由三层单测覆盖：作业层（`test_removals_apply_when_explicitly_enabled`）、
> 路由层（`test_apply_removes_selected_tags_only` 等 3 条）、UI 层
> （`分析的「建议删除标签」默认不勾选`）。

### 9. 剩余（第四批）

- C24：宿主 `--z-command-palette` 令牌（命令面板被插件面板遮住）；
- C23 的 `tag_merges` 一键执行（P1-5）；
- §9 剩余断链里还没处理的几条；
- §10 profile 编辑器支持编辑 `output_schema.properties`（自定义字段要能在这里加）；
- 前置过滤命中项在预览列表里置灰（§7 UI）；
- 宿主 H1–H4 仍未提交。

### 10. 证据

- `g11-analyze-removals.json`：analyze 的真实结果（schema_fields / content_flags / tag_removals）；
- 单测：`tests/test_jobs.py`（C11/C13/C14）、`tests/test_profiles.py`（C12）、
  `tests/test_classify.py`（C22）、`tests/test_agent.py`（C23）、`tests/test_routes.py`（C13 落地）、
  `tests_frontend/smoke.mjs`（删除勾选框）。
---

## 第四批（C24 + P1-5 + §9/§10 收尾）实现与验收

> 依据 `2026-10-22-ai-assistant-hardening.md` §11 P1（C24）、§3.2（P1-5）、§9、§10。
> 前三批见 `g4-` / `g10-` / `g12-batch*-report.md`。
>
> 本批之后 §8 的 5 处宿主改动全部完成（H1–H4 在第一批，H5=C24 在这里）。

---

### 1. 数字

| 项 | 第三批后 | 第四批后 |
|---|---|---|
| 插件单测 | 852 passed | **855 passed** |
| 前端冒烟 | 25/25 | **26/26** |
| 宿主单测（`ST-Manager`） | 3960 passed | **3961 passed / 1 skipped** |

### 2. C24：命令面板被插件面板遮住（宿主，§9 最后一条断链）

**问题**：`.command-palette` 和插件 surface 原先都取 `--z-modal-popover`。
但这台宿主的插件层带已经是 **3700–3740**（`--z-plugin-portal` … `--z-plugin-alert`），
命令面板 3400 比它们**低**——DOM 顺序在后的插件面板把命令面板整块盖住，
打开 AI 助手面板后 Ctrl+K 弹不出来。

**改法**（宿主侧，插件改不了——`test_manifest_contract.py` 明令禁止插件写 z-index）：

| 文件 | 改动 |
|---|---|
| `static/css/modules/variables.css` | 新增 `--z-command-palette: 3750`（+ `.z-command-palette` 工具类） |
| `static/css/modules/entry-dock.css` | `.command-palette { z-index: var(--z-command-palette) }` |
| `tests/test_plugin_surface_contracts.py` | 新增契约测试：令牌必须高于 `--z-plugin-alert`、低于 `--z-context-menu`，且命令面板只能用专用令牌 |

**与计划数值的差异（重要）**：计划写的是 `--z-command-palette: 3500`，「低于
`--z-modal-alert`(3600)」。但那份数值是在插件层带还停在 `--z-modal-popover`(3400)
时写的；本期宿主已经是 v3 契约、插件 portal 层带整体上移到 3700–3740。
**3500 仍然低于 3700，照抄的话这个 bug 一点没修**。所以取 3750：
高于整个插件层带、仍低于右键菜单(9000)/提示(9200)/Toast(9500)。
计划的本意（「命令面板要盖住插件面板，但不能盖住反馈层」）完全满足。

实测：沙盒 `:5001` 实际下发的 CSS 里已经是 `--z-command-palette: 3750`，
`.command-palette` 使用专用令牌。

### 3. P1-5：`tag_merges` 一键执行

**问题**：合并建议只展示、`requires_manual_review` 恒 true，没有任何落地通道。

**实现**（不需要宿主改动——`/api/batch_tags` 早有 `remove`）：

| 层 | 改动 |
|---|---|
| 后端 `routes._execute_tag_merge` | `from`（字符串或数组）→ 逐个查「带该标签的卡」→ 逐卡 `add(to)` + `remove(from)`；返回每条的 `moved` / `failed` |
| `_apply_propose` | 读 `payload.merges`，只执行**显式勾选**的；回执给 `merges_executed`；`requires_manual_review` 在有合并被真正执行后变为 false |
| 前端审批页 | 每条合并建议一个勾选框（**默认不勾**），文案说明「勾选后点确认写入才执行」；应用时把勾选项转成 `merges: [{from, to}]` |

安全边界：合并会删掉来源标签 → 默认全不勾；`from == to`、空 `to` 直接判 `invalid_merge`。

### 4. §9 七条断链的最终状态

| 断链 | 状态 |
|---|---|
| 选卡 → 世界书精简 → 预览「无法读取这本书」 | 第一批 C8 ✔ |
| 同一路径「开始精简」HTTP 400 | 第一批 C8 ✔ |
| 审批「接受」候选被清空 | 宿主 H2（第一批）✔ |
| 打标/归类审批 HTTP 404 | 宿主 H1 + 插件 P2-9（第一批）✔ |
| 归类面板「读取现有分类」永远 0 个 | C10 + 宿主 H4（第一批）✔ |
| 打开插件面板后 Ctrl+K 被遮 | **C24（本批）✔** |
| 世界书面板档案下拉 17 个全列 | 第一批 C6 ✔ |

### 5. §10 profile 通路的最终状态

| 能力 | 状态 |
|---|---|
| 新建/编辑/删除、复制内置、设置当前、导入导出、预览、恢复出厂 | 早已具备 ✔ |
| 按任务限定可选档案 | 第一批 C6 ✔ |
| 自定义输出字段能落地 | 第三批 C11 ✔ |
| **自定义字段能编辑** | **本批确认已完成**：档案编辑器的「高级：完整 output_schema 与写回动作」就是一个 JSON 文本框（`profiles.js:253/289-291`），加字段直接改 JSON 即可；配合 C11 的动态放行，改了就生效（`test_custom_schema_field_flows_into_result` 覆盖） |

### 6. 尚未做的（明确记录）

- **前置过滤命中项在预览列表里置灰**（§7 UI）：现在用户要等作业跑完，在「结果与审批」页才看到 `skipped` + 原因。属于体验优化，不阻塞主链路。
- **归类描述注入的数据源**（第三批遗留）：宿主 `/categories` 不返回 description，需要「把 propose 方案的描述持久化」才能生效。
- **宿主仓库的改动仍未提交**（H1–H4、H5/C24、以及对应的测试与文档）——留给用户 review 后自行提交。

### 7. 证据

- 宿主：`tests/test_plugin_surface_contracts.py::test_command_palette_sits_above_plugin_layers`；
  全量 `3961 passed, 1 skipped`。
- 插件：`tests/test_routes.py`（合并执行 3 条）、`tests_frontend/smoke.mjs`（合并勾选框默认不勾）；
  沙盒实发 CSS 校验（`--z-command-palette: 3750` 已生效）。

---

## 4. 如何再跑一轮真机验证

一次性验证脚本不进 git（它们写死了本机沙盒路径）。需要复验时：

1. 准备沙盒副本（`:5001`，绝不动 `:5000`）；
2. 在 `tmp/e2e-2026-10-22/` 下重建这几个驱动（按用途）：
   `harness.py`（接口封装 + 写卡安全网 `backup_card` / `restore_card` / `card_sha1`）、
   `h_sync.py`（同步代码到沙盒并重启，`--check` 只校验）、
   `h_preflight.py`（7 项前置条件，全 PASS 才开工）、
   `wi_scan.py`（本地读卡，绕过 API 过滤）、`h0c_pick.py`（挑测试对象）；
3. 按 `tmp/e2e-2026-10-22/H-VERIFY-PROMPT.md` 的 30 条用例逐条跑
   （打标 / 世界书精简 / 归类 / 提议），每条写清输入、期望、判定口径；
4. **写卡类用例务必先备份**：宿主 `/api/create_snapshot` → 改 → sha1 比对 →
   不一致就 `/api/restore_backup` 恢复；
5. 报告里每条用例附证据文件，并给出「模型行为」与「代码行为」两类结论——
   前者判定用例是否通过，后者判定是否有 bug。

## 5. 遗留与已知问题

| 项 | 状态 |
|---|---|
| 真机验证 30 条用例（打标 / 精简 / 归类 / 提议） | **未跑**，方案与夹具已备好（见 §4） |
| 归类提示词的「分类描述」数据源 | 不做（宿主分类是文件系统目录，无描述字段）；已改为「用户可自行改提示词」，并修掉了两处让它静默失效的问题 |
| apply 回执 `details` 仅标签写入时非空（P2-3） | 不做（不影响落地正确性） |
| 世界书摘要固定取前 ~2000 token（P2-6） | 不做（大卡覆盖率低，但 model 已被明确告知是节选） |
| 宿主 H1–H4 + C24 改动 | 已实施并跑过宿主全量测试，**提交范围待人工确认** |
