# 方案：任务化重构（Task-centric Redesign）

> 前提：插件与插件系统**都未发布**，不考虑兼容，不考虑改动量。
> 目标：让「打标 / 世界书精简 / 分类移动 / 结构提议」四个能力各自拥有**可辨认的入口、可直达的面板、可闭环的结果**，并且从**任何一个入口**进入时作用域与行为都一致。
>
> 关联分析：[2026-10-19-功能盘点与入口评估.md](2026-10-19-功能盘点与入口评估.md)。本方案逐条回应其中的 F1–F24。

---

## 0. 四条设计原则

1. **一个用户能力 = 一个任务（task）**。任务自带入口、面板、作业类型、结果渲染器与落地动作。不再把多个能力塞进同一个长表单用序号串起来。
2. **入口只声明「作用域 + 打开哪个任务面板」**，不承担业务语义。面板打开后第一屏就是「这份作用域会被怎么处理 + 一个主按钮」。
3. **作用域是面板的局部状态，不是插件全局状态**。任何入口都不写共享的 `selectedCardIds`；作用域对象只活在当前任务面板里，面板关闭即消失（回应 F16）。
4. **结果与落地统一到一个审批面**，按 `job_type` 渲染不同形态（diff / 迁移 / 方案勾选），落地前一次确认、可裁剪、有门禁（回应 F8 / F11）。

---

## 1. 目标形态

```
宿主入口层
├─ 坞：AI 助手 ────────────────┐
├─ 快捷键 Ctrl+Shift+A ────────┤
├─ 批量菜单（cards） 打标/归类 ┤
├─ 批量菜单（worldinfo）精简 ──┤
├─ 右键分类（cards） 打标/归类/精简嵌入书
├─ 右键分类（worldinfo）精简 ──┤
├─ 命令面板 设计分类体系/结果与审批
└─ 设置页  AI 助手设置 ────────┘
                                ↓
                    任务面板（每个任务一个 surface）
        ┌──────────────────────────────────────────┐
        │ 作用域横幅：分类「科幻」含子分类 · 12 张  [更改] │
        │ 参数（≤4 项）  预估成本 xx token / ¥x      │
        │            [ 开始打标 ]                   │
        │ ── 提交后同一面板内 ──                     │
        │ 进度 7/12  ▓▓▓▓▓░░░  [取消] [查看结果]     │
        └──────────────────────────────────────────┘
                                ↓
                    作业（host job 系统）
                                ↓
          结果与审批面板（按 job_type 渲染 + 裁剪 + 落地确认）
```

---

## 2. 信息架构：从「1 面板 7 段」到「7 个面板」

| 现在 | 现在的内容 | 改后 |
|---|---|---|
| 页签「工作台」 | 1 选卡 / 2 参数 / 3 预览 / 4 进度 / 5 世界书 / 6 归类 / 7 提议 | 拆成 **4 个任务面板**（打标 / 精简 / 归类 / 提议）+ **1 个任务中心**（hub） |
| 页签「结果与审批」 | 只认 summary/tags | **结果与审批面板**：按 job_type 分渲染，承载全部 4 类结果 |
| 页签「提示词档案」 | 全部档案混在一起 | **档案面板**：按任务分组，功能任务规格不再暴露给用户乱选 |
| 页签「设置」 | Provider/用量/建议 | **设置面板**（不变，补上「待审批/最近作业」概览） |
| — | — | **hub 任务中心**（坞入口首屏）：4 张任务卡 + 待审批卡 + 最近作业 |

**页签全部取消**。取代它的是「每个 surface 一件事」：入口直接打开对应 surface，hub 只是没有作用域时的统一出发页。这样「点了入口却落在长表单顶部」的问题从结构上消失。

---

## 3. 任务模型

新增 `frontend/tasks.js`（任务注册表），用一份声明驱动入口、面板、提交与渲染。

