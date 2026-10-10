/**
 * 面板骨架：浮动入口按钮 + 自绘 overlay + 四个页签。
 *
 * 说明（设计文档 C4）：主程序 manifest 的 `frontend.panels` / `settings_tab` /
 * `detail_tab` 目前**没有前端消费者**，`registerModal()` / `registerSettingsSection()`
 * 也只写入内存 Map 而无人渲染。因此插件 UI 自建：
 * - 入口：`api.createRoot()` 挂载点内的浮动按钮（`position: fixed`）；
 * - 面板：自带遮罩与尺寸的 overlay（不依赖主程序的 `ui-modal-*` 骨架）。
 */

import { badge, button, clear, closeSelectMenus, destroySelects, el, icon } from './dom.js';
import { createProfiles } from './views/profiles.js';
import { createReview } from './views/review.js';
import { createSettings } from './views/settings.js';
import { createWorkbench } from './views/workbench.js';

const TABS = [
  { id: 'workbench', label: '工作台', icon: 'sliders-settings' },
  { id: 'review', label: '结果与审批', icon: 'tags' },
  { id: 'profiles', label: '提示词档案', icon: 'note' },
  { id: 'settings', label: '设置', icon: 'settings-gear' },
];

export function createPanel(api, client, context) {
  const overlay = el('div', 'plugin-stm-ai-assistant-overlay');
  const shell = el('div', 'plugin-stm-ai-assistant-shell');

  // 顶栏：图标块 + 标题/副标 + 右侧状态徽章簇 + ghost 关闭钮（重构版）
  const header = el('header', 'plugin-stm-ai-assistant-header');
  const title = el('div', 'plugin-stm-ai-assistant-title');
  const glyph = el('span', 'plugin-stm-ai-assistant-glyph');
  glyph.setAttribute('aria-hidden', 'true');
  const titleText = el('div', 'plugin-stm-ai-assistant-title-block');
  titleText.append(
    el('span', 'plugin-stm-ai-assistant-title-text', 'AI 助手'),
    el('span', 'plugin-stm-ai-assistant-meta-caption', '角色卡批量分析'),
  );
  title.append(glyph, titleText);

  // 常驻容器：refreshStatus 每次只刷新它，不再删除 badge 节点本身
  const statusRow = el('span', 'plugin-stm-ai-assistant-status-row');
  statusRow.append(badge('未连接', 'is-muted'));

  const closeBtn = button('✕', 'is-ghost', () => close());
  closeBtn.title = '关闭';
  closeBtn.setAttribute('aria-label', '关闭');

  const headerRight = el('div', 'plugin-stm-ai-assistant-header-right');
  headerRight.append(statusRow, closeBtn);
  header.append(title, headerRight);

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

  TABS.forEach((tab) => {
    const node = el('button', 'plugin-stm-ai-assistant-tab');
    node.type = 'button';
    node.dataset.marqueeIgnore = 'true';
    node.setAttribute('role', 'tab');
    node.append(icon(tab.icon, 'ui-icon--xs plugin-stm-ai-assistant-tab-icon'));
    const label = el('span', 'plugin-stm-ai-assistant-tab-label', tab.label);
    node.append(label);
    // 数字角标容器（结果与审批等待审批数等），默认隐藏
    const count = el('span', 'plugin-stm-ai-assistant-tab-count');
    count.hidden = true;
    node.append(count);
    node.addEventListener('click', () => showTab(tab.id));
    tabButtons[tab.id] = { node, count };
    tabBar.append(node);
  });

  // 详情页骨架：header → 横向 tabBar → body（对齐 modal-detail.css 的 .detail-tabs 横排）
  shell.append(header, tabBar, body);
  overlay.append(shell);

  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener('keydown', onKeydown);

  function onKeydown(event) {
    if (event.key === 'Escape' && overlay.classList.contains('is-open')) close();
  }

  function cleanupView(view, { destroy = false } = {}) {
    if (!view) return;
    closeSelectMenus(view.element);
    if (destroy) {
      destroySelects(view.element);
      if (typeof view.destroy === 'function') view.destroy();
    }
  }

  function showTab(tabId) {
    const previousView = views[activeTab];
    if (previousView && previousView !== views[tabId]) cleanupView(previousView);
    activeTab = tabId;
    clear(body);
    Object.entries(tabButtons).forEach(([id, entry]) => {
      const on = id === tabId;
      entry.node.classList.toggle('is-active', on);
      entry.node.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    const view = views[tabId];
    body.classList.toggle('is-profiles', tabId === 'profiles');
    Object.values(views).forEach((candidate) => {
      if (candidate && candidate.element) candidate.element.classList.toggle('is-profiles', candidate === view);
    });
    if (view) body.append(view.element);
    if (view && typeof view.refresh === 'function') view.refresh();
    refreshStatus();
  }

  async function refreshStatus() {
    try {
      const data = await client.status();
      const providers = data.providers || {};
      const config = data.config || {};
      clear(statusRow);
      const pending = data.suggestions || 0;
      statusRow.append(
        badge(`档案 ${(data.profiles && data.profiles.count) || 0}`),
        badge(`Provider ${providers.count || 0}`),
        badge(`写回 ${config.apply_mode === 'auto' ? '直接写入' : '逐条审批'}`),
        badge(`待审批 ${pending}`, pending ? 'is-warn' : 'is-muted'),
      );
      // 同步「结果与审批」页签上的待审批角标
      const reviewEntry = tabButtons.review;
      if (reviewEntry) {
        reviewEntry.count.textContent = pending > 99 ? '99+' : String(pending);
        reviewEntry.count.hidden = !pending;
      }
    } catch (error) {
      clear(statusRow);
      statusRow.append(badge(`状态读取失败：${error.message}`, 'is-error'));
    }
  }

  function open() {
    overlay.classList.add('is-open');
    showTab(activeTab);
  }

  function close() {
    cleanupView(views[activeTab]);
    overlay.classList.remove('is-open');
  }

  function destroy() {
    document.removeEventListener('keydown', onKeydown);
    Object.values(views).forEach((view) => cleanupView(view, { destroy: true }));
  }

  return { element: overlay, open, close, destroy, showTab };
}
