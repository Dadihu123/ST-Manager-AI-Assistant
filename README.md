# AI 角色卡分析助手（stm-ai-assistant）

ST-Manager 的官方 AI 插件。批量把角色卡变成「一句话概览 + 结构化标签」，并把这件事做得可审计、可回滚、不重复扣费：

- **服务端调用 LLM**：API Key 只存在服务端 `secrets.json`，永不下发浏览器。
- **按模型分档的提示词工程**：档案（Profile）带 `model_match` / 优先级 / 破限段 / 格式与字数规则；模板变量保存时校验，预览先看再跑。
- **批量作业**：进度、取消、暂停、恢复、只重跑失败项；逐条审批后再写回。
- **幂等**：`content_hash`（卡片关键字段 + 世界书条目名集合）未变且档案/模型一致时跳过，避免重复扣费。
- **数据落点清晰**：概览写 `ui_data[ui_key].summary`（复用主程序渲染链路），标签默认合并写入，新标签只进待审批队列。

---

## 安装

插件本体放在主程序的 `data/plugins/` 下，**目录名必须等于 `manifest.id`**（`stm-ai-assistant`）：

```powershell
# 开发期：直接克隆到插件目录
git clone <本仓库地址> "<ST-Manager>/data/plugins/stm-ai-assistant"
```

1. 重启 ST-Manager（后端插件在 `create_app()` 阶段加载）。
2. 打开「设置 → 维护与高级 → 插件管理」，确认状态为「已启用 / 加载成功」。
3. 入口坞（屏幕右侧的图标条）会出现「AI 助手」入口；点击打开宿主提供的**居中弹层**。
   也可以在命令面板里搜「AI 助手」。

> **需要主程序 `PLUGIN_API_VERSION >= 2`**（本插件 `api_version = 2`）；旧主程序会以
> `PluginApiVersionError` 拒绝加载。
>
> 主程序以 `st_plugin_stm_ai_assistant` 模块名加载 `backend/`，**不注入 `sys.path`**，因此插件内部一律使用相对导入。
> 插件私有数据（档案、缓存、用量）写在 `data/plugins/stm-ai-assistant/data/`，卸载时由主程序按需清理。

### 插件声明的入口

插件**不画入口**——入口在 `manifest.json` 的 `frontend.entries` 里声明，由宿主渲染。
四个入口落在**互不相同**的宿主位置，各自承载一种作用域：

| 入口 | 类型 | 位置 | 作用域 | 何时可用 |
|---|---|---|---|---|
| AI 助手 | `dock` | 入口坞 / 命令面板 | 主程序当前选中集 | 始终（快捷键 `Ctrl+Shift+A`） |
| 分析此分类全部卡片 | `context` | **右键菜单**（右键**分类**） | 该分类含子分类的全部卡片 | 角色卡视图下右键一个分类 |
| AI 分析选中卡片 | `batch` | **批量操作菜单**（顶部工具栏） | 勾选的全部卡片 | 角色卡视图下且至少选中一张卡 |
| AI 助手设置 | `settings` | 设置页插件区 | — | 始终 |

> **为什么「选中卡片」在批量菜单而不在右键菜单**：右键一个分类时，你的意思是
> 「分析这个分类」，与恰好勾选了哪几张卡无关。把「分析选中卡片」放进右键菜单
> 会让右键分类的作用域变成全局选中集——语义错位。
> 因此：右键菜单项 = 对**右键目标**整体操作；批量菜单项 = 对**选中集**操作。

> 快捷键是**建议值**：你不在入口总览页改绑时就直接生效；改绑后以你的配置为准
> （设置 → 插件管理 → 入口总览 → 快捷键列的「设定 / 改绑 / 清除」）。
> 注意 `Ctrl+K` 是宿主的命令面板，不要占用。
>
> 入口总览页的快捷键列**按面板去重**：多个入口指向同一个面板时只有第一个能设快捷键。
> `context`（右键）类**不提供**快捷键设置——右键菜单没有「右键目标」这个键盘概念，
> 且所有右键入口打开的是同一个面板，设了也没有可用落点。