```js
// 每个任务一份声明；入口、面板头部、提交参数、结果渲染都从它派生
export const TASKS = {
  analyze: {
    id: 'analyze',
    label: 'AI 打标',
    verb: '打标',
    summary: '为卡片生成一句话概览与标签建议',
    jobTypes: ['ai.analyze'],
    scopes: ['card-selection', 'card-folder'],      // 支持哪些作用域
    surface: 'analyze',                              // 对应 surface id
    result: 'analyze-result',                        // 结果渲染器 key
    writes: ['note', 'tags', 'suggestions'],         // 可落地项（apply 时可选）
    needsProfile: 'analyze',
  },
  polish: {
    id: 'polish',
    label: 'AI 世界书精简',
    verb: '精简',
    summary: '压缩世界书条目正文，可选转 YAML；写回前逐条校验',
    jobTypes: ['ai.wi_rewrite'],
    scopes: ['wi-selection', 'wi-folder', 'embedded-books'],
    surface: 'polish',
    result: 'polish-result',
    writes: ['worldbook'],
    needsProfile: 'wi_rewrite',
  },
  classify: {
    id: 'classify',
    label: 'AI 自动归类',
    verb: '归类',
    summary: '判断卡片所属分类并移动目录',
    jobTypes: ['ai.classify'],
    scopes: ['card-selection', 'card-folder'],
    surface: 'classify',
    result: 'classify-result',
    writes: ['move'],
    needsProfile: 'classify',
  },
  propose: {
    id: 'propose',
    label: 'AI 设计分类体系',
    verb: '提议',
    summary: '分析整个卡片库，提出分类方案与标签池；只创建结构，不动卡片',
    jobTypes: ['ai.propose', 'ai.agent'],
    scopes: ['library'],                             // 唯一一个库级任务
    surface: 'propose',
    result: 'propose-result',
    writes: ['categories', 'tags'],                  // tag_merges 永远只展示
    needsProfile: 'propose',
  },
};
```

**作用域能力 → 入口**由注册表交叉生成，避免「写了入口忘了功能」：

```
for (task of TASKS)
  for (scope of task.scopes)
    entry = ENTRY_FOR[scope]        // batch / context / command 三种落点
```

---

## 4. 入口清单（最终形态）

`manifest.frontend.entries` 从 4 条扩到 **12 条**，每条都对应「一个任务 + 一种作用域」，宿主零猜测。

| # | entry id | kind | when | 宿主落点 | 打开 | 作用域 | 服务任务 |
|---|---|---|---|---|---|---|---|
| 1 | `workbench` | dock | — | 坞 / 快捷键 | `hub` | 无 | 全部（任务中心） |
| 2 | `analyze-selected` | batch | `mode:["cards"]`, `selection:"any"` | 批量菜单 | `analyze` | 选中卡片 | 打标 |
| 3 | `analyze-folder` | context | `mode:["cards"]`, `target:"folder"` | 右键分类 | `analyze` | 该分类（含子分类）全部卡片 | 打标 |
| 4 | `classify-selected` | batch | `mode:["cards"]`, `selection:"any"` | 批量菜单 | `classify` | 选中卡片 | 归类 |
| 5 | `classify-folder` | context | `mode:["cards"]`, `target:"folder"` | 右键分类 | `classify` | 该分类全部卡片 | 归类 |
| 6 | `polish-selector` | batch | `mode:["worldinfo"]`, `selection:"any"` | 批量菜单 | `polish` | 选中的世界书文件 | 精简 |
| 7 | `polish-folder` | context | `mode:["worldinfo"]`, `target:"folder"` | 右键世界书分类 | `polish` | 该分类下全部世界书 | 精简 |
| 8 | `polish-embedded` | context | `mode:["cards"]`, `target:"folder"` | 右键卡片分类 | `polish` | 这些卡片的内嵌世界书 | 精简 |
| 9 | `propose-library` | command | — | 命令面板 | `propose` | 全库 | 提议 |
| 10 | `open-review` | command | — | 命令面板 | `review` | 待审批作业 | 全部 |
| 11 | `open-profiles` | command | — | 命令面板 | `profiles` | — | — |
| 12 | `assistant-settings` | settings | — | 设置页 | `settings` | — | — |

**命名统一为用户词**：`打标` / `精简` / `归类` / `提议` 全部写进 `label` 与 `description`，命令面板立刻可检索（回应 F2 / F3）。

**入口数 12 ≤ 宿主上限 16**；坞只占 1 槽（`MAX_DOCK_ENTRIES_PER_PLUGIN=2`，留一个余量给未来）。

