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

四个入口用**同一个图标**（插件品牌图形 `frontend/assets/icon.svg`），
在坞、右键菜单、批量菜单与入口总览里辨识度一致。

> **为什么「选中卡片」在批量菜单而不在右键菜单**：右键一个分类时，你的意思是
> 「分析这个分类」，与恰好勾选了哪几张卡无关。把「分析选中卡片」放进右键菜单
> 会让右键分类的作用域变成全局选中集——语义错位。
> 因此：右键菜单项 = 对**右键目标**整体操作；批量菜单项 = 对**选中集**操作。

> 批量菜单项**只在角色卡视图出现**（本插件只处理角色卡）。切到世界书 / 会话 /
> 预设视图时它会自动从菜单消失，而不是留一条永远点不动的灰项。
> 右键菜单项则相反：不适用时**可见但置灰**并给出原因。

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
- **顶部只有一行**：图标（左上）、副标题与标题、状态胶囊、关闭按钮全在宿主的
  标题行里。插件内容区只有「页签 + 主体」，不再自绘第二条状态栏。
  状态胶囊显示档案数 / Provider 数 / 写回方式 / 待审批数。
  待审批数同时显示为坞入口上的角标。
- **弹层尺寸固定**（`lg` 档 = 1080×760）：切换页签时容器不会跳动。
- 关闭按钮是主程序的通用关闭图标（与设置弹窗一致），不是文字「×」。
- Prompt 全文预览走独立的 `modal` 弹层（不塞在页签里造成长滚动）。
- 从右键菜单 / 批量菜单进入时，面板顶部会显示**本次作用域**（如
  「作用域 分类「科幻」（含子分类）· 共 12 张待分析」），而不是只显示「已选 N 张」。

> **关于图标**：全部图标位（`entries[].icon` 与弹层标题行）都用**插件自己的品牌
> 图形** `frontend/assets/icon.svg`，由主程序解析成
> `/api/plugins/stm-ai-assistant/assets/…` 并用 CSS `mask` 着色（跟随主题前景色）。
> 因此同一个插件在坞、右键菜单、批量菜单、入口总览与弹层标题里显示**同一个图标**。
> 详见设计文档 §E.5 与约束 C25。

---

## 快速上手

1. **配置 Provider**：AI 助手 → 设置 → 新增/编辑 Provider，选**接口协议**，填 `base_url` 与 API Key；
   点「拉取模型列表」挑模型（拉不到就手填），再点「测试连接」确认可用。
2. **（可选）调提示词**：AI 助手 → 提示词档案 → 复制内置档案后改写 `system` / 模板 / 字数规则。内置档案按模型家族自动匹配，不改也能跑。
3. **跑一批**：工作台 → 读取主程序当前勾选的卡片（或手动选择）→ 选 Provider/模型/档案 → 点「预览 Prompt」确认 token 与成本 → 点「开始分析」。
4. **看结果并写回**：结果与审批 → 逐条接受/拒绝 → 「应用已接受」。`auto` 写回模式下第 3 步就已直接写入。

---

## 支持的 API 协议

三种协议在 `backend/protocols.py` 里统一抹平：内部一律用 OpenAI Chat 的 `messages`
作为中间表示，入站转换、出站归一化，因此提示词层/解析层完全不需要知道协议差异。

| 协议 | 端点 | 鉴权 | 关键差异 |
|---|---|---|---|
| `openai_chat`（默认） | `/chat/completions` | `Authorization: Bearer` | 支持 `stop` |
| `openai_responses` | `/responses` | 同上 | 消息走 `input[]`；最大 token 是 **`max_output_tokens`**；**不支持 `stop`**（会被显式丢弃并记入 `metadata`） |
| `anthropic_messages` | `/messages`（基址自动补 `/v1`） | **`x-api-key`** + `anthropic-version` | **`system` 抽到顶层**；`stop` → **`stop_sequences`**；`max_tokens` 必填；`temperature` 上限 clamp 到 1 |

「接口协议」在 Provider 编辑器里选择，存于 `providers.json` 的 `api_style` 字段
（未知值一律回落 `openai_chat`）。