> 本插件的归属分级是 `official`（登记在主程序 `static/plugins/catalog.json`，`tier` 只能
> 由宿主仓库清单决定，插件 manifest 无权声明），因此 dock 入口**默认进坞槽位**。
>
> 尽管 official 允许全部 7 类 kind，本插件**刻意只使用 community 也允许的 5 类**
> （`dock` / `context` / `batch` / `settings`），不声明 `nav` / `status`——这样即使
> 将来从 catalog 摘掉而降级，入口也不会被静默忽略。
>
> 不可用的入口**可见但置灰**，点击会跳到对应的配置位置，不会无反应。

> **未实现的 kind**：主程序目前只有 4 类 kind 有真实渲染消费者
> （`dock` / `context` / `command` / `batch`）。`settings` / `nav` / `status` 只在
> 契约里存在——入口总览页会如实标注「（宿主未实现）」，不会假装已生效。

### 界面约定

- 面板容器（定位、尺寸、滚动、焦点陷阱、ESC 关闭、遮罩合成）全部由主程序提供；
  插件只在容器内渲染内容。因此插件 CSS 里没有 `position: fixed`，也不接管键盘。
- 面板内容由「状态条 + 四个页签 + 主体」组成：状态条常驻显示品牌图标、档案数 /
  Provider 数 / 写回方式 / 待审批数。待审批数同时显示为坞入口上的角标。
- 主面板是 `modal`（居中弹层 + 遮罩）：它承载批量分析表单与审批列表，需要完整宽度。
- Prompt 全文预览走独立的 `modal` 弹层（不塞在页签里造成长滚动）。
- 从右键菜单 / 批量菜单进入时，面板顶部会显示**本次作用域**（如
  「作用域 分类「科幻」（含子分类）· 共 12 张待分析」），而不是只显示「已选 N 张」。

> **关于图标**：入口图标（坞 / 命令面板）用**宿主图标名**（`manifest.json` 的
> `entries[].icon`，如 `wand`），因为宿主图标槽不解析插件资源路径；
> 插件自己的品牌图标是 `frontend/assets/icon.svg`，走
> `/api/plugins/stm-ai-assistant/assets/…` 资源路由，挂在面板状态条左侧。
> 两条路径能力不同，详见设计文档附录 E.5。

---

## 快速上手

1. **配置 Provider**：AI 助手 → 设置 → 新增/编辑 Provider，填 `base_url`、默认模型与 API Key，点「测试连接」确认可用。
2. **（可选）调提示词**：AI 助手 → 提示词档案 → 复制内置档案后改写 `system` / 模板 / 字数规则。内置档案按模型家族自动匹配，不改也能跑。
3. **跑一批**：工作台 → 读取主程序当前勾选的卡片（或手动选择）→ 选 Provider/模型/档案 → 点「预览 Prompt」确认 token 与成本 → 点「开始分析」。
4. **看结果并写回**：结果与审批 → 逐条接受/拒绝 → 「应用已接受」。`auto` 写回模式下第 3 步就已直接写入。

---

## 配置项

配置由主程序设置页（`config_schema`）管理，也可在插件内调整：

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `default_profile_id` | string | `""` | 未显式指定时使用的档案 id；留空表示按模型自动匹配 |
| `max_tags` | integer | `8` | 最大标签数（全局上限，与档案规则取更严格者） |
| `summary_max_chars` | integer | `300` | 概览字数上限 |
| `confidence_min` | number | `0.5` | 标签置信度下限（低于此值丢弃） |
| `batch_size` | integer | `5` | 每批张数 |
| `concurrency` | integer | `1` | 并发数（过大易触发限流） |
| `apply_mode` | enum | `review` | `auto` 直接写入 / `review` 逐条审批 |
| `apply_tags` | boolean | `true` | 是否把 AI 标签合并写入卡片 |
| `skip_unchanged` | boolean | `true` | 内容未变化时跳过（幂等） |
| `validation_strict` | boolean | `false` | 严格校验：输出不通过即判失败，不做挽救 |
| `wi_excerpt_ratio` | number | `0.3` | 世界书节选占提示词预算的比例 |
| `wi_keywords` | string | `世界观,设定,背景,叙事要求,元数据,规则` | 条目名命中这些词的 content 优先进入提示词 |
| `price_per_1k_prompt` | number | `0` | 每千 prompt token 单价（用于成本预估，0 表示不估算） |
| `price_per_1k_completion` | number | `0` | 每千 completion token 单价 |
| `currency` | string | `CNY` | 计价货币符号 |

---

## 密钥项

密钥写在主程序 `data/system/secrets.json`，**环境变量优先级最高**（Docker/CI 场景不落盘）。

