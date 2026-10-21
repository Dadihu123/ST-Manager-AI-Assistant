/**
 * Prompt 预览（独立 modal surface）。
 *
 * 从任务面板里拆出来的原因：它是「次级内容」，不该和任务执行挤在同一个容器里；
 * 宿主 modal 自带遮罩、居中与焦点陷阱，正好承载这种「看完就关」的东西。
 */

import { clear, el } from './dom.js';

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
        description: '请在任意任务面板里点「预览」。',
      }));
      return;
    }

    blocks.forEach((message) => {
      const block = el('div', 'plugin-stm-ai-assistant-message');
      block.append(el('div', 'plugin-stm-ai-assistant-message-role', String(message.role || '')));
      block.append(el('pre', 'plugin-stm-ai-assistant-message-body', String(message.content || '')));
      root.append(block);
    });
  }

  return {
    mount(container) {
      root = el('div', 'plugin-stm-ai-assistant-view');
      container.append(root);
      render();
      return () => {
        root.remove();
        root = null;
      };
    },
    setMessages(messages) {
      blocks = Array.isArray(messages) ? messages : [];
      render();
    },
  };
}
