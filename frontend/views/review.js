/**
 * 结果与审批 —— 四类作业共用，但**按 job_type 渲染不同形态**。
 *
 * 旧版只认 `summary` / `tags` / `new_tags` 三个字段，于是世界书改写的 diff、
 * 归类的目标分类、提议的方案树全都看不见（数据早就由后端返回了）。
 * 现在每种作业一个渲染器，落地前统一确认，落地参数按类型裁剪。
 *
 * 审批状态写在主程序作业里（`POST /api/plugins/<id>/jobs/<run_id>/items/<key>/review`），
 * 落地走插件自己的 `/jobs/<run_id>/apply`。
 */

import { banner, clear, emptyState, formatNumber, jobStatusLabel, readLastRun, skeleton, statsRow } from '../ui.js';
import { el } from '../dom.js';
import { TASKS } from '../tasks.js';

const ITEM_STATUS_LABELS = {
  pending: '待处理',
  running: '处理中',
  done: '已接受',
  failed: '失败',
  skipped: '已跳过',
  awaiting_review: '待审批',
};

const JOB_RENDERERS = {
  'ai.analyze': renderAnalyzeItem,
  'ai.wi_rewrite': renderPolishItem,
  'ai.classify': renderClassifyItem,
  'ai.propose': renderProposeItem,
  'ai.agent': renderProposeItem,
};

const TASK_OF_JOB = {
  'ai.analyze': 'analyze',
  'ai.wi_rewrite': 'polish',
  'ai.classify': 'classify',
  'ai.propose': 'propose',
  'ai.agent': 'propose',
};

/**
 * 作业条目的稳定键。
 *
 * 主程序的作业条目字段是 `key`（`job_store._row_to_item`），插件前端以前统一读
 * `item_key` —— 于是「接受」发出去的是 `undefined`，叠加上路由不支持 `/` 直接
 * 产出 404（§9 的 P2-9 + P0-1）。这里做成单一出口，两种形状都认。
 */
function itemKeyOf(item) {
  if (!item || typeof item !== 'object') return '';
  return String(item.key || item.item_key || '');
}

/**
 * 条目是否有 **block 级**问题（`result.issues`）。
 *
 * `warnings` 是 note 级（变长/没精简/删得偏多），永不拦；只有 `issues`
 * 才影响「批量接受」的默认行为（§6.2 硬规则 2）。
 */
function hasBlockIssues(item) {
  const issues = (item && item.result && item.result.issues) || [];
  return Array.isArray(issues) && issues.length > 0;
}