| 键 | 说明 | 环境变量 |
|---|---|---|
| `api_key` | 默认 Provider 的 API Key（未单独配置专属密钥时回退到这里） | `STM_PLUGIN_STM_AI_ASSISTANT_API_KEY` |
| `base_url` | 默认 Provider 的 API 地址 | `STM_PLUGIN_STM_AI_ASSISTANT_BASE_URL` |
| `model` | 默认 Provider 的模型 | `STM_PLUGIN_STM_AI_ASSISTANT_MODEL` |
| `provider__<id>__api_key` | 某个 Provider 的专属 API Key（`<id>` 大写、非字母数字转下划线） | `STM_PLUGIN_STM_AI_ASSISTANT_PROVIDER_<ID>_API_KEY` |
| `provider__<id>__extra_headers` | 该 Provider 的自定义请求头（JSON 字符串，可能含密钥故按密钥存储） | `STM_PLUGIN_STM_AI_ASSISTANT_PROVIDER_<ID>_EXTRA_HEADERS` |

示例：Provider `openai` 的密钥环境变量是 `STM_PLUGIN_STM_AI_ASSISTANT_PROVIDER_OPENAI_API_KEY`。

> 插件与设置页**从不回显**密钥明文，只显示「已设置 / 未设置 + 掩码」。

---

## 提示词变量表

模板里用 `{{ ns.key }}` 插值；`{{ ns.key? }}` 表示「可选，缺失渲染为空」。

| 命名空间 | 变量 |
|---|---|
| `card` | `name`（必填）`description` `personality` `scenario` `first_mes` `mes_example` `creator` `creator_notes` `system_prompt` `post_history_instructions` `tags` `alternate_greetings_count` |
| `worldbook` | `outline` `excerpt` `count` `total_tokens` `excerpt_tokens` `truncated` |
| `tags` | `library` `taxonomy` `blacklist` `isolated` `usage_top` |
| `limits` | `max_tags` `summary_max_chars` `confidence_min`（必填，由配置与档案合并得出） |
| `model` | `name` `profile_id`（必填） |
| `user` | `instruction`（改写/翻译指令） |
| `text` | `content`（待处理正文） |
| `wi` | `entry_name` `entry_keys` `entry_content` `target_ratio` |

规则：

- 模板中出现**未知变量**会在保存档案时被拒绝（`unknown_variable`）。
- 运行期必填变量缺失 → 该条目失败（`missing_variable`），不发起 LLM 调用。
- 转义：`{{{{` 输出字面 `{{`，`}}}}` 输出字面 `}}`。

---

## 数据落点

| 数据 | 位置 |
|---|---|
| 概览 | `ui_data[<ui_key>].summary`（卡片列表/详情页直接渲染）；写入失败时降级到插件命名空间并提示 |
| 标签 | 经主程序 `apply_tags(replace=False)` **合并**写入（黑名单标签会被拦截） |
| 新标签建议 | `ui_data._plugins_v1.stm-ai-assistant.tag_suggestions`（**不自动创建标签**） |
| 分析元数据 | `ui_data` 热缓存（最近 800 条）+ `data/cache/card_meta.jsonl` 全量 |
| 提示词档案 | `data/prompts/<id>.json`（内置档案随代码分发，被修改时落盘覆盖） |
| Provider | `data/providers.json` |
| 用量 | `data/usage/usage.jsonl` + `ui_data` 台账 |

---

## CLI

```bash
python app.py plugin stm-ai-assistant status                 # 配置 / provider / 档案 / 用量
python app.py plugin stm-ai-assistant profiles list          # 档案列表
python app.py plugin stm-ai-assistant profiles show builtin-gpt
python app.py plugin stm-ai-assistant profiles export --out=profiles.json
python app.py plugin stm-ai-assistant preview Alice.png      # 渲染提示词（不消耗配额）
python app.py plugin stm-ai-assistant analyze Alice.png Bob.png   # 同步分析（默认不写盘）
python app.py plugin stm-ai-assistant analyze Alice.png --apply   # 写回磁盘
```

> CLI 不启动服务器、不跑 `init_services`，因此作业 worker 不会运行——`analyze` 是**同步执行**。

---

## 故障排查