### 模型列表

`POST /providers/<id>/models` 走 `GET /models`，端点由协议推导（与真实请求同基址，
Anthropic 是 `/v1/models`）。**拉取失败永不阻塞配置**：返回 `ok=false` + 原因，
UI 退回手填模型名——很多第三方网关并不实现 `/models`。

> 内网/本地地址（Ollama 等）仍受出站约束限制，需在 Provider 里勾选「允许本地地址」。

---

## 结构提议（分析整个卡片库）

工作台「7 · 结构提议」有两种模式，**产出完全同构**（分类方案 + 标签池 + 合并建议），
落地方式也一样。差别只在「怎么看卡片库」：

| 模式 | 怎么读 | 适合 |
|---|---|---|
| 一次性采样 | 分层抽 30 张，一次调用 | 库小、类型集中 |
| **Agent 分批读取** | 先看全库目录，再自己决定分批读哪些 | 卡片多、类型跨度大 |

### 为什么需要 Agent

采样 30 张的问题不是「AI 想不出来」，而是**样本之外的部分它根本没看过**。
「分批」不等于「抽样」—— 这两者不可互相替代。

### 核心取舍：目录覆盖全库，读取只覆盖抽样

| | 覆盖范围 | 单张成本 | 1000 张总成本 |
|---|---|---|---|
| **目录** | 全库 | ~15 token（名称+标签+分类） | ~15k token，一次给全 |
| **读取** | 抽样 | ~150 token（含正文） | 预算内只能读 40 张 |

模型先看目录看清明细全貌，再挑**说不清该归哪类**的卡去读。
读不完的部分由目录兜底，而不是被忽略。

### 四条约束（缺一不可）

1. **轮次上限**（默认 6）—— 否则模型可以无限读下去
2. **读取配额**（默认 40 张）—— 否则一口气要读 200 张，请求直接超长失败
3. **读取门禁** —— 超预算时**回灌具体原因**，而不是静默截断
4. **上下文压缩** —— 丢弃旧读取结果

第 4 条是本能力最大的简化，也是**正当的**简化：

> shujuku 的 Agent 丢弃旧轮次后必须手写「交接报告」，因为丢的是已发生的推演过程，
> 丢了就真的没了。这里丢的是**可重放的读取结果**——卡片随时能再 `read` 一次。
> 所以只需要一句「这些可以重新 read」。

### 读取门禁实测

```
单批 2000 · 累计上限 6000
小批 1000 token          → 放行
累计已满                  → 拒绝（read-batch-too-large）
上下文将溢出               → 拒绝（near-context-overflow）
上下文将溢出但批量小         → 放行
```

拒绝时报告里带每张卡的 token 与上限。静默截断会让模型以为自己读到了，
基于残缺信息继续判断。

### 协议是 JSON，不是原生 tool_calls

三个原因都很硬：`tool_calls` 在 OpenAI Chat / Responses / Anthropic 三种协议里
结构完全不同（要写三套解析）；酒馆会合并连续 assistant 消息、丢掉 `tool_calls`；
而 JSON 模式**可以安全用预填充**（`{"action": "` 是稳定前缀）。

动作只有三种：

```json
{"action": "read",     "ids": ["a.png"], "detail": "summary"}
{"action": "search",   "query": "奇幻", "limit": 10}
{"action": "finalize", "categories": [...], "tag_merges": [...], "rationale": "..."}
```

多余字段**直接报错**，不静默忽略 —— 否则模型会以为那个字段有效，行为就漂了。

### Agent 全程只读

运行期间不写任何卡片。产出与采样模式同一套落地逻辑：

| 动作 | 安全性 |
|---|---|
| 创建分类目录 | ✅ 建空目录 |
| 创建标签 | ✅ 只写分类体系 |
| 标签合并 | ⚠️ 只展示，不执行 |

### `agent_meta` 会回传

```json
{"iterations": 4, "cards_read": 4, "max_reads": 20,
 "catalog_total": 10, "catalog_listed": 10,
 "catalog_truncated": false, "read_tokens": 68, "stop_reason": ""}
```