export function createReview(api, client, context) {
  const root = el('div', 'plugin-stm-ai-assistant-view is-review');
  const state = {
    runId: '',
    job: {},
    items: [],
    runs: [],
    lastApply: null,
    busy: false,
    // 「写什么」在落地时决定：打标任务的概览 / 标签各一个开关。
    writeNote: true,
    writeTags: true,
  };

  const alertHost = el('div', 'plugin-stm-ai-assistant-alert-host');
  const runBarHost = el('div', 'plugin-stm-ai-assistant-run-bar-host');
  const summaryHost = el('div', 'plugin-stm-ai-assistant-review-summary');
  const toolbarHost = el('div', 'plugin-stm-ai-assistant-review-toolbar');
  const listHost = el('div', 'plugin-stm-ai-assistant-review-list');
  const detailHost = el('div', 'plugin-stm-ai-assistant-review-detail');

  root.append(alertHost, runBarHost, summaryHost, toolbarHost, listHost, detailHost);

  // ---------------------------------------------------------------- 工具条

  const acceptAllBtn = api.ui.button({
    label: '全部接受',
    variant: 'secondary',
    onClick: () => bulkReview('done'),
  });
  const rejectAllBtn = api.ui.button({
    label: '全部拒绝',
    variant: 'secondary',
    onClick: () => bulkReview('skipped'),
  });
  const retryBtn = api.ui.button({
    label: '只重跑失败项',
    variant: 'secondary',
    onClick: () => retryFailed(),
  });
  const applyBtn = api.ui.button({
    label: '落地已接受的',
    variant: 'primary',
    onClick: () => applyAccepted(),
  });
  [acceptAllBtn, rejectAllBtn, retryBtn, applyBtn].forEach((node) => {
    node.dataset.marqueeIgnore = 'true';
    toolbarHost.append(node);
  });

  // ---------------------------------------------------------------- 作业选择条

  function renderRunBar() {
    clear(runBarHost);
    if (!state.runs.length && !state.runId) {
      runBarHost.append(emptyState('还没有作业。到「任务中心」选一个任务开始。'));
      return;
    }
    const bar = el('div', 'plugin-stm-ai-assistant-run-bar');
    bar.append(el('span', 'plugin-stm-ai-assistant-run-bar-label', '作业'));
    const select = el('select', 'plugin-stm-ai-assistant-run-bar-select');
    select.dataset.marqueeIgnore = 'true';
    state.runs.forEach((run) => {
      const option = el('option');
      option.value = String(run.run_id || '');
      const task = TASKS[String(run.task || '')];
      option.textContent = `${task ? task.label : run.job_type || '作业'} · ${String(run.run_id).slice(0, 8)} · ${jobStatusLabel(run.status)}`;
      if (String(run.run_id) === state.runId) option.selected = true;
      select.append(option);
    });
    select.addEventListener('change', () => selectRun(select.value));
    bar.append(select);

    const refreshBtn = api.ui.button({ label: '刷新', variant: 'ghost', onClick: () => refresh() });
    refreshBtn.dataset.marqueeIgnore = 'true';
    bar.append(refreshBtn);
    runBarHost.append(bar);
  }

  // ---------------------------------------------------------------- 摘要

  function renderSummary() {
    clear(summaryHost);
    if (!state.runId) return;

    const counts = { awaiting_review: 0, done: 0, failed: 0, skipped: 0 };
    state.items.forEach((item) => {
      if (counts[item.status] !== undefined) counts[item.status] += 1;
    });

    const jobType = String(state.job.job_type || '');
    const task = TASKS[TASK_OF_JOB[jobType] || ''] || null;

    summaryHost.append(statsRow([
      { label: '任务', value: task ? task.label : jobType || '-' },
      { label: '状态', value: jobStatusLabel(state.job.status) },
      { label: '待审批', value: formatNumber(counts.awaiting_review) },
      { label: '已接受', value: formatNumber(counts.done) },
      { label: '失败', value: formatNumber(counts.failed) },
      { label: '跳过', value: formatNumber(counts.skipped) },
    ]));

    // 作业级错误（例如 LLM 校验未通过）必须显式呈现，不能只留在条目里
    if (state.job.error) {
      summaryHost.append(banner(`作业错误：${state.job.error}`, 'error'));
    }
    const summary = state.job.summary || {};
    if (summary.prompt_tokens || summary.completion_tokens) {
      summaryHost.append(el(
        'p',
        'plugin-stm-ai-assistant-note',
        `用量：prompt ${formatNumber(summary.prompt_tokens || 0)} · `
        + `completion ${formatNumber(summary.completion_tokens || 0)} token`,
      ));
    }

    const pending = counts.awaiting_review;
    // 打标任务：「写什么」在落地时决定（提交时不再有「分析目标」这个假控件）
    if (jobType === 'ai.analyze') {
      summaryHost.append(renderWriteToggles());
    }
    applyBtn.disabled = state.busy || pending > 0 && counts.done === 0;
    applyBtn.textContent = pending > 0
      ? `落地已接受的（还有 ${pending} 条待审批）`
      : '落地已接受的';
  }

  /** 打标落地时的「写什么」开关。 */
  function renderWriteToggles() {
    const row = el('div', 'plugin-stm-ai-assistant-row is-write-toggles');
    row.append(el('span', 'plugin-stm-ai-assistant-note', '落地时写入：'));
    [
      { key: 'writeNote', label: '卡片概览' },
      { key: 'writeTags', label: '已有标签' },
    ].forEach((item) => {
      const box = el('label', 'plugin-stm-ai-assistant-checkbox');
      const input = el('input');
      input.type = 'checkbox';
      input.checked = Boolean(state[item.key]);
      input.dataset.marqueeIgnore = 'true';
      input.addEventListener('change', () => {
        state[item.key] = input.checked;
      });
      box.append(input, el('span', '', item.label));
      row.append(box);
    });
    return row;
  }


  // ---------------------------------------------------------------- 条目

  function renderItems() {
    clear(listHost);
    if (!state.runId) return;
    if (!state.items.length) {
      listHost.append(emptyState('这个作业还没有条目结果。'));
      return;
    }

    const jobType = String(state.job.job_type || '');
    const renderItem = JOB_RENDERERS[jobType] || renderAnalyzeItem;

    state.items.forEach((item) => {
      const result = item.result || {};
      const row = el('div', 'plugin-stm-ai-assistant-review-item');
      row.dataset.status = String(item.status || '');
      // 有 block 级问题的条目单独标出来：批量「全部接受」不会带走它们（§6.2）。
      if (hasBlockIssues(item)) {
        row.classList.add('is-blocked');
        row.dataset.blocked = 'true';
      }

      const head = el('div', 'plugin-stm-ai-assistant-review-head');
      head.append(toneBadge(ITEM_STATUS_LABELS[item.status] || item.status, item.status));
      head.append(el('span', 'plugin-stm-ai-assistant-review-key',
        String(result.card_id || itemKeyOf(item))));
      if (hasBlockIssues(item)) {
        head.append(toneBadge(
          result.held_by_checker ? '检查拦下 · 需单独确认' : '有问题 · 需单独确认',
          'error',
        ));
      }

      const actions = el('div', 'plugin-stm-ai-assistant-review-actions');
      if (item.status === 'awaiting_review') {
        actions.append(
          api.ui.button({
            label: '接受',
            variant: 'secondary',
            onClick: () => reviewOne(item, 'done'),
          }),
          api.ui.button({
            label: '拒绝',
            variant: 'ghost',
            onClick: () => reviewOne(item, 'skipped'),
          }),
        );
      } else if (item.status === 'done') {
        actions.append(api.ui.button({
          label: '改回待审批',
          variant: 'ghost',
          onClick: () => reviewOne(item, 'awaiting_review'),
        }));
      }
      actions.querySelectorAll('button').forEach((node) => {
        node.dataset.marqueeIgnore = 'true';
      });
      head.append(actions);
      row.append(head);

      row.append(renderItem(result, item, jobType));

      if (item.error) {
        row.append(el('div', 'plugin-stm-ai-assistant-warning', String(item.error)));
      }
      listHost.append(row);
    });
  }

  function toneBadge(label, status) {
    const tone = status === 'failed' ? 'danger'
      : (status === 'awaiting_review' ? 'warning'
        : (status === 'done' ? 'success' : 'neutral'));
    return api.ui.badge({ value: label, tone });
  }

  // ---------------------------------------------------------------- 落地

  function renderDetail() {
    clear(detailHost);
    if (!state.lastApply) return;
    const data = state.lastApply;

    if (data.categories !== undefined || data.created_categories) {
      detailHost.append(banner(
        `已创建 ${(data.created_categories || []).length} 个分类、${(data.created_tags || []).length} 个标签`,
        'ok',
      ));
    } else if (data.applied !== undefined) {
      detailHost.append(banner(
        `已落地 ${data.applied} 条 · 已应用过 ${data.skipped || 0} · `
        + `内容已变化 ${data.stale || 0} · 失败 ${data.failed || 0}`,
        data.failed ? 'warn' : 'ok',
      ));
    }

    (data.details || []).slice(0, 12).forEach((detail) => {
      detailHost.append(el('div', 'plugin-stm-ai-assistant-warning',
        String(detail.message || detail.error || detail.warning || '').trim()));
    });
  }

  async function applyAccepted() {
    if (!state.runId) return;
    // 落地前一次确认：`api.ui.confirm` 返回 Promise（宿主模态框），必须 await，
    // 否则「取消」也会照常写入。
    const confirmed = await confirmApply();
    if (!confirmed) return;

    state.busy = true;
    applyBtn.disabled = true;
    try {
      const payload = buildApplyPayload();
      state.lastApply = await client.applyJob(state.runId, payload);
      const data = state.lastApply;
      if (data.applied) {
        api.host.refresh('cards');
        api.host.refresh('worldinfo');
      }
      api.host.toast(
        data.applied !== undefined ? `已落地 ${data.applied} 条` : '已创建结构',
        3200,
        'check',
      );
      renderDetail();
      await refresh();
      context.notifyReview && context.notifyReview();
    } catch (error) {
      clear(detailHost);
      detailHost.append(banner(`落地失败：${error.message}`, 'error'));
    } finally {
      state.busy = false;
      applyBtn.disabled = false;
    }
  }

  /** 落地前一次确认：把「将发生什么」写清楚，而不是点了就写。 */
  async function confirmApply() {
    const jobType = String(state.job.job_type || '');
    const accepted = state.items.filter((item) => item.status === 'done');
    if (!accepted.length) {
      api.host.toast('没有已接受的条目可落地', 2800, 'settings-help-entry');
      return false;
    }

    let action = '';
    if (jobType === 'ai.analyze') {
      const notes = accepted.filter((item) => String((item.result || {}).summary || '').trim()).length;
      const tagCount = state.writeTags ? accepted.length : 0;
      action = `写 ${notes} 张卡片的概览、为 ${tagCount} 张写入已有标签`;
    } else if (jobType === 'ai.wi_rewrite') {
      action = `写回 ${accepted.length} 条世界书条目`;
    } else if (jobType === 'ai.classify') {
      const moves = accepted.filter((item) => (item.result || {}).category).length;
      action = `移动 ${moves} 张卡片到目标分类`;
    } else {
      action = '创建提议里勾选的分类与标签（不动任何卡片）';
    }

    if (typeof api.ui.confirm === 'function') {
      return Boolean(await api.ui.confirm({
        title: '确认落地',
        message: `将${action}。此操作会修改主程序数据。`,
        confirmLabel: '确认写入',
      }));
    }
    return window.confirm(`将${action}。继续？`);
  }

  function buildApplyPayload() {
    const jobType = String(state.job.job_type || '');
    const accepted = state.items
      .filter((item) => item.status === 'done')
      .map((item) => itemKeyOf(item) || String((item.result || {}).card_id || ''))
      .filter(Boolean);

    if (jobType === 'ai.propose' || jobType === 'ai.agent') {
      const result = (state.items[0] || {}).result || {};
      const plan = result.plan || {};
      return {
        items: accepted,
        categories: selectedOf('categories', plan.create_categories || []),
        tags: selectedOf('tags', plan.create_tags || []),
      };
    }
    if (jobType === 'ai.analyze') {
      const payload = {
        items: accepted,
        write: {
          note: state.writeNote,
          tags: state.writeTags,
        },
      };
      // C13：勾选的删除项才提交（没有勾选框时 `selectedOf` 返回 null = 不删）。
      const removals = selectedOf('removal', null);
      if (Array.isArray(removals) && removals.length) {
        payload.remove_tags = removals;
      }
      return payload;
    }
    return { items: accepted };
  }

  /** 勾选式裁剪：UI 上打勾的才落地；没有渲染勾选框时退化为「全部」。 */
  function selectedOf(kind, fallback) {
    const nodes = Array.from(listHost.querySelectorAll(`input[data-${kind}]`));
    if (!nodes.length) return fallback;
    return nodes.filter((node) => node.checked).map((node) => node.dataset[kind]);
  }

  // ---------------------------------------------------------------- 数据

  async function refresh() {
    clear(alertHost);
    await loadRuns();
    if (!state.runId) {
      const last = readLastRun(api, 'analyze');
      state.runId = last || (state.runs[0] && String(state.runs[0].run_id)) || '';
    }
    renderRunBar();
    await loadJob();
  }

  async function loadRuns() {
    try {
      const data = await client.jobs(20);
      state.runs = data.jobs || [];
    } catch (error) {
      state.runs = [];
      alertHost.append(banner(`读取作业列表失败：${error.message}`, 'warn'));
    }
  }

  async function loadJob() {
    if (!state.runId) {
      state.job = {};
      state.items = [];
      renderSummary();
      renderItems();
      renderRunBar();
      return;
    }
    clear(listHost);
    listHost.append(skeleton('读取作业…'));
    try {
      const data = await client.job(state.runId);
      state.job = data.job || {};
      state.items = data.items || [];
    } catch (error) {
      state.job = {};
      state.items = [];
      clear(listHost);
      listHost.append(banner(`读取作业失败：${error.message}`, 'error'));
      return;
    }
    renderSummary();
    renderItems();
    renderRunBar();
  }

  function selectRun(runId) {
    state.runId = String(runId || '');
    loadJob();
  }

  async function reviewOne(item, status) {
    if (!state.runId) return;
    try {
      await client.reviewItem(state.runId, itemKeyOf(item), status);
      item.status = status;
      renderSummary();
      renderItems();
    } catch (error) {
      api.host.toast(`审批失败：${error.message}`, 3600, 'close');
    }
  }

  async function bulkReview(status) {
    if (!state.runId) return;
    let targets = state.items.filter((item) => item.status === 'awaiting_review');
    // §6.2 硬规则 2：「全部接受」只接受**没有问题**的条目。
    // 有 block 级 issue 的必须单独点，避免 0.35 置信度的错误归类被一把带走。
    let heldByChecker = 0;
    if (status === 'done') {
      const clean = targets.filter((item) => !hasBlockIssues(item));
      heldByChecker = targets.length - clean.length;
      targets = clean;
    }
    if (!targets.length) {
      api.host.toast(
        heldByChecker
          ? `${heldByChecker} 条有问题，需要逐条确认`
          : '没有待审批条目',
        3600,
        'close',
      );
      return;
    }
    state.busy = true;
    try {
      const result = await client.reviewItems(
        state.runId,
        targets.map((item) => ({ key: itemKeyOf(item), status })),
      );
      // 批量结果以服务端为准，避免个别失败被本地状态掩盖
      await loadJob();
      if (result.failed.length) {
        api.host.toast(`${result.total - result.failed.length} 条已更新，${result.failed.length} 条失败`, 3600, 'close');
      } else {
        api.host.toast(
          `已批量标记 ${targets.length} 条` + (heldByChecker ? `，跳过 ${heldByChecker} 条有问题的` : ''),
          3000,
          'check',
        );
      }
    } catch (error) {
      api.host.toast(`批量审批失败：${error.message}`, 3600, 'close');
    } finally {
      state.busy = false;
      renderSummary();
    }
  }

  async function retryFailed() {
    if (!state.runId) return;
    try {
      const data = await client.retryJob(state.runId, true);
      if (data.run_id) state.runId = String(data.run_id);
      api.host.toast('已重新入队', 3000, 'check');
      await refresh();
    } catch (error) {
      api.host.toast(`重跑失败：${error.message}`, 3600, 'close');
    }
  }

  return {
    element: root,
    refresh,
    applyContext(ctx) {
      // 宿主已删除 `targetId`（改为 `entryId`）；审批面板只认显式的 runId，
      // 入口 id 与 runId 是两件事，不能互相兜底。
      const runId = String((ctx && ctx.runId) || '');
      if (runId) {
        state.runId = runId;
      }
    },
    focusRun(runId) {
      state.runId = String(runId || '');
    },
    destroy() {
      clear(root);
    },
  };
}

