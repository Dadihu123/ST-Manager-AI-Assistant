/**
 * stm-ai-assistant 前端入口。
 *
 * 契约：导出 `activate(api)`，可选导出 `deactivate(api)`（见 `static/js/pluginHost.js:165-178`）。
 * 容器由 `api.createRoot()` 生成，自动带 `data-plugin="stm-ai-assistant"`（F12），
 * 与 `frontend/plugin.css` 的作用域前缀配套（F11）。
 */

import { createClient } from './api.js';
import { el } from './dom.js';
import { createPanel } from './panel.js';

export async function activate(api) {
  const client = createClient(api);

  // 跨视图共享的运行上下文（选中的卡片、当前作业、参数）
  const context = {
    selectedCardIds: [],
    runId: '',
    providerId: '',
    profileId: '',
    model: '',
    mode: 'both',
    applyMode: 'review',
    dryRun: false,
    force: false,
    notifyReview: null,
  };

  const root = api.createRoot('div');
  root.classList.add('plugin-stm-ai-assistant-root');

  const launcher = el('button', 'plugin-stm-ai-assistant-launcher', 'AI 助手');
  launcher.type = 'button';
  launcher.dataset.marqueeIgnore = 'true';

  const panel = createPanel(api, client, context);
  root.append(launcher, panel.element);

  launcher.addEventListener('click', () => panel.open());

  // 主程序勾选变化时同步一次（`viewState.selectedIds` 是 Provisional 耦合点）
  const syncSelection = () => {
    const store = api.store;
    const viewState = store && store.viewState;
    const ids = viewState && Array.isArray(viewState.selectedIds) ? viewState.selectedIds : null;
    if (ids) context.selectedCardIds = ids.map((item) => String(item || '')).filter(Boolean);
  };
  syncSelection();

  // 卡片被更新后刷新状态（避免显示过期信息）
  const offCardUpdated = api.on('card.updated', () => {
    panel.showTab('workbench');
  });

  try {
    await client.health().then((data) => {
      const checks = data.checks || {};
      if (!(checks.provider && checks.provider.ok)) {
        api.toast('尚未配置 Provider，请到「AI 助手 → 设置」完成配置', 5000, 'settings-help-entry');
      } else if (!(checks.api_key && checks.api_key.ok)) {
        api.toast('Provider 缺少 API Key，请到「AI 助手 → 设置」填写', 5000, 'settings-help-entry');
      }
      if (checks.summary_bridge && !checks.summary_bridge.ok) {
        api.toast('概览写入通道不可用，结果将只保存在插件内', 5000, 'settings-help-entry');
      }
    });
  } catch (error) {
    api.toast(`插件自检失败：${error.message}`, 5000, 'close');
  }

  api.log?.('info', 'stm-ai-assistant 前端已激活');

  return () => {
    if (typeof offCardUpdated === 'function') offCardUpdated();
    panel.destroy();
    root.remove();
  };
}

export function deactivate(api) {
  api.log?.('info', 'stm-ai-assistant 前端已卸载');
}
