/**
 * 选中集合读取 —— **唯一的 Provisional 耦合点**。
 *
 * 主程序目前没有把「当前选中的卡片 id」作为稳定契约暴露给插件：
 * `api.store.viewState.selectedIds` 是 Alpine store 的内部形状，随宿主重构而变。
 * 新契约把这部分责任交给宿主：`entries[].when.selection` 由宿主求值（插件只声明
 * 「需要至少选中一张」），入口的可用/置灰不经过本模块。
 *
 * 但「提交分析作业」终究需要**具体 id 列表**，而 `ctx.targetId` 只给入口 id，
 * `api.entries.list()` 也只给 `enabled` 布尔值。因此这里保留最小读取，并把耦合
 * 全部收进本文件：
 * - 优先用宿主 store 的 `viewState.selectedIds`（Provisional）；
 * - 读不到就返回 `[]`，工作台仍有「手动选择」兜底路径。
 *
 * 宿主一旦提供稳定 API（例如 `api.host.getSelection()`），**只改本文件**
 * 即可完成迁移——其余代码只调用 `readSelectedCardIds(api)`。
 */

/**
 * 读取主程序当前选中的卡片 id。
 *
 * @param {object} api 插件 API 门面。
 * @returns {string[]} 去空、去重的 id 数组；取不到时为空数组。
 */
export function readSelectedCardIds(api) {
  const store = api && api.store;
  const viewState = store && store.viewState;
  const ids = viewState && Array.isArray(viewState.selectedIds) ? viewState.selectedIds : null;
  if (!ids) return [];

  const seen = new Set();
  const result = [];
  ids.forEach((item) => {
    const value = String(item == null ? '' : item).trim();
    if (!value || seen.has(value)) return;
    seen.add(value);
    result.push(value);
  });
  return result;
}
