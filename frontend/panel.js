/**
 * 面板骨架：浮动入口按钮 + 自绘 overlay + 四个页签。
 *
 * 说明（设计文档 C4）：主程序 manifest 的 `frontend.panels` / `settings_tab` /
 * `detail_tab` 目前**没有前端消费者**，`registerModal()` / `registerSettingsSection()`
 * 也只写入内存 Map 而无人渲染。因此插件 UI 自建：
 * - 入口：`api.createRoot()` 挂载点内的浮动按钮（`position: fixed`）；
 * - 面板：自带遮罩与尺寸的 overlay（不依赖主程序的 `ui-modal-*` 骨架）。
 */

import { badge, button, clear, el } from '../dom.js';
import { createProfiles } from '../views/profiles.js';
import { createReview } from '../views/review.js';
import { createSettings } from '../views/settings.js';
import { createWorkbench } from '../views/workbench.js';

const TABS = [
  { id: 'workbench', label: '工作台' },
  { id: 'review', label: '结果与审批' },
  { id: 'profiles', label: '提示词档案' },
  { id: 'settings', label: '设置' },
];

export function createPanel(api, client, context) {
  const overlay = el('div', 'plugin-stm-ai-assistant-overlay');
  const shell = el('div', 'plugin-stm-ai-assistant-shell');

  const header = el('header', 'plugin-stm-ai-assistant-header');
  const title = el('div', 'plugin-stm-ai-assistant-title');
  title.append(el('span', 'plugin-stm-ai-assistant-title-text', 'AI 助手'));
  const statusBadge = badge('未连接', 'is-muted');
  title.append(statusBadge);

  const closeBtn = button('关闭', 'is-secondary', () => close());
  header.append(title, closeBtn);

  const tabBar = el('nav', 'plugin-stm-ai-assistant-tabs');
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
    const node = button(tab.label, 'plugin-stm-ai-assistant-tab', () => showTab(tab.id));
    tabButtons[tab.id] = node;
    tabBar.append(node);
  });

  shell.append(header, tabBar, body);
  overlay.append(shell);

  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener('keydown', onKeydown);

  function onKeydown(event) {
    if (event.key === 'Escape' && overlay.classList.contains('is-open')) close();
  }

  function showTab(tabId) {
    activeTab = tabId;
    clear(body);
    Object.entries(tabButtons).forEach(([id, node]) => {
      node.classList.toggle('is-active', id === tabId);
    });
    const view = views[tabId];
    if (view) body.append(view.element);
    if (view && typeof view.refresh === 'function') view.refresh();
    refreshStatus();
  }

  async function refreshStatus() {
    try {
      const data = await client.status();
      const providers = data.providers || {};
      const config = data.config || {};
      clear(statusBadge.parentNode);
      statusBadge.parentNode.append(
        badge(`档案 ${(data.profiles && data.profiles.count) || 0}`),
        badge(`Provider ${providers.count || 0}`),
        badge(`写回 ${config.apply_mode === 'auto' ? '直接写入' : '逐条审批'}`),
        badge(`待审批标签 ${data.suggestions || 0}`, data.suggestions ? 'is-warn' : 'is-muted'),
      );
    } catch (error) {
      clear(statusBadge.parentNode);
      statusBadge.parentNode.append(badge(`状态读取失败：${error.message}`, 'is-error'));
    }
  }

  function open() {
    overlay.classList.add('is-open');
    showTab(activeTab);
  }

  function close() {
    overlay.classList.remove('is-open');
  }

  function destroy() {
    document.removeEventListener('keydown', onKeydown);
    Object.values(views).forEach((view) => {
      if (view && typeof view.destroy === 'function') view.destroy();
    });
  }

  return { element: overlay, open, close, destroy, showTab };
}