> 右键菜单在 cards 视图会多出 3 项（打标 / 归类 / 精简嵌入书），在 worldinfo 视图多出 1 项（精简）。插件项仍排在宿主项之后、单独分组（沿用现状），暂不引入二级子菜单——宿主目前的 `entries[]` 契约没有 submenu 字段，为 3 项去加一个宿主特性不划算；如果后续入口继续增长，再补 `group`/`parent` 字段。

---

## 5. 作用域模型（scope）

### 5.1 契约：宿主传语义，插件自己解析

**这是本方案唯一强制的宿主接口改动**（理由见 §9.1）。`ctx` 从「一串不透明 id」改成「一个语义目标」：

```js
ctx = {
  source: 'dock' | 'context' | 'batch' | 'command' | 'settings',
  entryId: 'analyze-folder',
  mode: 'cards' | 'worldinfo' | ...,
  target: {                       // 没有作用域的入口传 null
    kind: 'selection',            // selection | folder | card | none
    mode: 'cards',                // 作用域所属视图
    path: '科幻/太空',            // folder 时的分类路径（'' = 根目录）
    id: '',                       // card 时的卡片 id
    ids: ['...'],                 // selection 时的宿主选中集（仅作初始值，插件仍可校验）
  },
}
```

插件侧 `frontend/scope.js` 把 `target` 解析成具体条目，**解析失败必须抛错而不是返回空数组**：

```js
// 统一返回 { label, kind, mode, items, count } 或 throw ScopeError
export async function resolveScope(api, target) {
  switch (target?.kind) {
    case 'selection':   // 宿主给的是选中集，逐条校验可读性
      return scopeFromIds(api, target.mode, target.ids);
    case 'folder':
      if (target.mode === 'cards') {
        const { items } = await api.cards.list({ category: target.path, limit: 0 });
        return scopeFromCards(target.path, items);
      }
      if (target.mode === 'worldinfo') {
        const books = await api.worldbook.sources({ category: target.path });
        return scopeFromBooks(target.path, books);
      }
      throw new ScopeError(`不支持的作用域：${target.mode} 的分类`);
    case 'card':
      return scopeFromIds(api, 'cards', [target.id]);
    case 'none':
      return null;                 // 由面板自己提供「选作用域」入口
    default:
      throw new ScopeError('入口没有提供作用域');
  }
}
```

要点：

- **插件自己调 `api.cards.list({limit:0})` 解析分类**，宿主不再在点击瞬间拉 10 万条（回应 F20）。右键菜单**立刻关闭**，面板先弹出骨架、作用域区域显示「正在读取…」，读完后填数。
- **错误可见**：解析失败时面板顶部红条「无法读取分类「科幻」：<原因>」+「重试」，不再有「该分类下没有卡片」这种误导文案（回应 F20）。
- **不再有全局 `selectedCardIds`**：删掉 `frontend/selection.js` 的共享状态用法，改为每个面板持有自己的 `scope` 对象；坞打开时默认 `target: null`，面板内提供「用主程序当前选中」按钮（通过新的 `api.host.selection()` 读取，见 §9.1）。

### 5.2 三类作用域的数据形状

| scope.kind | 来源 | items 形状 | 落库/提交时需要 |
|---|---|---|---|
| `card-selection` / `card-folder` | `api.cards.list` | `{id, name, category, token_count}` | card_ids |
| `card` | `target.id` | 单张 | card_id |
| `wi-selection` / `wi-folder` | `api.worldbook.sources` | `{source_type, file_path, card_id, category, name, entry_count}` | worldbook source 列表 |
| `embedded-books` | 卡片集合 → 每张卡的嵌入书 | `{card_id, name, entry_count}` | card_id 列表（后端 resolve 成 embedded source） |
| `library` | 无 | 全库统计 | 无 |

---

## 6. 作业与审批

### 6.1 提交与进度统一

新增 `frontend/ui.js` 的 `runTaskJob()`，四个任务共用同一条管线：

```js
export async function runTask(ctx, { task, scope, options, onProgress }) {
  const { run_id, total } = await client.runTask(task.id, {
    scope: serializeScope(scope),
    options,
  });
  await rememberRun(task.id, run_id);          // api.storage: last_run_id per task
  return subscribeJob(run_id, { snapshot, item, log, done });
}
```