| 现象 | 处理 |
|---|---|
| 插件列表显示「加载失败」 | 查看管理界面的 `load_error`；常见原因是 `api_version` 与主程序不匹配（需更新主程序） |
| 坞里没有「AI 助手」入口 | ① 确认插件已启用且重启过；② 坞槽位上限 8 个、单插件最多 2 个，超额时入口进**溢出菜单**，可在入口总览页固定；③ 打开控制台看 `[pluginHost]` 日志（样式未通过作用域检查时会被拒绝加载） |
| 点击坞入口没反应 | manifest 的 `surface.id` 与 `plugin.js` 的 `api.surfaces.register()` id 不一致时**不报错且无反应**（本插件统一用 `workbench`）；检查控制台有无 `[surfaceHost] 未注册的 surface` |
| 右键菜单里没有「分析此分类全部卡片」 | ① 该入口只在**角色卡视图**且右键的是**分类**时可用（右键卡片时它是灰的）；② 面板已打开时再次从别的分类右键进入，作用域会刷新（靠 `onOpen`），若仍显示旧分类请报告 |
| 批量操作菜单里没有「AI 分析选中卡片」 | 需要先勾选至少一张卡片（未勾选时菜单本身不显示）；置灰表示当前视图不是角色卡 |
| 面板里显示的作用域不是我以为的 | 右键菜单 / 批量菜单进入时作用域由入口决定，不由你后来勾选的卡片决定。要改用勾选集，点面板内的「读取主程序选择」 |
| 「分析选中的角色卡」是灰的 | 需要切到角色卡视图并至少选中一张卡片 |
| 提示「尚未配置 Provider」 | AI 助手 → 设置 → 新增 Provider 并填写 API Key |
| 提示「概览写入通道不可用」 | 主程序版本过旧或内部接口变化；概览会降级保存在插件内，标签写入不受影响 |
| 作业大量 `llm_auth_failed` | API Key 无效或权限不足（401/403 不重试） |
| 作业大量 `llm_rate_limited` | 降低并发数或换更宽松的供应商套餐（429 会退避重试 3 次） |
| 提交后立刻「全部跳过」 | 卡片内容与档案/模型未变（幂等生效）；需要重跑时勾选「忽略幂等，强制重跑」 |
| 应用时提示 `apply_stale` | 审批期间卡片被改动；确认无误后可勾选强制应用 |

---

## 开发与测试

```bash
# 纯函数层 + 装配层 + 路由的完整测试（不需要主程序，也不需要网络）
python -m pytest tests -q

# 与主程序仓库交叉验证（默认路径 D:\Workspace\MyOwn\ST-Manager，可用环境变量覆盖）
$env:STM_MAIN_APP_DIR = "D:\path\to\ST-Manager"
python -m pytest tests -q
```

代码结构：

- `backend/core/`：纯函数层（token 估算、内容哈希、模板渲染、档案匹配、输出解析、世界书取样）。**不导入 flask，也不导入主程序模块**，因此可以独立单测。
- `backend/`：装配层（`store` / `llm` / `jobs` / `routes` / `cli` / `summary_bridge`）。
- `frontend/`：ES module 前端。`plugin.js` 只做接线（注册 surface、回报入口状态、返回清理函数）；
  `panel.js` 是 surface **内容**渲染器（不含 overlay / ESC / 遮罩），并负责把宿主传入的
  入口作用域（`ctx.targetKind` / `ctx.targetIds`）折算成 `context.openTarget`；
  `dom.js` 里
  `icon`/`button`/`badge`/`section` 委托 `api.ui.*`，`styledSelect`/`numberInput` 为插件自有控件；
  `selection.js` 集中唯一的 Provisional 耦合点（读取选中卡片，仅坞 / 快捷键路径使用）；
  `assets/icon.svg` 是插件品牌图标（走插件资源路由，不占宿主图标名）。
  CSS 全部以 `.plugin-stm-ai-assistant` 作用域限定，且**不含 `position: fixed`**（层级归宿主容器）。
- `tests/`：`FakeApi` / `FakeCtx` 替身 + `FakeTransport` 打桩，覆盖纯函数、LLM 重试与取消、作业写回语义、HTTP 路由与审批应用；
  `test_manifest_contract.py` / `test_css_scope.py` 复刻主程序校验规则做静态自检，并用真实实现交叉验证。

设计文档：[`docs/2026-10-08-stm-ai-assistant-design.md`](docs/2026-10-08-stm-ai-assistant-design.md)（本仓库的唯一事实来源；实现与文档冲突时先改文档）。