**方案的可信度取决于它看了多少。** `catalog_truncated=true` 表示目录被预算截断，
未列出的部分模型只能靠 `search` 找。

### 路径安全

与采样模式共用一套校验（`core.classify`）：

```
题材/奇幻        → 保留
../etc/passwd    → 丢弃（越级路径段）
notes            → 丢弃（主程序保留目录）
根目录           → 丢弃（不能当分类）
```

## 自动归类

给卡片判断所属分类并**移动到目录**。工作台「6 · 自动归类」：
读取现有分类 → 勾选允许的 → 开始归类。

**默认不接受方案外的分类**：AI 顺手发明分类会让卡片散落到意外目录，且难以回收。
方案外的结果直接判失败、卡片不动，除非显式勾选「允许模型新建分类」。

### 路径安全（两层）

主程序 `api.cards.create_category` / `move` 已内置白名单。插件仍做一层**保守预检**：
与其让主程序静默返回 `False`，不如在提交前就指出不合法的方案。

| 输入 | 结果 |
|---|---|
| `题材/奇幻` | ✅ 保留（支持子分类） |
| `父级\子级` | ✅ 归一化为 `父级/子级` |
| `..` / `../etc` | ❌ 越级路径段 |
| `/abs/path` | ❌ 绝对路径 |
| `a:b` | ❌ 非法字符 |
| `notes` | ❌ 与主程序保留目录冲突 |
| `a/a/a/a/a/a` | ❌ 层级过深（> 5） |
| `根目录` / `` | ✅ 根目录 |

> 路径安全校验**不受「允许新建分类」影响** —— `../etc` 即使开了开关也拒绝。

### 批量移动

同一目标分类的卡片合并成一次 `api.cards.move(card_ids, category)`（主程序支持批量）。
已在目标分类的卡片幂等跳过。

## 世界书改写

把世界书条目**精简**并可选**转成 YAML**。工作台底部「5 · 世界书改写」：
读取条目 → 勾选 → 选格式与写回方式 → 开始改写。

**格式只是提示词要求，不做格式校验**（设计文档 §6.3）。判断依据是「谁读这个输出」：

| | 严格 JSON（分析输出） | 世界书改写 |
|---|---|---|
| 谁读 | **代码**（`json.loads` → 写回） | **人和 AI**（正文进提示词） |
| 格式错的后果 | 解析失败 → 作业失败 / 静默空结果 | 读起来稍差，**不影响使用** |
| 因此 | 必须强校验 + 容错挽救 | 只需提示词要求 |

所以**不引入 `pyyaml`**，也没有 `safe_load` 校验。YAML 模式下要求 AI
用 **block scalar（`|-`）** 包裹多行文本，一次性消除 `:`、`#`、行首 `- ` 的转义问题。

### 只校验「内容有没有被破坏」

| 规则 | 级别 | 为什么 |
|---|---|---|
| 宏保真 | **拦住** | `{{…}}` 是程序注入机制，丢了就是功能性破坏 |
| HTML 配平 | **拦住** | 卡片前端靠 HTML 渲染，配平破坏会显示错乱 |
| 压缩有效 | **拦住** | 这是本能力的目的（需 ≤ 原文 95% token） |
| 压缩过度 | **拦住** | 少于原文 30% 视为「静默丢设定」 |
| 调度字段不变 | **拦住** | `constant` / `insertion_order` / `depth` 等是程序读的 |
| 专有名词覆盖 | **仅警告** | 限定词常被正确概括，硬拦会让正常改写频繁失败 |

丢掉的专有名词进 `dropped_facts`，在审批时供人确认。

### 写回是「整本一次」

`api.worldbook.write` 带 `source_revision` 乐观锁，逐条写会撞版本。
因此通过的条目按来源攒起来，最后整本写一次，期望值用读取时拿到的 revision。

## 标签池

决定**给模型看哪些标签、花多少 token**。存于档案的 `tag_pool` 字段，
缺省回落到全局配置，再回落到内置默认。