- 提交后**在同一个任务面板内**显示进度条（不再依赖「第 4 段」）。
- 完成后 toast + 角标 + 主按钮变成「查看结果」；可选设置「完成后自动打开审批」。
- `runId` 落 `api.storage['task.<id>.last_run']`，**关面板不丢**（回应 F10）。
- hub 与审批面板都通过 `api.jobs.list({limit: 20})` 展示最近作业（宿主已提供该接口，见 `ST-Manager/core/plugins/routes.py:780`）。

### 6.2 结果与审批面板

单一 surface `review`，按 `job_type` 选择渲染器：

| job_type | 条目渲染 | 批量操作 | 落地参数 |
|---|---|---|---|
| `ai.analyze` | 概览（全文）+ 标签 chips + 新标签建议 + 写什么开关 | 全选/全弃/按状态筛 | `write: ['note','tags','suggestions']` 逐条可覆盖 |
| `ai.wi_rewrite` | **原文 / 改写后 并排 diff**，顶部 token 比与原长比，`dropped_facts` 黄条，`issues` 红条 | 全部接受 / 只接受通过校验的 | 条目选择 + `target_format` |
| `ai.classify` | `现分类 → 目标分类`、confidence、reason；低置信度标黄 | 全部接受 / 按置信度筛 | 按目标分类勾选（可取消某些分类的迁移） |
| `ai.propose` / `ai.agent` | 分类方案树（可勾选）+ 新标签清单（可勾选）+ 合并建议（**只读**，明确写「需人工处理」） | 全选/全不选 | `categories: [...]`, `tags: [...]`（后端已支持，`routes.py:1384-1392`） |

**落地前必须一次确认**：弹 `api.ui.confirm`，列出「将写备注 8 条 / 加标签 21 个 / 移动 5 张到 2 个分类 / 创建 3 个目录 7 个标签 / 改写 12 条世界书」。确认后才调 `apply`。

**门禁补齐**：`_apply_propose()` 增加 `if status != 'done': continue`（回应 F11）；其余三个分支已具备。

**审批批量提交**：新增 `POST /jobs/<run_id>/review`（插件路由）一次提交 `[{key,status}]`，前端不再循环 N 次请求。

### 6.3 「分析目标」的归宿

删除提交参数 `mode`（概览/标签/两者）。**打标永远生成概览 + 标签 + 新标签建议**，写什么在落地时决定：

```jsonc
POST /jobs/<run_id>/apply
{
  "items": ["card-a", "card-b"],          // 省略 = 全部 done
  "write": { "note": true, "tags": true, "suggestions": true }
}
```

这消除「选了仅标签、应用时概览还是被写了」的假控件（回应 F12）。

---

## 7. 后端 API 重设计

五个 `jobs/<type>` 端点合并成任务化入口，五个 `apply` 分支保持但统一参数：

| 方法 | 新路由 | 说明 |
|---|---|---|
| POST | `/tasks/<task_id>/preview` | 统一预览：返回消息、token、成本、**作用域摘要**（多少条、预计花费） |
| POST | `/tasks/<task_id>/run` | 统一提交；`task_id ∈ {analyze, polish, classify, propose}`，`propose` 用 `options.engine: 'sample' \| 'agent'` 选择作业类型 |
| GET | `/jobs` | 作业列表（`limit/offset/status`），支撑「最近作业」与「找回」 |
| GET | `/jobs/<run_id>` | 作业详情（保留） |
| POST | `/jobs/<run_id>/review` | 批量审批 |
| POST | `/jobs/<run_id>/apply` | 统一落地：按 `job_type` 分派 + 裁剪参数 + 门禁 |
| POST | `/jobs/<run_id>/cancel` `/pause` `/resume` `/retry` | 保留（补 pause/resume，前端也要用） |
| GET | `/status` | 扩展：`tasks[]`（每任务最近一次运行）、`pending`（按 job_type 聚合）、`profiles.by_task` |
| GET | `/review/summary` | 待审批聚合，供 hub 卡片与角标 |

