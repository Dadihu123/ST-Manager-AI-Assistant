/**
 * stm-ai-assistant 前端入口。
 *
 * 契约：导出 `activate(api)`，可选导出 `deactivate(api)`。
 *
 * 入口**不由插件绘制**：`manifest.frontend.entries` 声明 11 条入口，宿主把它们
 * 渲染到坞 / 批量菜单 / 右键菜单 / 命令面板 / 设置页；容器也由宿主提供
 * （`static/js/pluginSurfaceHost.js`）。
 *
 * 本次重写的要点（见 docs/2026-10-19-task-centric-redesign.md）：
 * - **一个用户能力 = 一个任务 = 一个 surface**，入口只声明「作用域 + surface」；
 * - 作用域通过 `ctx.target` 传语义目标，插件自己解析（不再由宿主预拉全量卡片）；
 * - 作用域是面板局部状态，坞打开时不继承任何上一次的右键范围。
 */

import { createClient } from './api.js';
import { createHeaderStatus } from './headerStatus.js';
import { createViews, mountView, SURFACES } from './panel.js';
import { createPromptPreview } from './promptPreview.js';

/**
 * 入口运行期状态回报器。
 *
 * `PluginHost.loadAll()` 先 `activate()`，之后才 `EntryRegistry.setPlugins()` 建表，
 * 因此激活期间写状态会静默失败。这里记住期望值并在 `entries:changed` 时重放，
 * 且只在值变化时写，保证重放幂等。
 */
function createEntryStatusReporter(api) {
  // 角标与状态点要**同时**写到多个入口：坞（workbench）与命令面板（open-review）。
  // 注意不能写成 `.some()` —— 那只会在第一个成功的入口处短路，后面的入口永远
  // 拿不到角标（`open-review` 从此看不见待审批数）。
  const entryIds = ['workbench', 'open-review'];
  const status = { current: '' };
  const badges = { current: null, has: false };
  let writtenStatus = null;
  let writtenBadge = undefined;

  /** 逐个写入（不能 `.some()` —— 短路会让后面的入口永远拿不到值）。 */
  const writeStatus = (value) => {
    let any = false;
    for (const id of entryIds) {
      if (api.entries.setStatus(id, value)) any = true;
    }
    return any;
  };
  const writeBadge = (value) => {
    let any = false;
    for (const id of entryIds) {
      if (api.entries.setBadge(id, value)) any = true;
    }
    return any;
  };

  /**
   * 任务 → 该任务的入口 id。
   *
   * 用于把「正在跑 3/12」写到**该任务所有入口**上：宿主的批量菜单与右键菜单
   * 都会画进度条（`entry.progress` 形如 `{done, total}`），因此用户在任何
   * 落点都能看到「这个任务已经在跑」。不写的话进度条永远不出现。
   */
  const TASK_ENTRIES = {
    analyze: ['analyze-selected', 'analyze-folder'],
    polish: ['polish-selector', 'polish-folder', 'polish-embedded'],
    classify: ['classify-selected', 'classify-folder'],
    propose: ['propose-library'],
    review: ['open-review'],
  };

  /** 每个任务最近一次进度（`null` = 无进度）。重放时按它恢复。 */
  const taskProgress = new Map();

  const apply = () => {
    if (status.current && writtenStatus !== status.current) {
      if (writeStatus(status.current)) writtenStatus = status.current;
    }
    if (badges.has && writtenBadge !== badges.current) {
      if (writeBadge(badges.current)) writtenBadge = badges.current;
    }
    // 进度同样可能在入口表就绪前写入（自动续跑、页面刚加载），一并重放
    taskProgress.forEach((progress, taskId) => {
      const ids = TASK_ENTRIES[String(taskId)] || [];
      ids.forEach((id) => {
        try {
          api.entries.setProgress(id, progress);
        } catch (error) {
          /* 忽略：重放是尽力而为 */
        }
      });
    });
  };

  apply();
  const off = api.on('entries:changed', apply);

  return {
    setStatus(next) {
      const value = String(next || '');
      if (value === status.current) return;
      status.current = value;
      if (value && writtenStatus !== value) {
        if (writeStatus(value)) writtenStatus = value;
      }
    },
    /** 待审批角标：同时挂在坞入口与「结果与审批」入口上。 */
    setPendingBadge(value) {
      const count = Number(value) || 0;
      badges.current = count > 0 ? (count > 99 ? '99+' : count) : null;
      badges.has = true;
      if (writtenBadge === badges.current) return;
      if (writeBadge(badges.current)) writtenBadge = badges.current;
    },
    /**
     * 把「正在跑」写到该任务的全部入口上（`{done, total}` / `null` 清除）。
     *
     * 宿主的批量菜单与右键菜单都渲染 `entry.progress`，因此这里一次写入
     * 就能让所有落点显示同一条进度，不需要每个面板各画一套。
     */
    setTaskProgress(taskId, progress) {
      const ids = TASK_ENTRIES[String(taskId || '')] || [];
      const next = progress && Number(progress.total) > 0
        ? { done: Number(progress.done) || 0, total: Number(progress.total) || 0 }
        : null;
      taskProgress.set(String(taskId), next);
      for (const id of ids) {
        try {
          api.entries.setProgress(id, next);
        } catch (error) {
          console.warn(`[stm-ai] 写入入口进度失败（${id}）:`, error);
        }
      }
    },
    dispose() {
      if (typeof off === 'function') off();
    },
  };
}

