/**
 * surface 内容渲染器：状态条 + 四个页签 + 主体。
 *
 * 契约变化（本次重写的核心）：
 * 旧版 `createPanel` 返回 `{ element: overlay, open, close, ... }`，插件自绘
 * `position: fixed` 遮罩、自己绑 ESC、自己处理点击遮罩关闭、自己管焦点。
 * 现在容器由宿主提供（`pluginSurfaceHost.js`）：定位、尺寸、滚动、`role="dialog"`、
 * `aria-modal`、焦点陷阱、ESC 关闭、遮罩合成全归宿主，插件**只在容器内画内容**。
 *
 * 因此这里：
 * - 不再创建 overlay / shell / 顶栏 / 关闭按钮（标题与关闭钮是宿主 DOM）；
 * - 不再 `document.addEventListener('keydown', ...)`，不再监听遮罩点击；
 * - 不再调用 `api.setFullscreenScrim`（宿主自己管遮罩合成）。
 *
 * 页签切换（`showTab` / `cleanupView` / `refreshStatus`）是插件自己的信息架构，
 * 原样保留。待审批角标改走 `api.entries.setBadge()`，交给宿主渲染。
 */

import { clear, closeSelectMenus, destroySelects, el } from './dom.js';
import { createProfiles } from './views/profiles.js';
import { createReview } from './views/review.js';
import { createSettings } from './views/settings.js';
import { createWorkbench } from './views/workbench.js';

export const TABS = [
  { id: 'workbench', label: '工作台', icon: 'sliders-settings' },
  { id: 'review', label: '结果与审批', icon: 'tags' },
  { id: 'profiles', label: '提示词档案', icon: 'note' },
  { id: 'settings', label: '设置', icon: 'settings-gear' },
];

/** `is-*` 旧色调 → 宿主 `api.ui.badge` 的 tone（设计文档第 3 节映射表）。 */
const TONE_BY_LEGACY = {
  'is-ok': 'success',
  'is-warn': 'warning',
  'is-error': 'danger',
  'is-muted': 'neutral',
};

function toneOf(legacy) {
  return TONE_BY_LEGACY[String(legacy || '')] || 'neutral';
}

