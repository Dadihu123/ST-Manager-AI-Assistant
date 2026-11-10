/**
 * 面板共用外壳与作业管线。
 *
 * 每个任务面板都由同一组积木拼成（见 docs/2026-10-19-task-centric-redesign.md §1）：
 *
 *   作用域横幅 → 参数区 → 预览 → 主按钮 → 内联进度 → 结果入口
 *
 * 以前这套流程散在 workbench 的 7 个段落里，每个段落自己管作用域、自己管
 * 进度、只有打标接了 SSE。现在统一到这里：任何任务只要给出 `spec`，
 * 就能得到一致的行为——包括「提交后在同一面板里看到进度」。
 */

import { el, clear, field, formatNumber, formatCost, formatTime } from './dom.js';
import { describeScope, serializeScope } from './scope.js';

const LAST_RUN_KEY = (taskId) => `task.${taskId}.last_run`;

/** 读取某任务上次运行的 run_id（关面板不丢）。 */
export function readLastRun(api, taskId) {
  try {
    return String(api.storage.get(LAST_RUN_KEY(taskId)) || '');
  } catch (error) {
    return '';
  }
}

/** 记住某任务最近一次运行。 */
export function rememberRun(api, taskId, runId) {
  try {
    if (runId) api.storage.set(LAST_RUN_KEY(taskId), String(runId));
  } catch (error) {
    console.warn('[stm-ai] 保存 last_run 失败:', error);
  }
}

// ---------------------------------------------------------------- 视觉积木

/** 顶部提示条（info / warn / error / ok）。 */
export function banner(text, tone = 'info', actions = []) {
  const node = el('div', `plugin-stm-ai-assistant-banner is-${tone}`);
  node.setAttribute('role', tone === 'error' ? 'alert' : 'status');
  node.append(el('span', 'plugin-stm-ai-assistant-banner-text', text));
  if (actions.length) {
    const group = el('div', 'plugin-stm-ai-assistant-banner-actions');
    actions.forEach((action) => group.append(action));
    node.append(group);
  }
  return node;
}

/**
 * 作用域横幅 —— 入口打开面板后第一眼看到的东西。
 *
 * 明确回答三个问题：作用域是什么、有多少、能不能改。
 */
export function scopeBar({ scope, error, loading, onChange = null, hint = '' }) {
  const node = el('div', 'plugin-stm-ai-assistant-scope-bar');
  const main = el('div', 'plugin-stm-ai-assistant-scope-main');
  const title = el('div', 'plugin-stm-ai-assistant-scope-title');
  // 圆点由 CSS 画（`background: currentColor`），不再塞一个占位字符（旧版在这里写了
  // 「◎」，字体缺字时会显示成方框）。
  title.append(el('span', 'plugin-stm-ai-assistant-scope-icon'));

  let text = '尚未选择作用域';
  let tone = 'is-muted';
  if (loading) {
    text = '正在读取作用域…';
    tone = 'is-loading';
  } else if (error) {
    text = error;
    tone = 'is-error';
  } else if (scope) {
    text = describeScope(scope);
    tone = scope.empty ? 'is-warn' : 'is-ok';
  }
  title.append(el('span', 'plugin-stm-ai-assistant-scope-text', text));
  title.classList.add(tone);
  main.append(title);

  if (hint) main.append(el('div', 'plugin-stm-ai-assistant-scope-hint', hint));
  node.append(main);

  if (typeof onChange === 'function') {
    const button = el('button', 'plugin-stm-ai-assistant-scope-change', '更改');
    button.type = 'button';
    button.dataset.marqueeIgnore = 'true';
    button.addEventListener('click', onChange);
    node.append(button);
  }
  return node;
}

/** 进度块：进度条 + 计数 + 取消按钮。 */
export function progressBlock({ onCancel = null } = {}) {
  const root = el('div', 'plugin-stm-ai-assistant-progress');
  const track = el('div', 'plugin-stm-ai-assistant-progress-track');
  const fill = el('div', 'plugin-stm-ai-assistant-progress-fill');
  track.append(fill);
  const meta = el('div', 'plugin-stm-ai-assistant-progress-meta');
  const counts = el('span', 'plugin-stm-ai-assistant-progress-counts', '准备中…');
  meta.append(counts);

  const cancel = el('button', 'plugin-stm-ai-assistant-progress-cancel', '取消');
  cancel.type = 'button';
  cancel.dataset.marqueeIgnore = 'true';
  if (typeof onCancel === 'function') cancel.addEventListener('click', onCancel);
  meta.append(cancel);

  root.append(track, meta);
  root.hidden = true;

  return {
    node: root,
    show() {
      root.hidden = false;
    },
    hide() {
      root.hidden = true;
    },
    update({ percent = 0, completed = 0, total = 0, failed = 0, message = '' } = {}) {
      root.hidden = false;
      fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
      const parts = [`${completed}/${total}`];
      if (message) parts.unshift(message);
      if (failed) parts.push(`失败 ${failed}`);
      counts.textContent = parts.join(' · ');
    },
    setRunning(running) {
      cancel.disabled = !running;
      cancel.hidden = !running;
    },
  };
}

