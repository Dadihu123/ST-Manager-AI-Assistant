/**
 * 审批页 / AI打标历史记录 共用的「手动添加标签」输入行。
 *
 * 行为（用户定的）：
 * - 手动加标签是用户自己的操作，不判断是否落地；
 * - 点「添加」立即写卡，不需要再点「落地」；
 * - 不显示 chips、不显示「已写入」；成功/被跳过都用 3 秒 toast 告知。
 *
 * `onAdd(names)` 由调用方提供（审批页走作业条目接口，历史卡片走历史接口），
 * 返回 `{added, skipped_blacklist, skipped_unknown}`。
 */

import { el, input } from '../dom.js';

/** 输入拆分：中英文逗号 / 分号 / 顿号 / 斜杠 / 换行都认。 */
export function splitManualInput(text) {
  const result = [];
  String(text || '').split(/[,，;；、/\n\r]+/).forEach((part) => {
    const name = part.trim();
    if (name && !result.includes(name)) result.push(name);
  });
  return result;
}

export function createManualTagInput({ api, onAdd, note = '手动添加标签（直接写入卡片）' }) {
  const wrap = el('div', 'plugin-stm-ai-assistant-manual-tags');
  const row = el('div', 'plugin-stm-ai-assistant-manual-row');
  const box = input('', 'text', '手动添加标签，用 、 或 / 分隔…');
  const addBtn = api.ui.button({
    label: '添加',
    variant: 'secondary',
    onClick: () => add(),
  });
  addBtn.dataset.marqueeIgnore = 'true';
  row.append(box, addBtn);

  async function add() {
    const names = splitManualInput(box.value);
    if (!names.length) return;
    addBtn.disabled = true;
    try {
      const data = await onAdd(names);
      box.value = '';
      const added = (data && data.added) || [];
      const skipped = [
        ...((data && data.skipped_blacklist) || []),
        ...((data && data.skipped_unknown) || []),
      ];
      if (added.length) {
        api.host.toast(`已添加标签：${added.join('、')}`, 3000, 'check');
      }
      if (skipped.length) {
        api.host.toast(
          `没有写入（黑名单 / 标签库锁）：${skipped.join('、')}`,
          3000,
          'close',
        );
      }
    } catch (error) {
      api.host.toast(`添加标签失败：${error.message}`, 3600, 'close');
    } finally {
      addBtn.disabled = false;
    }
  }

  box.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      add();
    }
  });

  wrap.append(
    el('p', 'plugin-stm-ai-assistant-note', note),
    row,
    el(
      'p',
      'plugin-stm-ai-assistant-field-hint',
      '点「添加」立即写入这张卡；黑名单词与「标签库锁」下的新标签会被宿主跳过（会有提示）。',
    ),
  );
  return wrap;
}
