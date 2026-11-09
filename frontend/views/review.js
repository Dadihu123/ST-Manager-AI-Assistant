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

import { banner, clear, emptyState, formatNumber, skeleton, statsRow } from '../ui.js';
import { el } from '../dom.js';
import { createManualTagInput } from './manualTags.js';
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
    items: [],
    runs: [],
    filterRunId: '',
    lastApply: null,
    busy: false,
    /** 勾选的删除项：`runId:itemKey -> Set(标签名)`；重渲染（接受/拒绝）后仍保留。 */
    removals: new Map(),
    // 「写什么」在落地时决定：打标任务的概览 / 标签各一个开关。
    writeNote: true,
    writeTags: true,
  };

  const alertHost = el('div', 'plugin-stm-ai-assistant-alert-host');
  const summaryHost = el('div', 'plugin-stm-ai-assistant-review-summary');
  const toolbarHost = el('div', 'plugin-stm-ai-assistant-review-toolbar');
  const listHost = el('div', 'plugin-stm-ai-assistant-review-list');
  const detailHost = el('div', 'plugin-stm-ai-assistant-review-detail');

  root.append(alertHost, summaryHost, toolbarHost, listHost, detailHost);

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
  const applyBtn = api.ui.button({
    label: '落地已接受的',
    variant: 'primary',
    onClick: () => applyAccepted(),
  });
  const refreshBtn = api.ui.button({
    label: '刷新',
    variant: 'ghost',
    onClick: () => refresh(),
  });
  const historyBtn = api.ui.button({
    label: 'AI打标历史记录',
    variant: 'secondary',
    onClick: () => openHistory(),
  });
  historyBtn.classList.add('plugin-stm-ai-assistant-history-btn');
  [acceptAllBtn, rejectAllBtn, applyBtn, refreshBtn, historyBtn].forEach((node) => {
    node.dataset.marqueeIgnore = 'true';
    toolbarHost.append(node);
  });

  function openHistory() {
    if (context && typeof context.openHistory === 'function') {
      context.openHistory();
      return;
    }
    api.host.toast('AI打标历史记录：界面还没接上（下一步做）', 3000, 'check');
  }

  // ---------------------------------------------------------------- 数据

  async function refresh() {
    clear(alertHost);
    clear(listHost);
    listHost.append(skeleton('读取结果…'));
    try {
      const data = await client.reviewQueue(state.filterRunId);
      state.items = Array.isArray(data.items) ? data.items : [];
      state.runs = Array.isArray(data.runs) ? data.runs : [];
    } catch (error) {
      clear(listHost);
      listHost.append(banner(`读取结果失败：${error.message}`, 'error'));
      return;
    }
    renderSummary();
    renderItems();
    renderDetail();
  }

  // ---------------------------------------------------------------- 摘要

  function renderSummary() {
    clear(summaryHost);
    const tally = { awaiting_review: 0, done: 0, failed: 0, skipped: 0 };
    state.items.forEach((item) => {
      if (tally[item.status] !== undefined) tally[item.status] += 1;
    });

    summaryHost.append(statsRow([
      { label: '待审批', value: formatNumber(tally.awaiting_review) },
      { label: '待落地', value: formatNumber(tally.done) },
      { label: '作业', value: formatNumber(state.runs.length) },
    ]));

    if (state.filterRunId) {
      const row = el('div', 'plugin-stm-ai-assistant-row');
      row.append(el(
        'span',
        'plugin-stm-ai-assistant-note',
        `只显示作业 ${state.filterRunId.slice(0, 8)} 的结果`,
      ));
      const showAll = api.ui.button({
        label: '显示全部结果',
        variant: 'ghost',
        onClick: () => {
          state.filterRunId = '';
          refresh();
        },
      });
      showAll.dataset.marqueeIgnore = 'true';
      row.append(showAll);
      summaryHost.append(row);
    }

    if (state.items.some((item) => item.job_type === 'ai.analyze')) {
      summaryHost.append(renderWriteToggles());
    }

    const pending = tally.awaiting_review;
    applyBtn.disabled = state.busy || tally.done === 0;
    applyBtn.textContent = tally.done > 0
      ? `落地已接受的（${tally.done} 条）`
      : '落地已接受的';
    acceptAllBtn.disabled = state.busy || pending === 0;
    rejectAllBtn.disabled = state.busy || pending === 0;
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
    if (!state.items.length) {
      listHost.append(emptyState(
        state.filterRunId
          ? '这个作业没有待处理的结果了。'
          : '没有待处理的结果。作业跑完的结果会自动出现在这里。',
      ));
      const button = state.filterRunId
        ? api.ui.button({
          label: '显示全部结果',
          variant: 'secondary',
          onClick: () => {
            state.filterRunId = '';
            refresh();
          },
        })
        : api.ui.button({
          label: '打开 AI打标历史记录',
          variant: 'secondary',
          onClick: () => openHistory(),
        });
      button.dataset.marqueeIgnore = 'true';
      listHost.append(button);
      return;
    }
    state.items.forEach((item) => listHost.append(renderCard(item)));
  }

  function renderCard(item) {
    const jobType = String(item.job_type || '');
    const result = item.result || {};
    const row = el('div', 'plugin-stm-ai-assistant-review-item');
    row.dataset.status = String(item.status || '');
    if (hasBlockIssues(item)) {
      row.classList.add('is-blocked');
      row.dataset.blocked = 'true';
    }

    const head = el('div', 'plugin-stm-ai-assistant-review-head');
    const label = item.status === 'done'
      ? '已接受 · 待落地'
      : (ITEM_STATUS_LABELS[item.status] || item.status);
    head.append(toneBadge(label, item.status));
    head.append(el(
      'span',
      'plugin-stm-ai-assistant-review-key',
      String(result.card_id || item.key || ''),
    ));
    const task = TASKS[String(item.task || '')];
    head.append(el(
      'span',
      'plugin-stm-ai-assistant-note',
      `${task ? task.label : jobType} · ${String(item.run_id || '').slice(0, 8)}`,
    ));
    // 有 block 级问题的条目单独标出来：批量「全部接受」不会带走它们。
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
      // 接受后**不提供逐条落地**：统一用顶部「落地已接受的」。
      // 这里保留按钮外观但置灰、不可点（避免误以为可以单条落地）。
      const landBtn = api.ui.button({
        label: '落地这条',
        variant: 'primary',
        onClick: () => {},
      });
      landBtn.disabled = true;
      landBtn.title = '用上方「落地已接受的」统一落地';
      actions.append(
        landBtn,
        api.ui.button({
          label: '改回待审批',
          variant: 'ghost',
          onClick: () => reviewOne(item, 'awaiting_review'),
        }),
      );
    }
    actions.querySelectorAll('button').forEach((node) => {
      node.dataset.marqueeIgnore = 'true';
    });
    head.append(actions);
    row.append(head);

    const renderItem = JOB_RENDERERS[jobType] || renderAnalyzeItem;
    row.append(renderItem(result, item, jobType));

    // 勾选的「建议删除的标签」要能对应回这一条：落地时按条目/作业收集。
    // 选择存在 `state.removals` 里 —— 「接受」会重渲染卡片，只放 DOM 会丢。
    const itemKey = itemKeyOf(item);
    row.dataset.itemKey = itemKey;
    const removalKey = `${item.run_id || ''}:${itemKey}`;
    row.querySelectorAll('input').forEach((box) => {
      if (!box.dataset || !box.dataset.removal) return;
      const name = String(box.dataset.removal || '');
      const selected = state.removals.get(removalKey);
      box.checked = Boolean(selected && selected.has(name));
      box.addEventListener('change', () => {
        if (!state.removals.has(removalKey)) state.removals.set(removalKey, new Set());
        const picked = state.removals.get(removalKey);
        if (box.checked) picked.add(name);
        else picked.delete(name);
      });
    });

    if (jobType === 'ai.analyze' && itemKey) {
      row.append(createManualTagInput({
        api,
        note: '手动添加标签（直接写入卡片）',
        onAdd: (names) => client.saveManualTags(item.run_id, itemKeyOf(item), names),
      }));
    }

    if (item.error) {
      row.append(el('div', 'plugin-stm-ai-assistant-warning', String(item.error)));
    }
    return row;
  }

  function toneBadge(label, status) {
    const tone = status === 'failed' ? 'danger'
      : (status === 'awaiting_review' ? 'warning'
        : (status === 'done' ? 'success' : 'neutral'));
    return api.ui.badge({ value: label, tone });
  }

  // ---------------------------------------------------------------- 审批与落地

  async function reviewOne(item, status) {
    if (state.busy) return;
    try {
      await client.reviewItem(item.run_id, itemKeyOf(item), status);
      // 接受/拒绝只改本地状态，**不重拉队列**（重拉要扫几十个作业，很慢）；
      // 等真正落地时再刷新，把已落地/已跳过的条目移出队列。
      item.status = status;
      renderSummary();
      renderItems();
    } catch (error) {
      api.host.toast(`审批失败：${error.message}`, 3600, 'close');
    }
  }

  async function bulkReview(status) {
    const targets = state.items.filter((item) => item.status === 'awaiting_review');
    let held = 0;
    let picked = targets;
    if (status === 'done') {
      picked = targets.filter((item) => !hasBlockIssues(item));
      held = targets.length - picked.length;
    }
    if (!picked.length) {
      api.host.toast(
        held ? `${held} 条有问题，需要逐条确认` : '没有待审批的结果',
        3600,
        'close',
      );
      return;
    }

    const groups = new Map();
    picked.forEach((item) => {
      const runId = String(item.run_id || '');
      if (!groups.has(runId)) groups.set(runId, []);
      groups.get(runId).push({ key: itemKeyOf(item), status });
    });

    state.busy = true;
    let failed = 0;
    try {
      for (const [runId, updates] of groups) {
        try {
          const result = await client.reviewItems(runId, updates);
          failed += (result.failed || []).length;
        } catch (error) {
          failed += updates.length;
        }
      }
    } finally {
      state.busy = false;
    }
    api.host.toast(
      `已更新 ${picked.length - failed} 条` + (held ? `，跳过 ${held} 条有问题的` : ''),
      3600,
      failed ? 'close' : 'check',
    );
    if (failed) {
      // 有失败才重拉，保证服务端状态为准；全部成功只更新本地状态，避免卡顿。
      await refresh();
    } else {
      picked.forEach((item) => {
        item.status = status;
      });
      renderSummary();
      renderItems();
    }
  }

  async function applyAccepted() {
    const done = state.items.filter((item) => item.status === 'done');
    if (!done.length) {
      api.host.toast('没有已接受的结果', 2800, 'close');
      return;
    }
    const confirmed = await confirmApply(done);
    if (!confirmed) return;

    const groups = new Map();
    done.forEach((item) => {
      const runId = String(item.run_id || '');
      if (!groups.has(runId)) groups.set(runId, []);
      groups.get(runId).push(itemKeyOf(item));
    });

    // 勾选的「删除标签」按条目找到对应选择，再按作业汇总 —— 落地请求里要带上
    // `remove_tags`，否则勾了也不会删。选择存在 `state.removals`（重渲染不丢）。
    const removalsByRun = new Map();
    done.forEach((item) => {
      const names = state.removals.get(`${item.run_id || ''}:${itemKeyOf(item)}`);
      if (!names || !names.size) return;
      const runId = String(item.run_id || '');
      if (!removalsByRun.has(runId)) removalsByRun.set(runId, new Set());
      names.forEach((name) => removalsByRun.get(runId).add(name));
    });

    state.busy = true;
    applyBtn.disabled = true;
    applyBtn.textContent = `落地中… 0/${done.length}`;
    // 落地要逐张写卡（可能几十张），必须先给反馈，否则就是「点了没反应」
    const progressNode = el(
      'div',
      'plugin-stm-ai-assistant-warning',
      `正在落地 0/${done.length} 条，请稍候…`,
    );
    clear(detailHost);
    detailHost.append(progressNode);

    let applied = 0;
    let failed = 0;
    let skipped = 0;
    let stale = 0;
    let handled = 0;
    const details = [];
    try {
      for (const [runId, keys] of groups) {
        try {
          const body = {
            items: keys,
            write: { note: state.writeNote, tags: state.writeTags },
          };
          const removals = removalsByRun.get(runId);
          if (removals && removals.size) body.remove_tags = [...removals];
          const data = await client.applyJob(runId, body);
          applied += Number(data.applied || 0);
          skipped += Number(data.skipped || 0);
          stale += Number(data.stale || 0);
          failed += Number(data.failed || 0);
          (data.details || []).forEach((detail) => details.push(detail));
        } catch (error) {
          failed += keys.length;
          details.push({ error: error.message });
        }
        handled += keys.length;
        progressNode.textContent = `正在落地 ${Math.min(handled, done.length)}/${done.length} 条…`;
        applyBtn.textContent = `落地中… ${Math.min(handled, done.length)}/${done.length}`;
      }
    } finally {
      state.busy = false;
    }

    state.lastApply = { applied, skipped, stale, failed, details };
    api.host.toast(`已落地 ${applied} 条`, 3200, 'check');
    if (applied) {
      api.host.refresh('cards');
      api.host.refresh('worldinfo');
      if (typeof context.notifyReview === 'function') context.notifyReview();
    }
    await refresh();
    // 主程序网格的备注图标读卡片缓存；只有主程序自己的完整保存会刷新它。
    // 这里让主程序按「当前卡片数据 + 新备注」保存一次（后端会先快照，失败自动回滚）。
    const cardIds = done
      .map((item) => String((item.result || {}).card_id || itemKeyOf(item)))
      .filter(Boolean);
    if (cardIds.length) {
      // 不阻塞落地：后台让主程序按完整数据保存一次，刷新备注图标缓存
      client.syncCardNoteCache({
        card_ids: cardIds,
        base_url: (typeof window !== 'undefined' && window.location && window.location.origin) || '',
      }).then(() => {
        try {
          api.host.refresh('cards');
        } catch (error) {
          console.warn('[stm-ai] 刷新卡片列表失败:', error);
        }
      }).catch((error) => {
        console.warn('[stm-ai] 刷新卡片备注缓存失败:', error);
      });
    }
    // 卡片网格可能还在模态层后面；关闭面板时 plugin.js 还会再刷一次，
    // 这里再补一个延迟刷新，覆盖宿主缓存提交较慢的情况。
    setTimeout(() => {
      try {
        api.host.refresh('cards');
      } catch (error) {
        console.warn('[stm-ai] 刷新卡片列表失败:', error);
      }
    }, 600);
  }

  /** 落地前一次确认：把「将发生什么」写清楚，而不是点了就写。 */
  async function confirmApply(done) {
    const notes = done.filter((item) => String((item.result || {}).summary || '').trim()).length;
    const tagCount = state.writeTags ? done.length : 0;
    let removalCount = 0;
    const uniqueRemovals = new Set();
    done.forEach((item) => {
      const names = state.removals.get(`${item.run_id || ''}:${itemKeyOf(item)}`);
      if (!names) return;
      names.forEach((name) => uniqueRemovals.add(`${item.run_id || ''}|${name}`));
    });
    removalCount = uniqueRemovals.size;
    const removalPart = removalCount ? `并删除 ${removalCount} 个勾选的标签。` : '';
    const message = `将写 ${notes} 张卡片的概览、为 ${tagCount} 张写入已有标签，`
      + `${removalPart}此操作会修改主程序数据。`;
    if (typeof api.ui.confirm === 'function') {
      return Boolean(await api.ui.confirm({
        title: '确认落地',
        message,
        confirmLabel: '确认写入',
      }));
    }
    return window.confirm(`${message}继续？`);
  }

  function renderDetail() {
    clear(detailHost);
    if (!state.lastApply) return;
    const data = state.lastApply;
    detailHost.append(banner(
      `已落地 ${data.applied} 条 · 已应用过 ${data.skipped || 0} · `
      + `内容已变化 ${data.stale || 0} · 失败 ${data.failed || 0}`,
      data.failed ? 'warn' : 'ok',
    ));
    (data.details || []).slice(0, 12).forEach((detail) => {
      const text = String(detail.message || detail.error || detail.warning || '').trim();
      if (text) detailHost.append(el('div', 'plugin-stm-ai-assistant-warning', text));
    });

    // 标签合并结果：被「冻结分类」（排除 / 隔离）挡下的必须让用户看见，
    // 否则他勾了合并、界面却说成功，实际那几条压根没动。
    const merges = Array.isArray(data.merges_executed) ? data.merges_executed : [];
    merges.forEach((merge) => {
      const skipped = Array.isArray(merge.skipped_frozen) ? merge.skipped_frozen : [];
      if (merge.moved) {
        detailHost.append(banner(
          `已合并 ${merge.moved} 张卡片：${(merge.from || []).join('、')} → ${merge.to}`,
          'ok',
        ));
      }
      if (skipped.length) {
        detailHost.append(banner(
          `未合并 ${skipped.join('、')}：属于排除/隔离分类（冻结），插件不会动这些标签`,
          'warn',
        ));
      }
      if (merge.error && merge.error !== 'invalid_merge') {
        const label = merge.error === 'all_frozen'
          ? '全部落在冻结分类里，未执行合并'
          : `合并失败：${merge.error}`;
        detailHost.append(banner(label, 'warn'));
      }
    });
  }

  return {
    element: root,
    refresh,
    applyContext(ctx) {
      const runId = String((ctx && ctx.runId) || '');
      if (runId) {
        state.filterRunId = runId;
        refresh();
      }
    },
    focusRun(runId) {
      state.filterRunId = String(runId || '');
    },
    destroy() {
      clear(root);
    },
  };
}

// ---------------------------------------------------------------- 按类型渲染

export function renderAnalyzeItem(result) {
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
    wrap.append(el(
      'div',
      'plugin-stm-ai-assistant-subtitle',
      '标签合并建议（勾选后点「确认写入」才执行：把来源标签下的卡改挂到目标标签，再删来源）',
    ));
    const list = el('div', 'plugin-stm-ai-assistant-checks');
    merges.forEach((merge, index) => {
      const rawFrom = (merge && merge.from) || '';
      const from = Array.isArray(rawFrom) ? rawFrom.join('、') : String(rawFrom);
      const to = String((merge && merge.to) || '');
      const reason = merge && merge.reason ? `：${merge.reason}` : '';
      const label = el('label', 'plugin-stm-ai-assistant-checkbox');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = false;              // 删标签不可逆，默认不勾
      box.dataset.merge = String(index);
      box.dataset.marqueeIgnore = 'true';
      label.append(box, el('span', '', `${from} → ${to}${reason}`));
      list.append(label);
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