export async function activate(api) {
  const client = createClient(api);

  /**
   * 跨 surface 的**运行参数**（provider / model / 档案选择）。
   *
   * 注意这里**没有** `selectedCardIds` / `openTarget`：作用域是各任务面板的
   * 局部状态，任何入口都不会把它写到这里（旧版正是因此出现「右键 12 张残留到
   * 下次坞打开」）。
   */
  const context = {
    providerId: '',
    profileId: '',
    model: '',
    /** 由本函数注入：打开某个任务面板，可带作用域。 */
    openTask: null,
    /** 由本函数注入：打开审批面板，可定位到某个 runId。 */
    openReview: null,
    /** 由本函数注入：打开设置面板。 */
    openSettings: null,
    /** 由本函数注入：Prompt 预览。 */
    openPromptPreview: null,
    /** 由本函数注入：待审批角标写入器。 */
    setPendingBadge: null,
    /** 由本函数注入：把「正在跑 done/total」写到该任务的全部入口上。 */
    setTaskProgress: null,
    /** 由 panel 注入：审批状态变化后刷新 hub 与角标。 */
    notifyReview: null,
  };

  const reporter = createEntryStatusReporter(api);
  context.setPendingBadge = (value) => reporter.setPendingBadge(value);
  context.setTaskProgress = (taskId, progress) => reporter.setTaskProgress(taskId, progress);

  const views = createViews(api, client, context);
  const preview = createPromptPreview(api);
  // 状态胶囊只挂在任务中心（hub）标题行：其余面板不重复拉 status。
  const headerStatus = createHeaderStatus(api);

  // ---------------------------------------------------------- surface 注册

  const surfaceIds = Object.keys(SURFACES);
  surfaceIds.forEach((surfaceId) => {
    const spec = SURFACES[surfaceId];
    const view = views.get(surfaceId);
    if (!view) return;
    api.surfaces.register(surfaceId, {
      title: spec.title,
      description: spec.description,
      // 坞入口与弹层标题行统一用插件自己的品牌图标（同一插件到处同一个图标）。
      icon: 'frontend/assets/icon.svg',
      type: 'modal',
      size: spec.size,
      // 状态胶囊只挂在 hub：宿主在标题行给了一个空容器，这里把胶囊槽搬进去，
      // 于是「图标 + 标题 + 胶囊 + 关闭」在同一行。
      headerExtra(container) {
        if (surfaceId === 'hub') headerStatus.mount(container);
      },
      mount: mountView(view),
      /**
       * 应用本次入口的作用域。
       *
       * 宿主在 `mount` 之后必定调用一次 `onOpen`；surface 已打开时再次从别的
       * 入口进入也走这里（`pluginSurfaceHost.js` 的 `openSurface` 复用容器并
       * 重新投递 ctx），因此只有一条路径，不会出现两套逻辑分叉。
       */
      onOpen(openCtx) {
        if (view && typeof view.applyContext === 'function') view.applyContext(openCtx);
        if (surfaceId === 'hub') refreshBadge();
      },
    });
  });

  // Prompt 预览（次级内容，独立 modal）
  api.surfaces.register('prompt-preview', {
    title: 'Prompt 预览',
    type: 'modal',
    mount(container) {
      return preview.mount(container);
    },
  });

  // ---------------------------------------------------------- 导航注入

  context.openPromptPreview = (messages) => {
    preview.setMessages(messages);
    api.surfaces.open('prompt-preview', { source: 'task' });
  };

  context.openTask = (taskId, target) => {
    const surfaceId = SURFACES[taskId] ? taskId : 'hub';
    api.surfaces.open(surfaceId, {
      source: target ? 'context' : 'hub',
      task: taskId,
      target: target || null,
    });
    const view = views.get(surfaceId);
    if (view && typeof view.applyContext === 'function') {
      // surface 首次打开时宿主会调 onOpen；已打开时 openSurface 也会重投递，
      // 但 `api.surfaces.open()` 走的是同一个入口，这里不必重复调用。
    }
  };

  context.openReview = (runId) => {
    const view = views.get('review');
    if (view && typeof view.focusRun === 'function') view.focusRun(runId);
    api.surfaces.open('review', { source: 'hub', runId: runId || '' });
  };

  context.openSettings = () => {
    api.surfaces.open('settings', { source: 'hub' });
  };

  context.notifyReview = () => {
    const review = views.get('review');
    if (review && typeof review.refresh === 'function' && api.surfaces.isOpen('review')) {
      review.refresh();
    }
    const hub = views.get('hub');
    if (hub && typeof hub.refresh === 'function' && api.surfaces.isOpen('hub')) {
      hub.refresh();
    }
    refreshBadge();
  };

  // ---------------------------------------------------------- 角标与状态

  async function refreshBadge() {
    try {
      const data = await client.status();
      const pending = data.pending_reviews && typeof data.pending_reviews === 'object'
        ? Object.keys(data.pending_reviews).length
        : 0;
      context.setPendingBadge(pending);
      const providers = (data.providers && data.providers.count) || 0;
      const config = data.config || {};
      reporter.setStatus(providers ? 'ok' : 'warn');
      headerStatus.render({
        pending,
        providers,
        applyMode: String(config.apply_mode || 'review'),
      });
    } catch (error) {
      console.warn('[stm-ai] 读取状态失败:', error);
      headerStatus.error(error.message);
    }
  }

  // 卡片/世界书被外部改动后刷新已打开的面板（避免显示过期结果）
  const offCardUpdated = api.on('card.updated', () => {
    if (api.surfaces.isOpen('hub')) views.get('hub').refresh();
  });
  const offWorldbookSaved = api.on('worldbook.saved', () => {
    if (api.surfaces.isOpen('polish')) views.get('polish').refresh();
  });

  const offConfigChange = typeof api.onConfigChange === 'function'
    ? api.onConfigChange(() => {
      refreshBadge();
      if (api.surfaces.isOpen('settings')) views.get('settings').refresh();
    })
    : null;

  // ---------------------------------------------------------- 启动自检

  const runSelfCheck = async () => {
    try {
      const data = await client.health();
      const checks = data.checks || {};
      const providerReady = checks.provider && checks.provider.ok;
      const keyReady = checks.api_key && checks.api_key.ok;
      const bridgeReady = checks.summary_bridge && checks.summary_bridge.ok;

      if (!providerReady) {
        api.host.toast('尚未配置 Provider，请到「AI 助手 → 设置」完成配置', 5000, 'settings-help-entry');
      } else if (!keyReady) {
        api.host.toast('Provider 缺少 API Key，请到「AI 助手 → 设置」填写', 5000, 'settings-help-entry');
      }
      if (!bridgeReady) {
        api.host.toast('概览写入通道不可用，结果将只保存在插件内', 5000, 'settings-help-entry');
      }
      reporter.setStatus(!providerReady || !keyReady || !bridgeReady ? 'warn' : 'ok');
    } catch (error) {
      reporter.setStatus('error');
      api.host.toast(`插件自检失败：${error.message}`, 5000, 'close');
    }
  };

  await Promise.all([runSelfCheck(), refreshBadge()]);

  api.log?.('info', 'stm-ai-assistant 前端已激活');

  // ---------------------------------------------------------- 清理（D9.1）

  return () => {
    if (typeof offCardUpdated === 'function') offCardUpdated();
    if (typeof offWorldbookSaved === 'function') offWorldbookSaved();
    if (typeof offConfigChange === 'function') offConfigChange();
    reporter.dispose();
    surfaceIds.forEach((surfaceId) => api.surfaces.close(surfaceId));
    api.surfaces.close('prompt-preview');
    headerStatus.detach();
    views.destroy();
  };
}

export function deactivate(api) {
  api.log?.('info', 'stm-ai-assistant 前端已卸载');
}
