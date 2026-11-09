/**
 * AI打标历史记录 —— 落地成功的打标结果按「落地日期」归档。
 *
 * 数据来自插件自己的快照（`history.json`），所以原作业被清理后仍能查看；
 * 每张卡片的「改回待审批」需要原作业还在：先在宿主把条目状态改回
 * `awaiting_review`（主程序自己的 review 路由），再让插件清掉历史与落地标记。
 */

import { banner, clear, emptyState, formatNumber, skeleton } from '../ui.js';
import { el } from '../dom.js';
import { renderAnalyzeItem } from './review.js';
import { createManualTagInput } from './manualTags.js';

function formatClock(stamp) {
  const value = Number(stamp) || 0;
  if (!value) return '';
  return new Date(value * 1000).toLocaleTimeString('zh-CN', {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function createHistory(api, client, context) {
  const root = el('div', 'plugin-stm-ai-assistant-view is-history');
  const head = el('div', 'plugin-stm-ai-assistant-history-head');
  const headInfo = el('span', 'plugin-stm-ai-assistant-note', '');
  const clearBtn = api.ui.button({
    label: '清空当日记录',
    variant: 'ghost',
    onClick: () => clearDay(),
  });
  clearBtn.dataset.marqueeIgnore = 'true';
  head.append(headInfo, clearBtn);

  const layout = el('div', 'plugin-stm-ai-assistant-history');
  const sidebar = el('div', 'plugin-stm-ai-assistant-history-dates');
  const main = el('div', 'plugin-stm-ai-assistant-history-main');
  layout.append(sidebar, main);
  root.append(head, layout);

  const state = {
    date: '',
    dates: [],
    entries: [],
    busy: false,
    /** 展开的月份（`YYYY-MM`）：日期栏是折叠式，默认只展开当前选中日期所在月。 */
    expandedMonths: new Set(),
  };

  async function refresh(date) {
    const target = date !== undefined ? String(date || '') : state.date;
    clear(main);
    main.append(skeleton('读取历史记录…'));
    try {
      const data = await client.history(target);
      state.date = String(data.date || '');
      state.dates = Array.isArray(data.dates) ? data.dates : [];
      state.entries = Array.isArray(data.entries) ? data.entries : [];
      if (state.date) state.expandedMonths.add(state.date.slice(0, 7));
    } catch (error) {
      clear(main);
      main.append(banner(`读取历史记录失败：${error.message}`, 'error'));
      return;
    }
    renderDates();
    renderEntries();
  }

  async function selectDate(date) {
    if (state.busy || !date) return;
    state.date = String(date);
    await refresh(state.date);
  }

  async function removeEntry(entry) {
    if (state.busy) return;
    state.busy = true;
    try {
      await client.deleteHistory(entry.entry_id);
      await refresh(state.date);
    } catch (error) {
      api.host.toast(`删除记录失败：${error.message}`, 3600, 'close');
    } finally {
      state.busy = false;
    }
  }

  async function clearDay() {
    if (state.busy || !state.date) return;
    const confirmed = await confirmClear();
    if (!confirmed) return;
    state.busy = true;
    try {
      const data = await client.clearHistory(state.date);
      api.host.toast(`已清空 ${state.date} 的 ${formatNumber(data.removed || 0)} 条记录`, 3000, 'check');
      await refresh(state.date);
    } catch (error) {
      api.host.toast(`清空失败：${error.message}`, 3600, 'close');
    } finally {
      state.busy = false;
    }
  }

  async function confirmClear() {
    const message = `将删除 ${state.date} 的全部历史记录（${formatNumber(state.entries.length)} 条）。`
      + '只删历史快照，不会改动卡片、标签或已写入的概览。';
    if (typeof api.ui.confirm === 'function') {
      return Boolean(await api.ui.confirm({
        title: '清空当日记录',
        message,
        confirmLabel: '删除',
      }));
    }
    return window.confirm(message);
  }

  async function reopen(entry) {
    if (state.busy) return;
    state.busy = true;
    try {
      // 1) 宿主的条目状态：改回待审批（主程序自己的 REST 路由）
      await client.reviewItem(entry.run_id, entry.item_key, 'awaiting_review');
      // 2) 插件侧：删快照 + 清掉落地的 applied_runs 标记
      await client.reopenHistory(entry.entry_id);
      api.host.toast('已改回待审批，可在「结果与审批」里重新处理', 3200, 'check');
      if (typeof context.notifyReview === 'function') context.notifyReview();
      await refresh(state.date);
    } catch (error) {
      api.host.toast(`改回待审批失败：${error.message}`, 4000, 'close');
    } finally {
      state.busy = false;
    }
  }

  // ---------------------------------------------------------------- 左栏：日期

  function renderDates() {
    clear(sidebar);
    if (!state.dates.length) return;
    sidebar.append(el('div', 'plugin-stm-ai-assistant-history-dates-title', '日期'));
    const list = el('div', 'plugin-stm-ai-assistant-history-dates-list');
    const currentYear = String(new Date().getFullYear());
    let lastMonth = '';
    state.dates.forEach((item) => {
      const parts = String(item.date || '').split('-');
      if (parts.length !== 3) return;
      const [year, month, day] = parts;
      const monthKey = `${year}-${month}`;
      if (monthKey !== lastMonth) {
        lastMonth = monthKey;
        const expanded = state.expandedMonths.has(monthKey);
        const label = year === currentYear
          ? `${Number(month)}月`
          : `${year}年 ${Number(month)}月`;
        const toggle = el(
          'button',
          'plugin-stm-ai-assistant-history-month',
          `${label} ${expanded ? '▾' : '▸'}`,
        );
        toggle.type = 'button';
        toggle.dataset.marqueeIgnore = 'true';
        toggle.title = expanded ? '收起这个月' : '展开这个月';
        toggle.addEventListener('click', () => {
          if (state.expandedMonths.has(monthKey)) state.expandedMonths.delete(monthKey);
          else state.expandedMonths.add(monthKey);
          renderDates();
        });
        list.append(toggle);
      }
      // 折叠式：只有展开的月份才渲染日期
      if (!state.expandedMonths.has(monthKey)) return;
      const button = el('button', 'plugin-stm-ai-assistant-history-day', String(Number(day)));
      button.type = 'button';
      button.dataset.marqueeIgnore = 'true';
      button.title = `${item.date}（${item.count} 条）`;
      if (item.date === state.date) button.classList.add('is-active');
      button.addEventListener('click', () => selectDate(item.date));
      list.append(button);
    });
    sidebar.append(list);
  }

  // ---------------------------------------------------------------- 右栏：卡片

  function renderEntries() {
    clear(main);
    headInfo.textContent = state.date
      ? `${state.date} · ${formatNumber(state.entries.length)} 条`
      : '';
    if (!state.entries.length) {
      main.append(emptyState('这一天还没有落地记录。'));
      return;
    }
    state.entries.forEach((entry) => main.append(renderCard(entry)));
  }

  function renderCard(entry) {
    const card = el('div', 'plugin-stm-ai-assistant-review-item is-history-card');
    const head = el('div', 'plugin-stm-ai-assistant-review-head');
    const accepted = entry.accepted !== false;
    head.append(api.ui.badge({
      value: accepted ? '已接受' : '未接受',
      tone: accepted ? 'success' : 'warning',
    }));
    head.append(el(
      'span',
      'plugin-stm-ai-assistant-review-key',
      String(entry.card_id || entry.item_key || ''),
    ));
    head.append(el(
      'span',
      'plugin-stm-ai-assistant-history-time',
      formatClock(entry.applied_at),
    ));

    const actions = el('div', 'plugin-stm-ai-assistant-review-actions');
    const reopenBtn = api.ui.button({
      label: '改回待审批',
      variant: 'ghost',
      onClick: () => reopen(entry),
    });
    reopenBtn.dataset.marqueeIgnore = 'true';
    const deleteBtn = el('button', 'plugin-stm-ai-assistant-history-delete', '×');
    deleteBtn.type = 'button';
    deleteBtn.title = '删除这条历史记录（只删记录，不动卡片）';
    deleteBtn.dataset.marqueeIgnore = 'true';
    deleteBtn.addEventListener('click', () => removeEntry(entry));
    actions.append(reopenBtn, deleteBtn);
    head.append(actions);

    card.append(head);
    card.append(renderAnalyzeItem({
      summary: entry.summary || '',
      tags: Array.isArray(entry.tags) ? entry.tags : [],
      new_tags: Array.isArray(entry.new_tags) ? entry.new_tags : [],
      tag_removals: Array.isArray(entry.tag_removals) ? entry.tag_removals : [],
    }));
    card.append(createManualTagInput({
      api,
      note: '手动添加标签（直接写入卡片）',
      onAdd: (names) => client.historyManualTags(entry.entry_id, names),
    }));
    return card;
  }

  return {
    element: root,
    refresh,
    /** 宿主再次打开这个 surface 时调用：始终看最新数据。 */
    onOpen() {
      return refresh(state.date || undefined);
    },
    destroy() {
      clear(root);
    },
  };
}