// ---------------------------------------------------------------- 按类型渲染

function renderAnalyzeItem(result) {
  const wrap = el('div', 'plugin-stm-ai-assistant-review-body');
  const summary = String(result.summary || '').trim();
  if (summary) wrap.append(el('p', 'plugin-stm-ai-assistant-review-summary', summary));

  const tags = Array.isArray(result.tags) ? result.tags : [];
  if (tags.length) {
    const row = el('div', 'plugin-stm-ai-assistant-tags');
    tags.forEach((tag) => {
      const name = tag && tag.name ? tag.name : String(tag || '');
      const confidence = tag && tag.confidence != null ? ` ${tag.confidence}` : '';
      row.append(el('span', 'plugin-stm-ai-assistant-tag-chip', `${name}${confidence}`));
    });
    wrap.append(row);
  }

  const suggestions = Array.isArray(result.new_tags) ? result.new_tags : [];
  if (suggestions.length) {
    wrap.append(el(
      'p',
      'plugin-stm-ai-assistant-note',
      `建议新增标签：${suggestions.map((tag) => (tag && tag.name) || tag).join('、')}（进建议队列，不直接写入）`,
    ));
  }

  // C13：删标签是不可逆的，所以默认**不勾**；勾了才会随 apply 一起提交。
  const removals = Array.isArray(result.tag_removals) ? result.tag_removals : [];
  if (removals.length) {
    wrap.append(el(
      'p',
      'plugin-stm-ai-assistant-note',
      '建议删除的现有标签（删标签不可逆，默认不删；勾选后才会移除）',
    ));
    const list = el('div', 'plugin-stm-ai-assistant-checks');
    removals.forEach((entry) => {
      const name = String((entry && entry.name) || entry || '').trim();
      if (!name) return;
      const label = el('label', 'plugin-stm-ai-assistant-checkbox');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = false;
      box.dataset.removal = name;
      box.dataset.marqueeIgnore = 'true';
      const reason = entry && entry.reason ? `：${entry.reason}` : '';
      label.append(box, el('span', '', `${name}${reason}`));
      list.append(label);
    });
    wrap.append(list);
  }
  return wrap;
}