```jsonc
"tag_pool": {
  "source": "library",              // library | categories | manual
  "categories_include": [],         // 只取这些分类
  "categories_exclude": [],         // 排除这些分类（隔离分类会自动加入）
  "tags_include": [],               // 手动追加，可含库外标签
  "tags_exclude": [],               // 手动排除
  "respect_isolated": true,         // 遵守主程序的隔离分类（变成硬过滤）
  "budget_tokens": 1200,            // 按 token 而非条数
  "allow_new_tags": true,
  "max_new_tags": 5
}
```

**两层注入**（学世界书取样的做法）：

1. 分类概览 —— 只有分类名 + 该类标签数量，成本极低但让模型知道「有哪些维度」；
2. 预算内明细 —— 按 `(-使用频次, -与卡片词面重叠, 名称)` 排序。

实测预算控制（同一份库）：

```
budget= 200 → 81 token  listed=6  dropped=0
budget=  60 → 53 token  listed=2  dropped=4
budget=  25 → 23 token  listed=0  dropped=6
```

### 三个必须分清的概念

| 概念 | 含义 |
|---|---|
| `listed` | 进了预算、**给模型看**的标签（影响 token） |
| `allowed` | **候选全集**（来源过滤后）。不是禁止项 |
| `blocked` | **明确被禁**（隔离/排除分类/手动排除）。唯一该硬过滤的集合 |

> ⚠️ 拿 `allowed` 当硬过滤用是错的：预算会让 `listed ≪ allowed`，
> 于是「一设预算就把大量合法标签判成违规」，陌生标签也会被直接丢弃
> （而它本该进「新标签建议」）。
> 正确关系：`listed ⊆ allowed`，`blocked` 与 `allowed` 互斥。

### 隔离分类是硬过滤

主程序的隔离分类原先只作为变量 `tags.isolated` 给模型看（软约束）。
现在变成硬过滤：**这些分类下的标签即使模型输出也进不了卡片**，
并在结果里如实报告被挡下的名字。

### 标签别名（敏感词 / 近义收敛）

单向 `alias → canonical`，**只做出站替换**（模型输出替代词 → 程序换回真标签）。
入站不转换：标签池里照常给模型看真实标签名。

```jsonc
"tag_aliases": [
  {"alias": "情感纠葛", "canonical": "恋爱"},
  {"alias": "成人向",   "canonical": "R18"}
]
```

也接受 `[{"from": "a", "to": "b"}]` 与 `["a→b"]` 两种形态，便于从主程序规则集导入。
支持**链式**（`a→b→c` 解析到 `c`）与**环检测**。

> **时机很关键**：还原必须发生在「判断已知/新标签」**之前**。
> 否则真实标签会被误判成新标签塞进建议队列：
> `恋爱` 是库里的真实标签，若 AI 输出 `情感纠葛` 而没先还原，它就会进建议队列。
>
> **保存时拒绝两种错配置**：同一 alias 映射到多个 canonical（歧义）；
> alias 恰好是真实标签名（出站会误替换真词）。

## 内容选择器

决定**哪些卡片信息真的进提示词**。位置与组织交给模板，取值广度交给选择器——
两者分离后，同一份模板可以配不同选择器（分析概览时不要正则脚本，排查问题时才要）。

在 **提示词档案 → 内容选择器** 里配置，存于档案的 `content_selector` 字段；
档案缺省时回落到全局配置的 `content_selector`，再回落到内置默认（等同旧行为）。

| 维度 | 取值 | 默认 |
|---|---|---|
| 正文字段 | 简介 / 性格 / 场景 / 开场白 / 对话示例 / 作者 / 作者备注 / 系统提示词 / 历史后置指令 / 现有标签 | 全开 |
| 备用开场白 | 不发 / 全部 / 仅指定序号 | 不发（只给数量） |
| 附加内容 | 正则脚本摘要 / 酒馆助手脚本摘要 / 本地备注 | 全关 |
| 世界书 | 按预算自动取样 / 仅指定条目 / 不发；可另设 token 上限 | 自动 |

**两条重要行为**：

1. **未勾选的字段渲染为空串，不删键**。因此模板写了 `{{ card.description }}`
   也不会误报 `missing_required` ——「不发」和「模板没写」都得到空字符串。
   唯一例外是 `card.name`：它是卡片身份，永远发送。
