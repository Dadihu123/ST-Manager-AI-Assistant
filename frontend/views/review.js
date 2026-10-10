/**
 * 结果与审批：逐条查看 AI 建议 → 接受 / 拒绝 → 统一写回。
 *
 * 审批走主程序端点 `POST /api/plugins/<id>/jobs/<run_id>/items/<key>/review`
 * （`core/plugins/routes.py`）；写回走插件自己的 `/jobs/<run_id>/apply`。
 */

import { clear, createUi, el, formatNumber } from '../dom.js';

const ITEM_STATUS_LABELS = {
  pending: '待处理',
  running: '处理中',
  done: '已接受',
  failed: '失败',
  skipped: '已跳过',
  awaiting_review: '待审批',
};

export function createReview(api, client, context) {
  const { badge, button, section } = createUi(api);
  const root = el('div', 'plugin-stm-ai-assistant-view');
  const state = { job: null, items: [], lastApply: null, busy: false };

  const headerSection = section(
    '分析结果',
    '「全部接受 / 全部拒绝」只影响待审批条目；「应用已接受」把状态为已接受的条目写回卡片。',
  );
  const summaryRow = el('div', 'plugin-stm-ai-assistant-row');
  const actionRow = el('div', 'plugin-stm-ai-assistant-row');
  const listBox = el('div', 'plugin-stm-ai-assistant-review-list');
  const detailBox = el('div', 'plugin-stm-ai-assistant-review-detail');

  const refreshBtn = button('刷新', 'is-secondary', load);
  const acceptAllBtn = button('全部接受', 'is-secondary', () => bulkReview('done'));
  const rejectAllBtn = button('全部拒绝', 'is-secondary', () => bulkReview('skipped'));
  const retryBtn = button('只重跑失败项', 'is-secondary', retryFailed);
  const applyBtn = button('应用已接受', 'is-primary', applyAccepted);

  actionRow.append(refreshBtn, acceptAllBtn, rejectAllBtn, retryBtn, applyBtn);
  headerSection.append(summaryRow, actionRow);
  root.append(headerSection, listBox, detailBox);

  function renderSummary() {
    clear(summaryRow);
    if (!context.runId) {
      summaryRow.append(el('span', 'plugin-stm-ai-assistant-hint', '当前没有作业，请先在「工作台」提交。'));
      return;
    }

    const job = state.job || {};
    const counts = { awaiting_review: 0, done: 0, failed: 0, skipped: 0, pending: 0 };
    state.items.forEach((item) => {
      if (counts[item.status] !== undefined) counts[item.status] += 1;
    });

    summaryRow.append(
      badge(`作业 ${String(context.runId).slice(0, 8)}`, 'is-ok'),
      badge(`状态 ${job.status || '-'}`),
      badge(`待审批 ${counts.awaiting_review}`),
      badge(`已接受 ${counts.done}`),
      badge(`失败 ${counts.failed}`, counts.failed ? 'is-error' : 'is-muted'),
      badge(`跳过 ${counts.skipped}`, 'is-muted'),
    );

    const summary = job.summary || {};
    if (summary.prompt_tokens) {
      summaryRow.append(badge(`prompt ${formatNumber(summary.prompt_tokens)} token`, 'is-muted'));
    }
  }

  function renderItems() {
    clear(listBox);
    if (!state.items.length) {
      listBox.append(el('p', 'plugin-stm-ai-assistant-hint', '（没有条目）'));
      return;
    }

    state.items.forEach((item) => {
      const result = item.result || {};
      const row = el('div', 'plugin-stm-ai-assistant-review-item');

      const head = el('div', 'plugin-stm-ai-assistant-review-head');
      head.append(
        badge(ITEM_STATUS_LABELS[item.status] || item.status,
          item.status === 'failed' ? 'is-error' : (item.status === 'awaiting_review' ? 'is-warn' : '')),
        el('span', 'plugin-stm-ai-assistant-review-key', result.card_id || item.item_key),
      );

      const summaryText = String(result.summary || '').trim();
      if (summaryText) {
        head.append(el('span', 'plugin-stm-ai-assistant-review-summary', summaryText));
      }

      const tags = Array.isArray(result.tags) ? result.tags : [];
      if (tags.length) {
        const tagRow = el('div', 'plugin-stm-ai-assistant-tags');
        tags.forEach((tag) => {
          const label = tag && tag.name ? tag.name : String(tag || '');
          const confidence = tag && tag.confidence != null ? ` ${tag.confidence}` : '';
          tagRow.append(badge(`${label}${confidence}`, 'is-muted'));
        });
        head.append(tagRow);
      }

      const suggestions = Array.isArray(result.new_tags) ? result.new_tags : [];
      if (suggestions.length) {
        head.append(el('span', 'plugin-stm-ai-assistant-hint',
          `新标签建议：${suggestions.map((tag) => tag.name).join('、')}`));
      }

      if (item.error) {
        head.append(el('span', 'plugin-stm-ai-assistant-warning', item.error));
      }

      const actions = el('div', 'plugin-stm-ai-assistant-review-actions');
      if (item.status === 'awaiting_review') {
        actions.append(
          button('接受', 'is-secondary', () => reviewOne(item, 'done')),
          button('拒绝', 'is-secondary', () => reviewOne(item, 'skipped')),
        );
      } else if (item.status === 'done') {
        actions.append(button('改回待审批', 'is-secondary', () => reviewOne(item, 'awaiting_review')));
      }
      head.append(actions);

      row.append(head);
      listBox.append(row);
    });
  }

  function renderDetail() {
    clear(detailBox);
    if (!state.lastApply) return;

    const data = state.lastApply;
    detailBox.append(el(
      'p', 'plugin-stm-ai-assistant-hint',
      `应用结果：写入 ${data.applied} · 已应用过 ${data.skipped} · `
      + `内容已变化跳过 ${data.stale} · 失败 ${data.failed}`,
    ));

    (data.details || []).slice(0, 10).forEach((detail) => {
      detailBox.append(el('div', 'plugin-stm-ai-assistant-warning',
        `${detail.card_id || ''} ${detail.error || detail.warning || ''}`.trim()));
    });
  }

  async function load() {
    if (!context.runId) {
      state.items = [];
      state.job = null;
      renderSummary();
      renderItems();
      return;
    }

    try {
      const data = await client.job(context.runId);
      state.job = data.job || {};
      state.items = data.items || [];
    } catch (error) {
      api.host.toast(`读取作业失败：${error.message}`, 3600, 'close');
      state.items = [];
    }

    renderSummary();
    renderItems();
  }

  async function reviewOne(item, status) {
    if (!context.runId) return;
    try {
      await client.reviewItem(context.runId, item.item_key, status);
      item.status = status;
      renderSummary();
      renderItems();
    } catch (error) {
      api.host.toast(`审批失败：${error.message}`, 3600, 'close');
    }
  }

  async function bulkReview(status) {
    if (!context.runId) return;
    const targets = state.items.filter((item) => item.status === 'awaiting_review');
    if (!targets.length) {
      api.host.toast('没有待审批条目', 2400, 'settings-help-entry');
      return;
    }

    state.busy = true;
    try {
      for (const item of targets) {
        await client.reviewItem(context.runId, item.item_key, status);
        item.status = status;
      }
      api.host.toast(`已批量标记 ${targets.length} 条`, 2400, 'check');
    } catch (error) {
      api.host.toast(`批量审批失败：${error.message}`, 3600, 'close');
    } finally {
      state.busy = false;
      renderSummary();
      renderItems();
    }
  }

  async function applyAccepted() {
    if (!context.runId) {
      api.host.toast('当前没有作业', 2400, 'close');
      return;
    }

    applyBtn.disabled = true;
    try {
      state.lastApply = await client.applyJob(context.runId, { apply_tags: true });
      const data = state.lastApply;
      api.host.toast(`已写入 ${data.applied} 张卡片`, 3200, 'check');
      if (data.applied) api.host.refresh('cards');
      renderDetail();
      await load();
    } catch (error) {
      api.host.toast(`应用失败：${error.message}`, 4000, 'close');
    } finally {
      applyBtn.disabled = false;
    }
  }

  async function retryFailed() {
    if (!context.runId) return;
    try {
      const data = await client.retryJob(context.runId, true);
      context.runId = data.run_id || context.runId;
      api.host.toast('已重新入队，可回到工作台查看进度', 3200, 'check');
      await load();
    } catch (error) {
      api.host.toast(`重跑失败：${error.message}`, 3600, 'close');
    }
  }

  return { element: root, refresh: load, destroy: () => {} };
}
