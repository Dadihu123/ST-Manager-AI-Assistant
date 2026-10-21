/**
 * 宿主标题行里的状态胶囊。
 *
 * 位置约定（沿用上一轮修好的结构，别回退）：品牌图标与标题归**宿主**标题行，
 * 胶囊通过 `spec.headerExtra` 注入宿主的附加槽，与标题、关闭按钮**同一行**。
 * 插件自绘第二条状态栏会让弹层顶部出现两行，比宿主自己的弹窗「多一层」。
 *
 * 内容只放「一眼需要知道的三件事」：
 *   - 待审批作业数（决定要不要去审批）
 *   - Provider / 密钥是否就绪（决定任务能不能跑）
 *   - 写回方式（auto / review）
 *
 * 各任务面板不再各自拉一次 status：胶囊只挂在任务中心（hub）上，
 * 其余面板从 `context.statusSnapshot` 读同一份快照。
 */

import { clear, el } from './dom.js';

const TONE_BY_LEGACY = {
  'is-ok': 'success',
  'is-warn': 'warning',
  'is-error': 'danger',
  'is-muted': 'neutral',
};

export function createHeaderStatus(api) {
  const slot = el('span', 'plugin-stm-ai-assistant-status-badges');

  function badge(value, legacyTone = '') {
    return api.ui.badge({ value, tone: TONE_BY_LEGACY[legacyTone] || 'neutral' });
  }

  return {
    element: slot,
    /** 挂进宿主标题行的附加槽（`spec.headerExtra` 的回调目标）。 */
    mount(container) {
      if (container && slot.parentNode !== container) container.append(slot);
    },
    render(snapshot) {
      const data = snapshot || {};
      const pending = Number(data.pending) || 0;
      const providers = Number(data.providers) || 0;
      const applyMode = String(data.applyMode || 'review');
      clear(slot);
      slot.append(
        badge(`待审批 ${pending}`, pending ? 'is-warn' : 'is-muted'),
        badge(`Provider ${providers}`, providers ? 'is-muted' : 'is-error'),
        badge(`写回 ${applyMode === 'auto' ? '直接写入' : '逐条审批'}`, 'is-muted'),
      );
    },
    error(message) {
      clear(slot);
      slot.append(badge(`状态读取失败：${message}`, 'is-error'));
    },
    detach() {
      slot.remove();
    },
  };
}