2. **预览会报两类不一致**（`selector.gaps`）：
   - 模板引用了某变量、选择器却关掉了来源 → **会渲染成空，警告**；
   - 选择器放行了某变量、模板没引用 → 白勾，提示（元数据类变量不计入）。

### 空值不会在提示词里留下残句

渲染层会做两层清理，**不会出现 `简介：` 这种只有标签的孤儿行**
（那既浪费 token，也会让模型以为「简介本来就为空」而不是「没提供」）：

| 机制 | 作用 |
|---|---|
| 条件块 `{{# var }} … {{/ var }}` | 变量为空/缺失时**整段不输出**。这是推荐的写法，确定性由模板作者掌控 |
| 反向条件块 `{{^ var }} … {{/ var }}` | 变量为空/缺失时输出。用来写「没有标签库时换一种措辞」这类备用文案 |
| 标签行清理 | 兜底：某行引用了空值变量、且渲染后只剩标签（`简介：` / `简介=` / `- 场景:`），整行丢弃 |
| 空行折叠 | 条件块被丢弃后留下的连续空行合并为一个，并去掉首尾空行 |

清理只对**同时满足**「模板该行引用了变量」+「渲染后只剩标签」的行生效，
所以模板里写死的说明文字（如 `注意：以下是卡片信息`）不会被误吞。

内置档案已经全部改用条件块，因此关掉的字段在提示词里**完全不占位置**：

```
关闭前（全选）                       关闭后（只留简介与性格）
<card_data>                          <card_data>
名称：爱丽丝                          名称：爱丽丝
简介：一个流浪法师                     简介：一个流浪法师
性格：冷静、寡言                       性格：冷静、寡言
场景：王都的雨夜                      </card_data>
开场白：「你也是来避雨的？」
对话示例：…
现有标签：奇幻、法师
</card_data>
```

> 条件块支持嵌套（由内向外收敛），标签未闭合会在保存档案时直接报错。
> `{{# x }}` 里的变量名同样受「校验变量」检查，拼错会被拒绝。

`api.cards.read_extensions` / `api.cards.read_note` **只在选择器要求时才调用**，
不会为了没用到的数据白跑 IO。正则与脚本只发**摘要**（名称、匹配式、启用状态、变量键名），
绝不把脚本正文塞进提示词。

## token 明细（`tokens.by_block`）

预览返回按变量拆解的消耗，让「钱花在哪」可见：

```
tokens.by_block = {
  items: [{name, tokens, count, empty}, ...],   # 按 token 降序
  variables_total, template_overhead, total,
}
```

`template_overhead` = 总数 − 各变量之和，即模板里的固定文字与消息包装开销
（估算误差可能让它为负，此时夹到 0）。`count > 1` 表示该变量在模板里被引用了多次。

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
| `api_key` | **回退密钥**：默认 Provider 没有专属密钥时才用它 | `STM_PLUGIN_STM_AI_ASSISTANT_API_KEY` |
| `provider__<id>__api_key` | 某个 Provider 的专属 API Key（`<id>` 大写、非字母数字转下划线），**优先于回退密钥** | `STM_PLUGIN_STM_AI_ASSISTANT_PROVIDER_<ID>_API_KEY` |
| `provider__<id>__extra_headers` | 该 Provider 的自定义请求头（JSON 字符串，可能含密钥故按密钥存储） | `STM_PLUGIN_STM_AI_ASSISTANT_PROVIDER_<ID>_EXTRA_HEADERS` |

示例：Provider `openai` 的密钥环境变量是 `STM_PLUGIN_STM_AI_ASSISTANT_PROVIDER_OPENAI_API_KEY`。

**读取优先级**：专属密钥 → （仅默认 Provider）回退密钥 → 空。环境变量压过文件值。

> **两个入口的分工**：主程序「插件管理 → 密钥」只能配 `api_key`（回退用）；
> `base_url` / `model` 等 Provider 配置**只在插件的 Provider 页**维护，
> 不再出现在主程序表单里（曾经放在那里但只有首次运行生效，是个死输入）。