export function createPanelBody(api, client, context) {
  // 状态胶囊槽：**由宿主标题行承载**（`spec.headerExtra`）。
  //
  // 旧版插件自绘「品牌图标 + 徽章」第二条状态栏，与宿主标题行叠在一起 →
  // 弹层顶部出现两行，视觉上比宿主自己的弹窗「多一层」。
  // 现在图标与标题归宿主标题行（`.plugin-surface__header`），插件只把状态胶囊
  // 塞进宿主的附加槽，两者同行。
  const badgeSlot = el('span', 'plugin-stm-ai-assistant-status-badges');

  const tabBar = el('nav', 'plugin-stm-ai-assistant-tabs');
  tabBar.setAttribute('role', 'tablist');
  const body = el('div', 'plugin-stm-ai-assistant-body');

  const views = {
    workbench: createWorkbench(api, client, context),
    review: createReview(api, client, context),
    profiles: createProfiles(api, client, context),
    settings: createSettings(api, client, context),
  };

  context.notifyReview = () => {
    views.review.refresh();
    refreshStatus();
  };

  let activeTab = 'workbench';
  const tabButtons = {};

  const badge = (value, legacyTone) => api.ui.badge({ value, tone: toneOf(legacyTone) });

  TABS.forEach((tab) => {
    const node = el('button', 'plugin-stm-ai-assistant-tab');
    node.type = 'button';
    node.dataset.marqueeIgnore = 'true';
    node.setAttribute('role', 'tab');
    node.append(api.ui.icon(tab.icon, { className: 'ui-icon--xs plugin-stm-ai-assistant-tab-icon' }));
    node.append(el('span', 'plugin-stm-ai-assistant-tab-label', tab.label));
    node.addEventListener('click', () => showTab(tab.id));
    tabButtons[tab.id] = node;
    tabBar.append(node);
  });

  // 内容顺序：页签 → 主体（标题行与状态胶囊归宿主容器）
  const root = el('div', 'plugin-stm-ai-assistant-panel');
  root.append(tabBar, body);

  function cleanupView(view, { destroy = false } = {}) {
    if (!view) return;
    closeSelectMenus(view.element);
    if (destroy) {
      destroySelects(view.element);
      if (typeof view.destroy === 'function') view.destroy();
    }
  }

  function showTab(tabId) {
    if (!views[tabId]) tabId = 'workbench';
    const previousView = views[activeTab];
    if (previousView && previousView !== views[tabId]) cleanupView(previousView);
    activeTab = tabId;
    clear(body);
    Object.entries(tabButtons).forEach(([id, node]) => {
      const on = id === tabId;
      node.classList.toggle('is-active', on);
      node.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    const view = views[tabId];
    body.append(view.element);
    if (typeof view.refresh === 'function') view.refresh();
    refreshStatus();
  }

  async function refreshStatus() {
    try {
      const data = await client.status();
      const providers = data.providers || {};
      const config = data.config || {};
      const pending = data.suggestions || 0;
      clear(badgeSlot);
      badgeSlot.append(
        badge(`档案 ${(data.profiles && data.profiles.count) || 0}`),
        badge(`Provider ${providers.count || 0}`),
        badge(`写回 ${config.apply_mode === 'auto' ? '直接写入' : '逐条审批'}`),
        badge(`待审批 ${pending}`, pending ? 'is-warn' : 'is-muted'),
      );
      // 待审批数走宿主入口角标（坞 / 命令面板），不再自绘页签数字
      syncPendingBadge(pending);
    } catch (error) {
      clear(badgeSlot);
      badgeSlot.append(badge(`状态读取失败：${error.message}`, 'is-error'));
    }
  }

  /** 把待审批数同步到 `workbench` 入口角标；为 0 时清除。
   *
   * 交给 `context.setPendingBadge`（由 `plugin.js` 注入）而不是直接调
   * `api.entries.setBadge`：注册表可能还没建表（见 `plugin.js` 的
   * `createEntryStatusReporter` 注释），那个写入器会负责重放。
   * 独立存在时（例如被单测直接构造）退化为直接调用。
   */
  function syncPendingBadge(pending) {
    const value = Number(pending) || 0;
    if (typeof context.setPendingBadge === 'function') {
      context.setPendingBadge(value);
      return;
    }
    api.entries.setBadge('workbench', value > 0 ? (value > 99 ? '99+' : value) : null);
  }

  /** 入口打开时按 `ctx.targetId` 落到对应页签（一个 surface 服务多个入口）。 */
  function tabForTarget(targetId) {
    const target = String(targetId || '');
    if (target === 'assistant-settings') return 'settings';
    return 'workbench';
  }

  /**
   * 应用入口打开时的宿主上下文（`mount(container, ctx)` 的 ctx）。
   *
   * 宿主把「这次点击的作用域」放在 `ctx.targetIds` 里交给插件：
   * - 右键分类 → 该分类（含子分类）下的全部卡片 id；
   * - 批量菜单 → 当前选中的卡片 id；
   * - 坞 / 快捷键 → 空数组（沿用主程序当前选中集）。
   *
   * 插件据此**不必**再去读宿主的 `viewState.selectedIds`（那是 Provisional 耦合，
   * 而且右键分类的作用域与全局选中集根本不是一回事）。
   */
  function applyEntryContext(ctx) {
    const source = String((ctx && ctx.source) || '');
    const targetIds = Array.isArray(ctx && ctx.targetIds) ? ctx.targetIds.map(String) : [];
    // 作用域只看**来源**，不看 id 数量：右键一个空分类也是「分类作用域」，
    // 此时正确行为是如实显示「该分类下没有卡片」，而不是悄悄退回全局选中集
    // （那会让用户以为要分析的是自己勾选的那几张，与右键意图不符）。
    const scoped = source === 'context' || source === 'batch';

    context.openTarget = scoped
      ? {
        kind: String((ctx && ctx.targetKind) || ''),
        path: String((ctx && ctx.targetPath) || ''),
        count: targetIds.length,
      }
      : null;

    // 只有带作用域的入口才覆盖选中集；坞 / 快捷键打开时保留用户已有选择。
    if (scoped) context.selectedCardIds = targetIds;

    showTab(tabForTarget(ctx && ctx.targetId));
  }
  return {
    element: root,
    showTab,
    applyEntryContext,
    refreshStatus,
    tabForTarget,
    /**
     * 把状态胶囊挂进宿主的标题行（`spec.headerExtra` 的回调目标）。
     *
     * 宿主在标题行给了一个空容器，这里把胶囊槽搬进去——**同一行**显示
     * 「图标 + 标题 + 状态胶囊 + 关闭」，与主程序自己的弹窗观感一致。
     * 状态刷新只清空胶囊槽，图标与标题是宿主 DOM，不受影响。
     */
    mountHeaderExtra(container) {
      if (container && badgeSlot.parentNode !== container) container.append(badgeSlot);
    },
    /** 宿主关闭 surface 时调用：只摘 DOM，视图实例留待下次 mount 复用。 */
    detach() {
      cleanupView(views[activeTab]);
      root.remove();
    },
    get activeTab() {
      return activeTab;
    },
    destroy() {
      Object.values(views).forEach((view) => cleanupView(view, { destroy: true }));
    },
  };
}

/**
 * 「Prompt 预览」次级 surface（`type: 'modal'`）。
 *
 * 存在意义有二：把结构上属于「次级内容」的东西从页签里搬出来，以及让插件真实
 * 使用四种容器类型中的第二种——宿主的 modal 容器自带遮罩、居中、焦点陷阱。
 */
export function createPromptPreview(api) {
  let root = null;
  let blocks = [];

  function render() {
    if (!root) return;
    clear(root);
    if (!blocks.length) {
      root.append(api.ui.empty({
        icon: 'file',
        title: '还没有可预览的 Prompt',
        description: '请先在工作台点击「预览 Prompt」。',
      }));
      return;
    }

    blocks.forEach((message) => {
      const block = el('div', 'plugin-stm-ai-assistant-message');
      block.append(el('div', 'plugin-stm-ai-assistant-message-role', message.role));
      block.append(el('pre', 'plugin-stm-ai-assistant-message-body', message.content));
      root.append(block);
    });
  }

  return {
    mount(container) {
      root = el('div', 'plugin-stm-ai-assistant-view');
      container.append(root);
      render();
      // 宿主关闭时调用：摘掉自己的节点，并清空引用（下次 mount 重新创建）
      return () => {
        root.remove();
        root = null;
      };
    },
    /** 由工作台 / 档案页在拿到预览结果后调用（调用方随后打开该 surface）。 */
    setMessages(messages) {
      blocks = Array.isArray(messages) ? messages : [];
      render();
    },
  };
}
