/**
 * surface 内容渲染器注册表。
 *
 * 旧版这里是一个「四个页签的长面板」：工作台里塞了 7 个分段（选卡 / 参数 /
 * 预览 / 进度 / 世界书 / 归类 / 提议），其它三个页签平级挂在同一容器里。
 * 于是「入口」与「功能」之间的对应关系只能靠页签猜，右键进来会落在第 1 段顶部。
 *
 * 现在一个 surface 只做一件事（见 docs/2026-10-19-task-centric-redesign.md）：
 *
 *   hub      任务中心（坞入口首屏）
 *   analyze  AI 打标
 *   polish   AI 世界书精简
 *   classify AI 自动归类
 *   propose  AI 设计分类体系
 *   review   结果与审批
 *   profiles 提示词档案
 *   settings 设置
 *
 * 每个 surface 由宿主提供容器（定位、尺寸、焦点陷阱、ESC、遮罩全归宿主），
 * 插件只在容器内画内容；入口通过 `spec.surface` 直接定位到对应面板。
 */

import { clear, closeSelectMenus, destroySelects, el } from './dom.js';
import { createHub } from './views/hub.js';
import { createProfiles } from './views/profiles.js';
import { createReview } from './views/review.js';
import { createSettings } from './views/settings.js';
import { createTaskPanel } from './views/task.js';
import { TASKS, TASK_ORDER } from './tasks.js';

/** surface id → 标题 / 图标 / 尺寸。manifest 的 `action.surface.id` 与这里必须一致。 */
export const SURFACES = {
  hub: { title: 'AI 助手', icon: 'wand', size: 'lg', description: '任务中心' },
  analyze: { title: 'AI 打标', icon: 'tags', size: 'lg', description: '生成概览与标签' },
  polish: { title: 'AI 世界书精简', icon: 'note', size: 'xl', description: '压缩世界书条目' },
  classify: { title: 'AI 自动归类', icon: 'folder', size: 'lg', description: '判断并移动分类' },
  propose: { title: 'AI 设计分类体系', icon: 'sliders-settings', size: 'lg', description: '整库提议' },
  review: { title: '结果与审批', icon: 'check', size: 'xl', description: '逐条确认后落地' },
  profiles: { title: '提示词档案', icon: 'note', size: 'xl', description: '按任务分组管理' },
  settings: { title: 'AI 助手设置', icon: 'settings-gear', size: 'lg', description: 'Provider 与用量' },
};

/**
 * 构造全部 surface 的视图实例（每个 surface 一个，生命周期与插件一致）。
 *
 * @returns {{ get(id): object|null, ids: string[], destroy(): void }}
 */
export function createViews(api, client, context) {
  const views = {};

  views.hub = createHub(api, client, context);
  views.review = createReview(api, client, context);
  views.profiles = createProfiles(api, client, context);
  views.settings = createSettings(api, client, context);

  TASK_ORDER.forEach((taskId) => {
    views[taskId] = createTaskPanel(api, client, context, taskId);
  });

  return {
    get(id) {
      return views[String(id || '')] || null;
    },
    ids() {
      return Object.keys(views);
    },
    destroy() {
      Object.values(views).forEach((view) => {
        closeSelectMenus(view.element);
        destroySelects(view.element);
        if (typeof view.destroy === 'function') view.destroy();
      });
    },
  };
}

/**
 * 把一个视图挂进宿主容器。
 *
 * 返回的函数由宿主在关闭 surface 时调用（D8 契约）：只摘 DOM，
 * 视图实例留在 `createViews` 里复用，因此下次打开不会丢状态、也不会重复建监听。
 */
export function mountView(view) {
  return (container) => {
    if (!container) return () => {};
    const shell = el('div', 'plugin-stm-ai-assistant-view-shell');
    shell.append(view.element);
    container.append(shell);
    if (typeof view.refresh === 'function') {
      Promise.resolve()
        .then(() => view.refresh())
        .catch((error) => console.error('[stm-ai] 视图刷新失败:', error));
    }
    return () => {
      closeSelectMenus(view.element);
      shell.remove();
    };
  };
}

/** 任务 id → surface id（两者同名，但保留映射以便将来改名）。 */
export function surfaceOfTask(taskId) {
  const task = TASKS[String(taskId || '')];
  return task ? task.surface : '';
}

export { clear };