> 插件与设置页**从不回显**密钥明文，只显示「已设置 / 未设置 + 掩码」。

---

## 提示词变量表

模板里用 `{{ ns.key }}` 插值；`{{ ns.key? }}` 表示「可选，缺失渲染为空」；
条件块有正向 `{{# ns.key }} … {{/ ns.key }}`（非空才输出）与反向
`{{^ ns.key }} … {{/ ns.key }}`（为空才输出）两种，可嵌套。

| 命名空间 | 变量 |
|---|---|
| `card` | `name`（必填，始终发送）`description` `personality` `scenario` `first_mes` `mes_example` `creator` `creator_notes` `system_prompt` `post_history_instructions` `tags` `alternate_greetings_count` |
| `card`（选择器控制） | `alternate_greetings` `local_note` `regex_scripts` `st_scripts` |
| `worldbook` | `outline` `excerpt` `count` `total_tokens` `excerpt_tokens` `truncated` |
| `tags` | `library` `taxonomy` `blacklist` `isolated` `usage_top` |
| `limits` | `max_tags` `summary_max_chars` `confidence_min`（必填，由配置与档案合并得出） |
| `model` | `name` `profile_id`（必填） |
| `user` | `instruction`（改写/翻译指令） |
| `text` | `content`（待处理正文） |
| `wi` | `entry_name` `entry_keys` `entry_content` `target_ratio` |

`card` 里除 `name` 与 `alternate_greetings_count` 外的字段都受**内容选择器**控制：
未勾选时渲染为空串（键仍存在，不报 missing）。详见上文「内容选择器」。

规则：

- 模板中出现**未知变量**会在保存档案时被拒绝（`unknown_variable`）。
  条件块标签 `{{# x }}` / `{{^ x }}` / `{{/ x }}` 里的名字同样被检查。
- 运行期必填变量缺失 → 该条目失败（`missing_variable`），不发起 LLM 调用。
- 转义：`{{{{` 输出字面 `{{`，`}}}}` 输出字面 `}}`。
- 空值清理：引用了空值变量、且渲染后只剩标签的行会被整行丢弃（见上文）。
- `tags.*` / `worldbook.*` 的「空」是**空串**，不是「（无）」这类占位文案 ——
  占位文案会被发进提示词，还可能被模型当成真实条目。

---

## 任务规格（task spec，`schema_version: 2`）

**一个「功能」= 一份 task spec**，四件事都在里面声明（设计文档 §8）：

| 维度 | 字段 |
|---|---|
| 怎么问 | `system` / `system_prelude` / `system_suffix` / `user_template` / `user_prelude` / `user_suffix` / `assistant_prefill` |
| 问什么 | `content_selector`（见上文「内容选择器」） |
| 答什么形状 | `output_schema` |
| 答完怎么落地 | `apply_actions` |

因此**新增功能是配置而不是代码**：写一份 spec + 复用现成动作即可。

### `output_schema`：一份声明同时驱动「要求」与「校验」

```jsonc
{
  "json_only": true,
  "strip_think": true,
  "forbidden_wrappers": ["```json", "```"],
  "required": ["summary", "tags"],
  "properties": {
    "summary": {"type": "string", "min_chars": 0, "max_chars": 300},
    "tags":    {"type": "array", "min_items": 0, "max_items": 8,
                "min_confidence": 0.5, "order_by": "confidence",
                "item": {"name": {"type": "string"},
                         "confidence": {"type": "number", "min": 0, "max": 1}}},
    "card_type": {"type": "enum", "values": ["OC", "同人", "工具卡"], "fallback": "keep"}
  }
}
```

支持的类型：`string`（`min_chars`/`max_chars`）、`number`（`min`/`max`）、
`array`（`min_items`/`max_items`/`min_confidence`/`order_by`/`item`）、
`enum`（`values`/`fallback`）、`object`（`item`）。**只做一层对象 + 数组 + 标量**，
不实现完整 JSON Schema（覆盖 95% 场景，且能复用现有的容错解析链）。

