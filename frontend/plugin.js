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

const POSITION_KEY = 'stm-ai-assistant.launcher-pos';
const DRAG_THRESHOLD = 6; // 超过该位移才算拖拽（避免点击被误判）

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

  // ---------------------------------------------------------- 悬浮入口（图标 + 可拖拽）

  const launcher = el('button', 'plugin-stm-ai-assistant-launcher');
  launcher.type = 'button';
  launcher.dataset.marqueeIgnore = 'true';
  launcher.title = 'AI 助手';
  launcher.setAttribute('aria-label', 'AI 助手');

  // 用 <span> + CSS mask 渲染图标：mask 只取 SVG 的 alpha 形状，
  // 颜色由 background: var(--action-icon) 决定 → 深浅主题都跟随主程序图标色。
  // （不能用 <img>：外部 SVG 的 currentColor 解析不到页面 CSS 变量，永远是黑色剪影。）
  const icon = el('span', 'plugin-stm-ai-assistant-launcher-icon');
  icon.setAttribute('role', 'img');
  icon.setAttribute('aria-hidden', 'true');
  launcher.append(icon);

  const panel = createPanel(api, client, context);
  root.append(launcher, panel.element);

  // ---- 位置：localStorage 记忆，默认右下（往上抬避开翻页栏）----

  const applyPos = (x, y) => {
    launcher.style.left = `${x}px`;
    launcher.style.top = `${y}px`;
    launcher.style.right = 'auto';
    launcher.style.bottom = 'auto';
  };

  const defaultPos = () => ({
    x: window.innerWidth - launcher.offsetWidth - 20,
    // 抬高到翻页工具栏上方（底部工具栏约 64px，留 20px 间距）
    y: window.innerHeight - launcher.offsetHeight - 96,
  });

  const clampPos = (x, y) => ({
    x: Math.min(Math.max(0, x), Math.max(0, window.innerWidth - launcher.offsetWidth)),
    y: Math.min(Math.max(0, y), Math.max(0, window.innerHeight - launcher.offsetHeight)),
  });

  const restorePos = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(POSITION_KEY) || 'null');
      if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
        const { x, y } = clampPos(saved.x, saved.y);
        applyPos(x, y);
        return;
      }
    } catch (_) { /* 忽略坏数据 */ }
    const pos = defaultPos();
    applyPos(pos.x, pos.y);
  };

  const savePos = () => {
    try {
      localStorage.setItem(POSITION_KEY, JSON.stringify({
        x: launcher.offsetLeft,
        y: launcher.offsetTop,
      }));
    } catch (_) { /* localStorage 不可用时静默 */ }
  };

  // ---- 拖拽：pointer 事件统一鼠标/触摸，位移超阈值才进入拖拽 ----

  let dragging = false;
  let moved = false;
  let startX = 0;
  let startY = 0;
  let baseX = 0;
  let baseY = 0;

  launcher.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    dragging = true;
    moved = false;
    startX = event.clientX;
    startY = event.clientY;
    baseX = launcher.offsetLeft;
    baseY = launcher.offsetTop;
    launcher.setPointerCapture(event.pointerId);
  });

  launcher.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    const dx = event.clientX - startX;
    const dy = event.clientY - startY;
    if (!moved && Math.hypot(dx, dy) > DRAG_THRESHOLD) {
      moved = true;
      launcher.classList.add('is-dragging');
    }
    if (moved) {
      const { x, y } = clampPos(baseX + dx, baseY + dy);
      applyPos(x, y);
    }
  });

  const endDrag = (event) => {
    if (!dragging) return;
    dragging = false;
    launcher.classList.remove('is-dragging');
    try { launcher.releasePointerCapture(event.pointerId); } catch (_) {}
    if (moved) {
      savePos();
      // 拖拽后的这次 pointerup 不应触发 click
      event.preventDefault();
      event.stopPropagation();
    }
  };

  launcher.addEventListener('pointerup', endDrag);
  launcher.addEventListener('pointercancel', endDrag);

  // 只有「没有拖动」的点击才打开面板
  launcher.addEventListener('click', (event) => {
    if (moved) {
      event.preventDefault();
      event.stopPropagation();
      moved = false;
      return;
    }
    panel.open();
  });

  // 尺寸就绪后恢复位置；窗口尺寸变化时把按钮收进可视区
  requestAnimationFrame(restorePos);
  const onResize = () => {
    const { x, y } = clampPos(launcher.offsetLeft, launcher.offsetTop);
    applyPos(x, y);
  };
  window.addEventListener('resize', onResize);

  // ---------------------------------------------------------- 与主程序联动

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
    window.removeEventListener('resize', onResize);
    if (typeof offCardUpdated === 'function') offCardUpdated();
    panel.destroy();
    root.remove();
  };
}

export function deactivate(api) {
  api.log?.('info', 'stm-ai-assistant 前端已卸载');
}
