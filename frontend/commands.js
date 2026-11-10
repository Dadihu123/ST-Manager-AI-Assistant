/**
 * stm-ai-assistant 的命令处理器（v3 `contributions.commands[].module` / `export`）。
 *
 * v2 的 `kind: "command"` 入口由宿主直接打开 surface；v3 把它们升级为**命令**：
 * 菜单项（dock 坞栏、命令面板）用 `action: {"type": "command", "id"}` 引用，
 * 命令面板按 `when` 求值列出，快捷键按下时求值后执行。
 *
 * 处理器形如 `(ctx) => void`，ctx = { api, pluginId, mode, target, selection, trigger }。
 * 这里只负责「打开对应 surface」——surface 的生命周期、视图实例仍由
 * `frontend/plugin.js` 的 activate() 管理。
 */

/** 打开一个已注册的 surface（宿主负责容器/焦点/ESC；这里只请求打开）。 */
function openSurface(ctx, surfaceId, source) {
  const api = ctx && ctx.api;
  if (!api || !api.surfaces) return false;
  api.surfaces.open(surfaceId, { source: source || "command" });
  return true;
}

/** 坞入口 / Ctrl+Shift+A：打开任务中心 hub。 */
export function openWorkbench(ctx) {
  return openSurface(ctx, "hub", "dock");
}

/** 命令面板「AI 结果与审批」。 */
export function openReview(ctx) {
  return openSurface(ctx, "review", "palette");
}

/** 命令面板「AI 设计分类体系」。 */
export function proposeLibrary(ctx) {
  return openSurface(ctx, "propose", "palette");
}

/** 命令面板「AI 提示词档案」。 */
export function openProfiles(ctx) {
  return openSurface(ctx, "profiles", "palette");
}

/**
 * 命令面板「AI 打标历史」。
 *
 * 历史面板原先只能从「结果与审批」里的按钮间接打开 —— 用户得先想到去审批面板，
 * 而落地记录恰恰是「审批之后」才产生的东西。给一条直接入口。
 */
export function openHistory(ctx) {
  return openSurface(ctx, "history", "palette");
}
