# CHANGELOG

## 0.1.0（未发布）

首个可用版本（P0 最小垂直切片）。

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