function renderPolishItem(result) {
  const wrap = el('div', 'plugin-stm-ai-assistant-review-body');

  const original = String(result.original || result.source_text || '');
  const candidate = String(result.candidate || '');
  // §6.4 的指标行：原 → 后（±差值），不出现「失败」字样
  const metrics = result.metrics || {};
  const origTokens = Number(metrics.orig_tokens ?? result.original_tokens ?? 0);
  const candTokens = Number(metrics.cand_tokens ?? result.candidate_tokens ?? 0);
  const delta = Number.isFinite(Number(metrics.delta))
    ? Number(metrics.delta)
    : candTokens - origTokens;
  const deltaText = delta === 0 ? '未改，已足够精炼' : (delta > 0 ? `+${delta}，变长` : String(delta));

  wrap.append(statsRow([
    { label: '原 token', value: formatNumber(origTokens) },
    { label: '改写后', value: formatNumber(candTokens) },
    { label: '差值', value: deltaText },
  ]));

  const diff = el('div', 'plugin-stm-ai-assistant-diff');
  const left = el('div', 'plugin-stm-ai-assistant-diff-side is-before');
  left.append(el('div', 'plugin-stm-ai-assistant-diff-title', '原文'));
  left.append(el('pre', 'plugin-stm-ai-assistant-diff-body', original || '（作业未回传原文）'));
  const right = el('div', 'plugin-stm-ai-assistant-diff-side is-after');
  right.append(el('div', 'plugin-stm-ai-assistant-diff-title', '改写后'));
  right.append(el('pre', 'plugin-stm-ai-assistant-diff-body', candidate || '（没有候选文本）'));
  diff.append(left, right);
  wrap.append(diff);

  const dropped = Array.isArray(result.dropped_facts) ? result.dropped_facts : [];
  if (dropped.length) {
    wrap.append(banner(
      `可能丢失的信息（${dropped.length} 项）：${dropped.slice(0, 6).map((item) => String(item)).join('；')}`,
      'warn',
    ));
  }
  // note 级：只显示，永不拦（§6.3）
  const notes = Array.isArray(result.warnings) ? result.warnings : [];
  if (notes.length) {
    wrap.append(banner(`提示：${notes.join('；')}`, 'warn'));
  }
  const issues = Array.isArray(result.issues) ? result.issues : [];
  if (issues.length) {
    wrap.append(banner(
      `检查器拦下（${issues.length} 项，仍可手动接受）：${issues.join('；')}`,
      'error',
    ));
  }
  return wrap;
}

