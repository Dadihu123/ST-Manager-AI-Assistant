# CHANGELOG

## 0.1.0（未发布）

首个可用版本（P0 最小垂直切片）。

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
- **「提示词档案」页签的全屏黑屏**：全屏遮罩原本用裸 `rgba(0, 0, 0, 0.55)`。当面板内
  出现高内容滚动容器时（「提示词档案」一屏正是如此），Chromium 会把半透明黑合成到
  黑色 backdrop root 上，整屏塌成纯黑；切换设备模拟会触发重排所以能暂时恢复，切回该
  页签又复现。现改用主程序 `.modal-overlay` 的同一套做法：语义 `--surface-scrim` +
  `backdrop-filter: blur(5px)`（后者强制建立 backdrop root，让 alpha 正确混到真实
  页面内容上）。
- **提示词档案无法切换当前选中项**：`openProfile()` 只重绘了编辑器，没有重绘列表，
  所以高亮要等下一次切页签才更新。现在 `openProfile()` / `newProfile()` / `load()`
  都会同步重绘列表，并且档案行整行可点即切换编辑对象（按钮区域除外）。
- **只能导出全部档案**：`/profiles/export` 本来就支持 `?profile_id=`，但前端没有把
  参数传下去。现在每个档案行都有「导出」按钮（单档，文件名带 id），顶部保留「导出全部」。
- **档案列表新增「设为当前」**：把某个档案写入主程序 `ui_data.active_profile_id`，
  并在列表里用左侧 3px 强调条标出当前使用的档案（与「正在编辑」的填充态区分）。

### 测试

- 新增 4 个路由回归用例：单档导出、未知档案导出 404、active 档案往返、未知档案 404。
- 新增 3 个样式契约用例：遮罩必须用语义 scrim token + backdrop-filter、选中页签必须
  无位移/无边框/带 2px 底条、页签圆角必须是 `0.55rem`。已用变异测试确认这些断言
  都能捕获旧实现。

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