> **同源保证**：`api` 把 `describe_constraints()` 生成的文字拼进 system，
> 事后用 `validate()` 逐条核对，两者读同一份声明 —— 不会出现
> 「要求 200 字、校验按 300 判」这种漂移。
>
> **枚举越界恒为错误**（不因 `strict=false` 降级）：字数差一点可以放过，
> 但 `card_type` 返回「宇宙飞船」是值根本不属于该字段，原样写进卡片就是污染。

### `apply_actions`：声明式写回

```jsonc
"apply_actions": [
  {"from": "summary",            "action": "write_card_note"},
  {"from": "tags",               "action": "apply_tags", "params": {"replace": false}},
  {"from": "suggested_new_tags", "action": "queue_suggestions"}
]
```

`from` 指向 `output_schema.properties` 里的字段名（保存时会校验，写错直接报错）。

| action | 做什么 |
|---|---|
| `write_card_note` | 写主程序的本地备注（概览） |
| `apply_tags` | 把标签合并进卡片（黑名单/未知标签由主程序过滤） |
| `write_card_fields` | 改写卡片正文字段（写前自动快照；`params.fields` 白名单） |
| `write_worldbook` | 改写世界书条目（读-改-写 + 修订号乐观并发） |
| `queue_suggestions` | 新标签进建议队列（**不自动建标签**） |
| `move_cards` | 移动到分类目录（可选自动建目录） |

**单动作失败不中断其余**：概览写不进去不该让标签也丢掉。
执行结果逐条回传（`actions[]`），失败项带 `ok=false` 与原因。

`-- 配置里关掉标签写入（apply_tags=false）时，只剔除该动作，其余照常执行。`

## 数据落点

| 数据 | 位置 |
|---|---|
| 概览（本地备注） | `api.cards.write_note()` → `ui_data[<ui_key>].summary`（卡片列表/详情页直接渲染）；写入失败时降级到插件命名空间并提示 |
| 标签 | 经 `api.cards.apply_tags(replace=False)` **合并**写入（黑名单标签会被拦截） |
| 新标签建议 | `ui_data._plugins_v1.stm-ai-assistant.tag_suggestions`（**不自动创建标签**） |
| 分析元数据 | `ui_data` 热缓存（最近 800 条）+ `data/cache/card_meta.jsonl` 全量 |
| 提示词档案 | `data/prompts/<id>.json`（内置档案随代码分发，被修改时落盘覆盖） |
| Provider | `data/providers.json` |
| 用量 | `data/usage/usage.jsonl` + `ui_data` 台账 |

> **插件后端零越界**：不再有任何模块 `import core.*`。概览的读写走
> `api.cards.read_note` / `write_note`（`tests/test_manifest_contract.py::test_no_backend_module_imports_main_app_modules` 锁定）。
> 旧的 `summary_bridge.py` 曾靠内部 import 绕过契约，现已退化为薄封装。

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

> **插件 API 形态（主程序 v0.2+）**：`PluginAPI` 是**域命名空间**，不是扁平方法表。本插件用到的：
>
> | 用途 | 调用 |
> |---|---|
> | 配置 | `api.config.get() / .set()` |
> | 密钥 | `api.secrets.get / .set / .list` |
> | 日志 | `api.log.info / .warn / .error / .debug` |
> | HTTP | `@api.http.route(...)` |
> | 作业 | `api.jobs.register / .submit / .get / .items / .cancel / .retry` |
> | CLI | `api.cli.register(...)` |
> | 私有数据 | `api.private.read / .update / .dir / .query`（`query` 只读） |
> | 卡片 | `api.cards.read / .read_many / .write_fields / .apply_tags / .read_note / .write_note / .list` |
> | 标签 | `api.tags.library()` |
> | 世界书 | `api.worldbook.read / .write` |
> | 快照 | `api.backups.snapshot_card(...)` |
> | token | `api.estimate_tokens(text)` |
>
> 旧的扁平名（`api.read_card` / `api.apply_tags` / `api.ui_data_update` / `api.db` …）
> **已被移除**，主程序不保留别名。裸 SQL 连接 `api.db()` 也取消了——
> 读作业条目用 `api.jobs.items()`，查卡片用 `api.cards.list()`。
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
