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
  // 状态条：常驻容器。品牌图标与徽章槽分开——refreshStatus 只清徽章槽，
  // 否则每次刷新都会把图标一起清掉。
  const statusRow = el('div', 'plugin-stm-ai-assistant-status-row');
  const glyph = el('span', 'plugin-stm-ai-assistant-glyph');
  glyph.setAttribute('aria-hidden', 'true');
  const badgeSlot = el('span', 'plugin-stm-ai-assistant-status-badges');
  statusRow.append(glyph, badgeSlot);

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

  // 内容顺序：状态条 → 横向页签 → 主体（宿主的标题栏在上方）
  const root = el('div', 'plugin-stm-ai-assistant-panel');
  root.append(statusRow, tabBar, body);

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

  /** 把待审批数同步到 `open-review` 入口角标；为 0 时清除。
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
    api.entries.setBadge('open-review', value > 0 ? (value > 99 ? '99+' : value) : null);
  }

  /** 入口打开时按 `ctx.targetId` 落到对应页签（一个 surface 服务 4 个入口）。 */
  function tabForTarget(targetId) {
    const target = String(targetId || '');
    if (target === 'open-review') return 'review';
    if (target === 'assistant-settings') return 'settings';
    return 'workbench';
  }

  return {
    element: root,
    showTab,
    refreshStatus,
    tabForTarget,
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