/** 主按钮行：主动作 + 次动作。 */
export function actionRow() {
  const node = el('div', 'plugin-stm-ai-assistant-actions');
  return node;
}

/** 载入态占位。 */
export function skeleton(text = '正在读取…') {
  const node = el('div', 'plugin-stm-ai-assistant-skeleton', text);
  node.setAttribute('aria-busy', 'true');
  return node;
}

/** 空态。 */
export function emptyState(text, action = null) {
  const node = el('div', 'plugin-stm-ai-assistant-empty');
  node.append(el('p', 'plugin-stm-ai-assistant-empty-text', text));
  if (action) node.append(action);
  return node;
}

/** 键值网格（成本 / 条目数 / 模型等摘要）。 */
export function statsRow(items) {
  const row = el('div', 'plugin-stm-ai-assistant-stats');
  items.forEach((item) => {
    const cell = el('div', 'plugin-stm-ai-assistant-stat');
    cell.append(el('span', 'plugin-stm-ai-assistant-stat-label', item.label));
    cell.append(el('span', 'plugin-stm-ai-assistant-stat-value', item.value));
    row.append(cell);
  });
  return row;
}

/** 成本 / token 摘要行。 */
export function estimateRow(estimate, { total = 0 } = {}) {
  if (!estimate) return null;
  const check = estimate.check || {};
  const cost = check.cost || {};
  const parts = [];
  if (total) parts.push({ label: '条目', value: formatNumber(total) });
  if (check.total_tokens) parts.push({ label: '预计 token', value: formatNumber(check.total_tokens) });
  parts.push({ label: '预计花费', value: formatCost(cost) });
  return statsRow(parts);
}

/**
 * 预览区：把 preview 接口的返回渲染成「将发生什么」。
 *
 * 每个任务的首条卡片原文/差量由 `renderPreview` 负责，这里只管外壳。
 */
export function previewSection({ primary, extra = [] }) {
  const wrap = el('div', 'plugin-stm-ai-assistant-preview');
  if (primary) wrap.append(primary);
  extra.forEach((node) => wrap.append(node));
  return wrap;
}

// ---------------------------------------------------------------- 作业管线

/**
 * 提交作业并订阅进度。
 *
 * `submit` 由调用方提供（各任务参数不同），管线只负责：
 * 记住 run_id → 订阅 SSE → 把事件翻译成进度 → 结束时回调。
 */
