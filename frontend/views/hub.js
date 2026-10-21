/**
 * 任务中心（hub）—— 坞入口打开的首屏。
 *
 * 回答三个问题，别的不放：
 *   「现在能做什么」→ 四个任务卡
 *   「有没有要我处理的东西」→ 待审批卡
 *   「刚才那些跑哪去了」→ 最近作业
 *
 * 这里**不读也不写**任何作用域状态：任务面板各自持有自己的作用域，
 * 因此从右键进来再关掉、再从坞打开时看到的一定是干净的（见设计文档 §5）。
 */

import { banner, clear, emptyState, formatNumber, jobStatusLabel, skeleton, statsRow } from '../ui.js';
import { el, formatTime } from '../dom.js';
import { TASK_ORDER, TASKS } from '../tasks.js';

const TASK_ICON = {
  analyze: 'tags',
  polish: 'book-open',
  classify: 'folder',
  propose: 'layers',
};

export function createHub(api, client, context) {
  const root = el('div', 'plugin-stm-ai-assistant-view is-hub');

  const alertHost = el('div', 'plugin-stm-ai-assistant-alert-host');
  const summaryHost = el('div', 'plugin-stm-ai-assistant-hub-summary');
  const taskHost = el('div', 'plugin-stm-ai-assistant-task-grid');
  const reviewHost = el('div', 'plugin-stm-ai-assistant-hub-review');
  const runsHost = el('div', 'plugin-stm-ai-assistant-hub-runs');

  root.append(
    alertHost,
    summaryHost,
    el('div', 'plugin-stm-ai-assistant-hub-title', '现在能做什么'),
    taskHost,
    reviewHost,
    el('div', 'plugin-stm-ai-assistant-hub-title', '最近作业'),
    runsHost,
  );

  TASK_ORDER.forEach((taskId) => taskHost.append(renderTaskCard(TASKS[taskId])));

  function renderTaskCard(task) {
    const card = el('button', 'plugin-stm-ai-assistant-task-card');
    card.type = 'button';
    card.dataset.marqueeIgnore = 'true';

    const head = el('div', 'plugin-stm-ai-assistant-task-card-head');
    head.append(api.ui.icon(TASK_ICON[task.id] || 'wand', { className: 'ui-icon--md' }));
    head.append(el('span', 'plugin-stm-ai-assistant-task-card-title', task.label));
    card.append(head);
    card.append(el('p', 'plugin-stm-ai-assistant-task-card-summary', task.summary));
    card.append(el('p', 'plugin-stm-ai-assistant-task-card-detail', task.detail));

    const foot = el('div', 'plugin-stm-ai-assistant-task-card-foot');
    foot.append(el('span', 'plugin-stm-ai-assistant-task-card-verb', `打开${task.verb}面板 →`));
    card.append(foot);

    card.addEventListener('click', () => {
      context.openTask ? context.openTask(task.id, null) : api.surfaces.open(task.surface);
    });
    return card;
  }

  async function refresh() {
    await Promise.all([loadStatus(), loadRuns()]);
  }

  async function loadStatus() {
    clear(alertHost);
    clear(summaryHost);
    clear(reviewHost);
    summaryHost.append(skeleton('读取状态…'));
    try {
      const data = await client.status();
      clear(summaryHost);
      const pending = data.pending_reviews && typeof data.pending_reviews === 'object'
        ? Object.keys(data.pending_reviews).length
        : 0;
      const profiles = (data.profiles && data.profiles.count) || 0;
      const providers = (data.providers && data.providers.count) || 0;
      const suggestions = data.suggestions || 0;

      summaryHost.append(statsRow([
        { label: '待审批作业', value: formatNumber(pending) },
        { label: '新标签建议', value: formatNumber(suggestions) },
        { label: '提示词档案', value: formatNumber(profiles) },
        { label: 'Provider', value: formatNumber(providers) },
      ]));

      if (!providers) {
        const goto = api.ui.button({
          label: '去设置 Provider',
          variant: 'primary',
          onClick: () => context.openSettings(),
        });
        goto.dataset.marqueeIgnore = 'true';
        alertHost.append(banner('还没有配置 Provider，所有 AI 任务都无法运行。', 'warn', [goto]));
      }

      renderReviewCard(pending);
    } catch (error) {
      clear(summaryHost);
      alertHost.append(banner(`读取状态失败：${error.message}`, 'error'));
    }
  }

  function renderReviewCard(pending) {
    const card = el('div', 'plugin-stm-ai-assistant-hub-card');
    const head = el('div', 'plugin-stm-ai-assistant-hub-card-head');
    head.append(el('span', 'plugin-stm-ai-assistant-hub-card-title', '结果与审批'));
    if (pending) {
      head.append(api.ui.badge({ value: `${pending} 待审`, tone: 'warning' }));
    }
    card.append(head);
    card.append(el(
      'p',
      'plugin-stm-ai-assistant-hub-card-text',
      pending
        ? `有 ${pending} 个作业等待你确认后才会写入卡片或世界书。`
        : '作业跑完后会在这里逐条确认；没有待审内容时也可以翻看历史结果。',
    ));
    const open = api.ui.button({
      label: pending ? '去审批' : '打开审批面板',
      variant: pending ? 'primary' : 'secondary',
      onClick: () => context.openReview(''),
    });
    open.dataset.marqueeIgnore = 'true';
    card.append(open);
    reviewHost.append(card);
  }

  async function loadRuns() {
    clear(runsHost);
    runsHost.append(skeleton('读取最近作业…'));
    try {
      const data = await client.jobs(8);
      const jobs = data.jobs || [];
      clear(runsHost);
      if (!jobs.length) {
        runsHost.append(emptyState('还没有运行过任何任务。'));
        return;
      }
      const list = el('div', 'plugin-stm-ai-assistant-run-list');
      jobs.forEach((job) => list.append(renderRunRow(job)));
      runsHost.append(list);
    } catch (error) {
      clear(runsHost);
      runsHost.append(banner(`读取最近作业失败：${error.message}`, 'warn'));
    }
  }

  function renderRunRow(job) {
    const row = el('button', 'plugin-stm-ai-assistant-run-row');
    row.type = 'button';
    row.dataset.marqueeIgnore = 'true';

    const task = TASKS[String(job.task || '')];
    row.append(el('span', 'plugin-stm-ai-assistant-run-task', task ? task.label : String(job.job_type || '作业')));

    const status = String(job.status || '');
    const statusNode = el('span', `plugin-stm-ai-assistant-run-status is-${status}`);
    statusNode.textContent = jobStatusLabel(status);
    row.append(statusNode);

    const counts = job.counts || {};
    if (counts.total) {
      row.append(el('span', 'plugin-stm-ai-assistant-run-counts',
        `${counts.completed || 0}/${counts.total}`));
    }
    row.append(el('span', 'plugin-stm-ai-assistant-run-time', formatTime(job.created_at)));

    row.addEventListener('click', () => context.openReview(String(job.run_id || '')));
    return row;
  }

  return {
    element: root,
    refresh,
    applyContext() {
      // hub 不消费作用域：入口带目标却打开 hub 是无意义的组合，
      // 这里只清理可能残留的提示，不做任何作用域继承。
      clear(alertHost);
    },
    destroy() {
      clear(root);
    },
  };
}