function renderClassifyItem(result) {
  const wrap = el('div', 'plugin-stm-ai-assistant-review-body');
  const from = String(result.current_category || result.card_category || '（当前分类未知）');
  const to = String(result.category || '（无）');
  const confidence = result.confidence != null ? Number(result.confidence) : null;

  const line = el('div', 'plugin-stm-ai-assistant-move');
  line.append(el('span', 'plugin-stm-ai-assistant-move-from', from));
  line.append(el('span', 'plugin-stm-ai-assistant-move-arrow', '→'));
  line.append(el('span', 'plugin-stm-ai-assistant-move-to', to));
  if (confidence != null) {
    line.append(el(
      'span',
      `plugin-stm-ai-assistant-move-confidence${confidence < 0.6 ? ' is-low' : ''}`,
      `置信度 ${confidence}`,
    ));
  }
  wrap.append(line);

  const reason = String(result.reason || '').trim();
  if (reason) wrap.append(el('p', 'plugin-stm-ai-assistant-note', reason));
  const issues = Array.isArray(result.issues) ? result.issues : [];
  if (issues.length) wrap.append(banner(issues.join('；'), 'warn'));
  return wrap;
}

function renderProposeItem(result) {
  const wrap = el('div', 'plugin-stm-ai-assistant-review-body');
  const plan = result.plan || {};

  const categories = Array.isArray(plan.create_categories) ? plan.create_categories : [];
  if (categories.length) {
    wrap.append(el('div', 'plugin-stm-ai-assistant-subtitle', `新建分类（${categories.length}）`));
    const list = el('div', 'plugin-stm-ai-assistant-checks');
    categories.forEach((name) => {
      const box = el('label', 'plugin-stm-ai-assistant-checkbox');
      const input = el('input');
      input.type = 'checkbox';
      input.checked = true;
      input.dataset.categories = String(name);
      input.dataset.marqueeIgnore = 'true';
      box.append(input, el('span', '', String(name)));
      list.append(box);
    });
    wrap.append(list);
  }

  const tags = Array.isArray(plan.create_tags) ? plan.create_tags : [];
  if (tags.length) {
    wrap.append(el('div', 'plugin-stm-ai-assistant-subtitle', `新建标签（${tags.length}）`));
    const list = el('div', 'plugin-stm-ai-assistant-checks');
    tags.forEach((name) => {
      const box = el('label', 'plugin-stm-ai-assistant-checkbox');
      const input = el('input');
      input.type = 'checkbox';
      input.checked = true;
      input.dataset.tags = String(name);
      input.dataset.marqueeIgnore = 'true';
      box.append(input, el('span', '', String(name)));
      list.append(box);
    });
    wrap.append(list);
  }

  const merges = Array.isArray(plan.tag_merges) ? plan.tag_merges : [];
  if (merges.length) {
    wrap.append(el('div', 'plugin-stm-ai-assistant-subtitle', '标签合并建议（需人工处理，不会自动执行）'));
    const list = el('div', 'plugin-stm-ai-assistant-checks');
    merges.forEach((merge) => {
      const from = (merge && merge.from) || '';
      const to = (merge && merge.to) || '';
      list.append(el('div', 'plugin-stm-ai-assistant-note', `${from} → ${to}`));
    });
    wrap.append(list);
  }

  const notes = String(plan.rationale || result.rationale || '').trim();
  if (notes) wrap.append(el('p', 'plugin-stm-ai-assistant-note', notes));

  if (!categories.length && !tags.length && !merges.length) {
    wrap.append(emptyState('提议里没有可落地内容。'));
  }
  return wrap;
}