删除：`/jobs/analyze`、`/jobs/wi-rewrite`、`/jobs/classify`、`/jobs/propose`、`/jobs/agent`、`/wi/entries`（并入 preview）、`/proposal/sample`（并入 preview）。

**worldbook source 解析**：`resolve_wi_source` 扩展为接受
`{source_type: 'global'|'resource', file_path}` 与 `{source_type: 'embedded', card_id}`，
前端从 scope 直接产出这三种形状（不再从 `global::xxx` 这种宿主内部 id 反解）。

**worldbook 任务的两级选择**：作用域 = 世界书文件；面板内再列条目（默认仅启用项），用户可以只勾一部分。后端 `/tasks/polish/preview` 返回每个文件的条目清单与 token。

---

## 8. 配置与档案收敛

### 8.1 profile 增加任务维度

`profile.task` 字段（`analyze` / `wi_rewrite` / `classify` / `propose` / `agent`），内置档案自带。影响：

- 档案面板按任务分组（「打标档案」「精简档案」「归类档案」…）；
- 每个任务面板的档案下拉**只列本任务的档案**，不再出现「用 GPT 家族档案做归类」这种致命错配（回应 F13）；
- `default_profile_id` 拆成 `task_profiles: {analyze: 'builtin-gpt', classify: 'builtin-classify', ...}`，全局默认真正生效；
- 校验：保存时若 `task` 与 `output_schema.required` 语义冲突（如归类档案缺 `category`）直接拒绝。

### 8.2 配置项处置

| 配置 | 处置 |
|---|---|
| `batch_size` | **删除**（无消费者，作业按条目逐条跑） |
| `concurrency` | **实现**：作业内用线程池并发调用 LLM（默认 1，上限 8），或删除；建议实现——大批量场景是真实痛点 |
| `default_profile_id` | 改为 `task_profiles.*`（见上） |
| `apply_mode` | 保留为默认值，但**任务面板的写回方式下拉从它初始化**（不再硬编码 `review`），状态胶囊改读同一份来源（回应 F15） |
| `apply_tags` | 保留为默认值，落地时以 `write` 参数为准（回应 F15） |
| `summary_max_chars` / `max_tags` / `confidence_min` | 只作用于 analyze；其他任务的对应值来自各自 task spec |
| `content_selector` / `tag_pool` | 只作用于 analyze；档案里仍可覆盖 |
| `wi_keywords` / `wi_excerpt_ratio` | 只作用于 analyze 的提示词取样；精简任务不需要 |
| `tag_aliases` | 补编辑入口（档案面板内「标签别名」区），或删除该能力——**不能继续做隐形后端特性** |
| `validation_strict` / `skip_unchanged` / `price_*` / `currency` | 保留 |

所有配置**只有一个编辑入口**：插件内设置面板。主程序插件管理页的 `config_schema` 只保留密钥类（`secret_schema`），其余从 manifest 移除——消除「三处配置各说各话」（回应 F15）。

---

## 9. 宿主侧改动（只列真正的能力缺口）

### 9.1 `ctx.target` 语义化 + `api.host.selection()`（必须）

**现状**：宿主在点击右键项时同步拉整个分类的卡片（`contextMenu.js:162-183`，`page_size:100000`），失败返回 `[]`，插件无法区分「空」与「错」；批量菜单传不透明 id；命令面板完全不给 target。

**改法**：

1. `contextMenu.js` / `header.js` / `commandPalette.js` 统一构造 `ctx.target`（结构见 §5.1），**不再调用 `resolvePluginEntryTargetIds`**，删除该函数。
2. 新增 `api.host.selection()` → `{mode, ids}`，读 `store.viewState.selectedIds` 与 `currentMode`。插件最后一条 Provisional 耦合（`frontend/selection.js`）随之消失。
3. 命令面板：**过滤掉 `kind: "context"` 入口**（没有右键目标，语义不成立），保留 `batch` 并传当前选中集（回应 F22）；`kindLabel()` 补 `batch` 映射（回应 F23）。

### 9.2 `api.worldbook.sources()`（必须）

世界书任务需要「按分类列出世界书文件」。现在只有 `api.worldbook.read/write/list_entries`，没有列来源的能力。

在 `pluginHost.js` 的 `worldbook` 域加一个**纯前端方法**（数据取自 `store.wiList`，不新增后端路由）：

