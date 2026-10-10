# CHANGELOG

## 0.2.0（未发布）

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

- **`frontend/assets/icon.svg`（插件品牌图标）**：**保留**，并接到面板状态条左侧的
  `.plugin-stm-ai-assistant-glyph`（CSS `mask` 抠形 + `var(--accent-text)` 着色，
  深浅主题自适应）。

  新契约下插件图标的**三条路径能力不同**（实测结论，详见设计文档附录 E.5）：

  | 路径 | 插件自带图标 |
  |---|---|
  | 插件资源 URL（CSS `mask` / `<img>`） | ✅ `assets.py` 路由 + `.svg` 在白名单 |
  | `entries[].icon`（坞 / 命令面板图标槽） | ❌ 只吃宿主图标名 |
  | `manifest.icon`（插件级图标） | ⚠️ 后端下发，前端无渲染者 |

  宿主对新契约下的插件品牌图标没有展示位，因此挂回插件自己的内容区。
  入口图标仍走宿主图标名（`wand` / `tags` / `clipboard` / `settings-gear`）。

  状态条相应拆成「glyph + 徽章槽」两层：`refreshStatus()` 只清徽章槽，
  否则每次刷新都会把品牌图标一起清掉。

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

## 0.1.0（未发布）

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