export async function runTaskJob({
  api,
  taskId,
  submit,
  onProgress,
  onDone,
  onError,
  onCancelled,
}) {
  let runId = '';
  let response = null;
  try {
    response = await submit();
    runId = String((response && response.run_id) || '');
    if (!runId) {
      // 空提交（「没有需要处理的卡片」）不是失败：把后端的 code / 原因带出去，
      // 让面板能画出「为什么没提交 + 怎么办」，而不是一条红色「提交失败」。
      const message = (response && response.message) || '没有需要处理的内容';
      const empty = new Error(message);
      empty.code = (response && response.code) || 'nothing_to_process';
      empty.payload = response || {};
      onError && onError(empty, { response, empty: true });
      return { runId: '', response };
    }
    rememberRun(api, taskId, runId);
  } catch (error) {
    onError && onError(error, {});
    return { runId: '', error };
  }

  if (typeof api.subscribeJob !== 'function') {
    onDone && onDone({ runId, response });
    return { runId };
  }

  let unsubscribe = null;
  let settled = false;
  /** 最近一次快照的进度（`item` 帧只带条目，不带计数，需要本地累加）。 */
  let latest = normalizeProgress(null);

  const finish = (payload) => {
    if (settled) return;
    settled = true;
    try {
      if (typeof unsubscribe === 'function') unsubscribe();
    } catch (error) {
      console.warn('[stm-ai] 取消作业订阅失败:', error);
    }
    onDone && onDone({ runId, response, ...payload });
  };

  try {
    const maybe = api.subscribeJob(runId, {
      snapshot(job) {
        latest = normalizeProgress(job);
        onProgress && onProgress(latest);
        const status = String((job && job.status) || '');
        if (['completed', 'failed', 'cancelled', 'aborted'].includes(status)) {
          finish({ job });
        }
      },
      item(payload) {
        // 宿主逐条上报的是 `item` 事件（snapshot 只在订阅开始时合成一次）。
        // 不在这里累加，进度条会一直停在 0/N，直到作业结束 —— 用户会以为卡死。
        const status = String((payload && payload.status) || '');
        if (!['done', 'failed', 'skipped', 'awaiting_review'].includes(status)) return;
        const next = {
          ...latest,
          status: latest.status === 'completed' ? 'completed' : 'running',
          completed: latest.completed + (status === 'done' ? 1 : 0),
          failed: latest.failed + (status === 'failed' ? 1 : 0),
          skipped: latest.skipped
            + (status === 'skipped' || status === 'awaiting_review' ? 1 : 0),
        };
        const total = Number(next.total || 0);
        const finished = next.completed + next.failed + next.skipped;
        next.percent = total
          ? Math.min(100, Math.round((finished / total) * 100))
          : 0;
        // 让面板能显示「正在跑哪张卡」：宿主的 item 帧字段是 `item_key`。
        const rawKey = String((payload && (payload.item_key || payload.key)) || '');
        const itemName = rawKey ? rawKey.split('/').pop() : '';
        if (itemName) {
          next.message = total
            ? `已完成 ${Math.min(finished, total)}/${total}：${itemName}`
            : `已完成：${itemName}`;
        }
        latest = next;
        onProgress && onProgress(next);
      },
      done(payload) {
        // `done` 帧只有状态与计数，没有 job_type/message，合并进最后一次 snapshot
        finish({ job: { ...latest, ...(payload || {}) } });
      },
      error(error) {
        if (settled) return;
        settled = true;
        try {
          if (typeof unsubscribe === 'function') unsubscribe();
        } catch (inner) { /* noop */ }
        onError && onError(error instanceof Error ? error : new Error(String(error)), {});
      },
    });
    if (typeof maybe === 'function') unsubscribe = maybe;
  } catch (error) {
    onError && onError(error, {});
  }

  return { runId };
}

/**
 * 归一化宿主作业快照。
 *
 * 坑：SSE `snapshot` 把计数放在**顶层**（`total` / `completed` / `failed` /
 * `skipped`，见主程序 `core/plugins/jobs.py` 的 `build_snapshot`），而插件后端
 * `/jobs/<id>` 返回的是 `job` 对象（同样的扁平形状）。早期版本读 `job.counts`，
 * 结果进度条永远停在 0——这里统一兜住两种形状。
 */
export function normalizeProgress(job) {
  const source = (job && job.counts) || job || {};
  const total = Number(source.total || 0);
  const completed = Number(source.completed || 0);
  const failed = Number(source.failed || 0);
  const skipped = Number(source.skipped || 0);
  const percent = total ? Math.round(((completed + failed + skipped) / total) * 100) : 0;
  return {
    status: String((job && job.status) || ''),
    percent: Math.min(100, percent),
    completed,
    total,
    failed,
    skipped,
    message: String((job && (job.message || job.current_item)) || ''),
  };
}

/** 作业状态 → 中文标签。 */
export function jobStatusLabel(status) {
  const map = {
    queued: '排队中',
    running: '运行中',
    paused: '已暂停',
    completed: '已完成',
    failed: '失败',
    cancelled: '已取消',
    awaiting_review: '待审批',
    applied: '已落地',
  };
  return map[String(status || '')] || String(status || '未知');
}

/** 建立「参数区」容器，各任务往里放自己的控件。 */
export function optionsBlock(title = '参数') {
  const node = el('div', 'plugin-stm-ai-assistant-option-block');
  node.append(el('div', 'plugin-stm-ai-assistant-option-title', title));
  const body = el('div', 'plugin-stm-ai-assistant-option-body');
  node.append(body);
  return { node, body };
}

/** 作用域 → 提交参数（卡片 / 世界书两种形状）。 */
export function scopePayload(scope) {
  return serializeScope(scope) || {};
}

export { clear, el, field, formatNumber, formatCost, formatTime };