```js
sources({ category = '', sourceType = '' } = {}) {
  const list = storeRef()?.wiList || [];
  return list
    .filter((item) => !category || String(item.display_category || '').startsWith(category))
    .filter((item) => !sourceType || String(item.source_type || item.type) === sourceType)
    .map((item) => ({
      source_type: item.source_type || item.type,
      file_path: item.path || '',
      card_id: item.owner_card_id || '',
      category: item.display_category || '',
      name: item.name || '',
      entry_count: item.entry_count || 0,
    }));
}
```

### 9.3 批量菜单：移动端 + badge/progress（必须）

1. 移动端选中栏（`header.html:819-993`）渲染 `pluginBatchEntries`，与桌面同款按钮。
2. 批量菜单项补 `entry.badge` / `entry.progress` 渲染（与 `context_menu.html:136-139` 对齐），让「待审批」「运行中」在多入口可见（回应 F19）。

### 9.4 小修（可一起做）

- `commandPalette.js` 的 `kindLabel` 补 `batch: "批量"`。
- `header.js` 的 `refreshPluginBatchEntries` 保留 `filter(enabled)`（批量菜单是动作清单，不适用就该收起）——同步修正设计文档 §8.1 与 manifest 里「未选卡可见但置灰」的表述（回应 F24）。
- `api.jobs` 增加 `review(runId, itemKey, status)` 与 `reviewBatch(runId, updates)`，让插件不再手拼 `/jobs/<id>/items/<key>/review` 路径。

> 不做的宿主改动：卡片级右键菜单（单卡场景用「勾选一张 → 批量菜单」覆盖）、入口二级子菜单（3 项以内不值得加契约）、`when` 新增字段。

---

## 10. 实施顺序（4 个 PR，各自可验收）

### PR1 · 宿主语义化 + 插件骨架（打标跑通）

- 宿主：`ctx.target`、`api.host.selection()`、`api.worldbook.sources()`、命令面板过滤、`kindLabel`。
- 插件：task registry、`scope.js`、`jobPipeline.js`、`hub` surface、`analyze` surface（作用域 → 参数 → 预览 → 提交 → 进度）。
- 旧 workbench 暂时保留为隐藏入口，PR4 删除。

**验收**：右键分类「科幻」→ 面板 300ms 内出现 → 作用域显示 12 张 → 开始打标 → 同面板进度到 100% → 结果可写回；把分类路径写成不存在时，面板显示错误而不是「0 张」。

### PR2 · 审批与作业生命周期

- 插件：`review` surface（四类渲染器 + 裁剪 + 落地确认 + 批量审批），`runId` 持久化，hub 的「最近作业」「待审批」卡片，角标统一。
- 后端：`GET /jobs`、`POST /jobs/<id>/review`、apply 统一参数与门禁、propose 门禁修复。

**验收**：关掉面板再打开能从 hub 找回上次作业；打标审批后「仅标签」只写标签；未审批的提议点落地会被拦。

### PR3 · 世界书精简 + 自动归类（新作用域）

- 插件：`polish` surface（文件 → 条目两级选择 + diff 审批）、`classify` surface（分类方案勾选 + 低置信度提示）。
- 后端：`/tasks/polish/*`、`/tasks/classify/*`、worldbook source 解析、`worldbook.sources` 对接。

**验收**：worldinfo 视图批量选中 3 本世界书 → 精简 → 审批页看到并排 diff → 落地写回；右键卡片分类「精简嵌入书」只处理这些卡的内嵌书；归类任务只用归类档案。

### PR4 · 结构提议 + 配置/档案收敛 + 入口收尾

- 插件：`propose` surface（采样/Agent 切换 + 方案预览）、档案按任务分组、设置面板收敛、删除旧 workbench/selection.js/四页签。
- manifest：12 条入口定稿，全部用用户词命名；`config_schema` 只留密钥。

**验收**：命令面板搜「打标」「世界书」「精简」「归类」「提议」全部命中；插件管理页不再出现 15 项配置表单；提议的方案可勾选后创建。

---

## 11. 删除清单

| 位置 | 删除内容 |
|---|---|
| —— | 下表里 `frontend/views/workbench.js` 与 `frontend/selection.js` 指向**已删除**的文件，因此 `ls` 找不到它们属正常 |
| `frontend/panel.js` | TABS、`tabForTarget`、`applyEntryContext`、`createPanelBody`（拆成 hub + 各任务面板） |
| `frontend/views/workbench.js` | 整个文件（1–7 段拆分到 4 个任务面板） |
| `frontend/selection.js` | 整个文件（改用 `api.host.selection()`） |
| `frontend/views/review.js` | 重写（按 job_type 渲染） |
| `backend/routes.py` | `/jobs/analyze`、`/jobs/wi-rewrite`、`/jobs/classify`、`/jobs/propose`、`/jobs/agent`、`/wi/entries`、`/proposal/sample` |
| `backend/jobs.py` | `mode` 相关动作过滤分支（改到 apply 侧） |
| `manifest.json` | `config_schema` 除密钥外的 15 项；`description` 重写为覆盖四个能力 |
| `frontend/plugin.js` | `context.selectedCardIds` / `openTarget` / `applyMode` / `dryRun` / `force` 等全局运行状态（改由各面板局部持有） |

---

## 12. 测试计划

| 层 | 新增用例 |
|---|---|
| 契约 | 「每个 task 在 manifest 里有 ≥1 个入口」「每个入口的 surface 在 plugin.js 注册过」「入口 label 含用户词（打标/精简/归类/提议至少各一）」「入口数 ≤ 16、dock ≤ 2」 |
| 作用域 | `resolveScope()` 纯函数：selection / folder / card / wi-folder / embedded 五类；分类不存在 → 抛错而非空；根目录 → 全库且标注「全库」 |
| 作业 | 四个 task 的 preview/run 参数映射；`apply` 的 write 裁剪、items 裁剪、propose 门禁 |
| 渲染契约 | review 渲染器：`ai.wi_rewrite` 必须渲染 `candidate`；`ai.classify` 必须渲染 `category`；`ai.propose` 必须渲染 `plan`（静态断言，防止再次退化成只认 summary/tags） |
| 状态 | 从右键进入后关闭面板，再从坞打开，作用域必须为空（防 F16 回归） |

---

## 13. 取舍说明

1. **不自动提交**。入口打开的是「确认页」而不是「立刻花钱」。主按钮就在第一屏，视觉上等价一击；但保留了「取消/改作用域」的机会。对 `propose` 这类整库任务尤其必要。
2. **任务面板独立 surface，不做单一大面板**。代价是 surface 数量从 2 涨到 9，收益是入口可以直接指定落点、每个面板参数数量压到 ≤4 项、快捷键可以按任务绑定。
3. **世界书任务以「文件」为作用域，条目在面板内选**。因为宿主的世界书视图以文件为可选中单元，直接对齐宿主语义；embedded 书通过「卡片分类」入口覆盖。
4. **归类任务必须能选分类方案**。现在 `allow_new_category` 是单个布尔，用户无法预选；方案里改为第一步「读取分类 → 勾选允许的目标」，第二步才提交。
5. **提案的 `tag_merges` 永远只读**。它改卡片标签、破坏性最强，保持「只展示 + 明确提示需人工处理」，不放进 apply 勾选（与现有后端 `_apply_propose` 的取舍一致）。

---

## 附录：最终入口 × 功能矩阵

| 功能 | 坞 | 快捷键 | 批量菜单 | 右键菜单 | 命令面板 | 设置 |
|---|---|---|---|---|---|---|
| 打标 | ✅ hub | ✅ | ✅ cards | ✅ cards 分类 | — | — |
| 世界书精简 | ✅ hub | ✅ | ✅ worldinfo | ✅ worldinfo 分类 · ✅ cards 分类（嵌入书） | — | — |
| 自动归类 | ✅ hub | ✅ | ✅ cards | ✅ cards 分类 | — | — |
| 结构提议 | ✅ hub | ✅ | — | — | ✅ 库级 | — |
| 结果与审批 | ✅ hub 卡片 | — | — | — | ✅ | — |
| 提示词档案 | ✅ hub | — | — | — | ✅ | — |
| 助手设置 | ✅ hub | — | — | — | — | ✅ |
