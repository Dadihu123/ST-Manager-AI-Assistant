/**
 * 任务面板 —— 四个能力共用的一套面板骨架。
 *
 * 面板自上而下只有五块（见 docs/2026-10-19-task-centric-redesign.md §1）：
 *
 *   ① 作用域横幅（这是什么范围、多少条、能不能改）
 *   ② 参数（≤4 项，与任务相关）
 *   ③ 预览（可选：看 Prompt / 看采样）
 *   ④ 主按钮 + 内联进度
 *   ⑤ 结果入口（跳到「结果与审批」，并按 runId 定位）
 *
 * 以前这些散在 workbench 的 7 个段落里，每段各自管作用域、各自提交、
 * 只有打标订阅了进度。现在统一在这里：任何任务都得到一致行为。
 */

import {
  actionRow,
  banner,
  clear,
  el,
  emptyState,
  estimateRow,
  formatNumber,
  jobStatusLabel,
  normalizeProgress,
  optionsBlock,
  previewSection,
  progressBlock,
  runTaskJob,
  scopeBar,
  scopePayload,
  readLastRun,
  skeleton,
  statsRow,
} from '../ui.js';
import { checkbox, createUi, field, formatTime, input } from '../dom.js';
import {
  ScopeError,
  describeScope,
  normalizeTarget,
  readHostSelection,
  resolveScope,
} from '../scope.js';
import { getTask, taskActionLabel } from '../tasks.js';

const APPLY_MODES = [
  { value: 'review', label: '逐条审批后写入' },
  { value: 'auto', label: '分析完直接写入' },
];

/**
 * @param {object} api 插件 API 门面
 * @param {object} client 后端调用封装
 * @param {object} context 跨视图运行上下文
 * @param {string} taskId 任务 id（`tasks.js` 的键）
 */
export function createTaskPanel(api, client, context, taskId) {
  const task = getTask(taskId);
  const root = el('div', `plugin-stm-ai-assistant-view is-task is-task-${taskId}`);
  const ui = createUi(api);
  const { select } = ui;

  const state = {
    scope: null,
    scopeError: '',
    scopeLoading: false,
    providers: [],
    profiles: [],
    providerId: context.providerId || '',
    model: context.model || '',
    profileId: '',
    applyMode: 'review',
    running: false,
    runId: '',
    /** 用户点「仍然重新打标」时置 true：这一次提交绕过内容未变化判定。 */
    forceOnce: false,
    lastRunId: readLastRun(api, taskId),
    /** 由 `onOpen` 注入；`null` 表示本面板自己还没收到作用域。 */
    target: null,
  };

  // ---------------------------------------------------------------- 骨架

  const scopeHost = el('div', 'plugin-stm-ai-assistant-scope-host');
  const alertHost = el('div', 'plugin-stm-ai-assistant-alert-host');
  const optionsHost = el('div', 'plugin-stm-ai-assistant-options');
  const previewHost = el('div', 'plugin-stm-ai-assistant-preview-host');
  // 面板高度是固定的（宿主 surface 尺寸），没有占位时下半屏是一大片空白，
  // 看起来像「内容没加载出来」。这里给一句说明，点预览后会被真实内容替换。
  previewHost.append(el(
    'div',
    'plugin-stm-ai-assistant-preview-hint',
    '点「预览」可先确认这次会读到哪些内容 —— 不调用模型，也不写回。',
  ));
  const actionsHost = el('div', 'plugin-stm-ai-assistant-actions-host');
  const progress = progressBlock({ onCancel: () => cancelRun() });
  const resultHost = el('div', 'plugin-stm-ai-assistant-result-host');

  root.append(scopeHost, alertHost, optionsHost, previewHost, actionsHost, progress.node, resultHost);

  // 参数区：provider / model / profile / apply_mode（四个任务都有）
  const { node: optionsNode, body: optionsBody } = optionsBlock('运行参数');
  optionsHost.append(optionsNode);

  const providerSelect = select([], '', () => {
    state.providerId = providerSelect.value;
    context.providerId = state.providerId;
    syncProviderModel();
    renderProfileOptions();
  });
  const modelInput = input('', 'text', '留空则用 provider 的默认模型');
  modelInput.addEventListener('input', () => {
    state.model = modelInput.value.trim();
    context.model = state.model;
  });
  const profileSelect = select([{ value: '', label: '（按任务自动匹配）' }], '', () => {
    state.profileId = profileSelect.value;
  });
  const applySelect = select(APPLY_MODES, 'review', () => {
    state.applyMode = applySelect.value;
  });

  const optionsGrid = el('div', 'plugin-stm-ai-assistant-grid');
  optionsGrid.append(
    field('Provider', providerSelect, '缺省用插件设置里的默认 Provider'),
    field('模型', modelInput, ''),
    field('提示词档案', profileSelect, '只列出本任务可用的档案'),
    field('写回方式', applySelect, ''),
  );
  optionsBody.append(optionsGrid);

  // 任务专属参数挂在同一块里，避免再出现「第二段参数」
  const taskOptionsHost = el('div', 'plugin-stm-ai-assistant-grid');
  optionsBody.append(taskOptionsHost);

  const actions = actionRow();
  const submitBtn = api.ui.button({ label: '开始', variant: 'primary', onClick: () => submit() });
  submitBtn.dataset.marqueeIgnore = 'true';
  const previewBtn = api.ui.button({
    label: '预览',
    variant: 'secondary',
    onClick: () => runPreview(),
  });
  previewBtn.dataset.marqueeIgnore = 'true';
  const openResultBtn = api.ui.button({
    label: '查看结果',
    variant: 'secondary',
    onClick: () => openReview(state.runId || state.lastRunId),
  });
  openResultBtn.dataset.marqueeIgnore = 'true';
  openResultBtn.hidden = true;
  // AI打标历史记录：入口先放好，点开的具体界面后续再接（`context.openHistory` 钩子）。
  const historyBtn = api.ui.button({
    label: 'AI打标历史记录',
    variant: 'secondary',
    onClick: () => openHistory(),
  });
  historyBtn.dataset.marqueeIgnore = 'true';
  historyBtn.classList.add('plugin-stm-ai-assistant-history-btn');
  actions.append(submitBtn, previewBtn, openResultBtn, historyBtn);
  actionsHost.append(actions);

  function openHistory() {
    if (context && typeof context.openHistory === 'function') {
      context.openHistory(taskId);
      return;
    }
    api.host.toast('AI打标历史记录：界面还没接上（下一步做）', 3000, 'check');
  }

  // ---------------------------------------------------------------- 任务专属参数

  const extras = buildTaskOptions(taskId, taskOptionsHost, state, client, ui);

  // ---------------------------------------------------------------- 作用域

  async function loadScope(target, { keepLastRun = false } = {}) {
    state.target = target || null;
    state.scopeError = '';
    state.scope = null;

    if (!target) {
      renderScope();
      return;
    }

    state.scopeLoading = true;
    renderScope();
    try {
      // 世界书精简：cards 视图的分类作用域要转成「这些卡的内嵌世界书」，
      // 否则会把卡片当世界书来源传给后端。语义由任务决定，不靠入口 id 猜。
      const as = taskId === 'polish' && target.mode === 'cards' ? 'embedded-books' : 'cards';
      const scope = await resolveScope(api, target, { as });
      state.scope = scope;
      if (scope && scope.empty) {
        state.scopeError = `${scope.label} 下没有可处理的内容`;
      }
    } catch (error) {
      state.scope = null;
      state.scopeError = error instanceof ScopeError
        ? error.message
        : `读取作用域失败：${error.message}`;
    } finally {
      state.scopeLoading = false;
      renderScope();
      renderActions();
      if (!keepLastRun) renderResultHint();
    }
  }

  function renderScope() {
    clear(scopeHost);
    const actionsList = [];
    if (state.scopeError && !state.scopeLoading) {
      const retry = api.ui.button({
        label: '重试',
        variant: 'ghost',
        onClick: () => loadScope(state.target),
      });
      retry.dataset.marqueeIgnore = 'true';
      actionsList.push(retry);
    }
    scopeHost.append(scopeBar({
      scope: state.scope,
      error: state.scopeError,
      loading: state.scopeLoading,
      hint: scopeHint(),
      onChange: () => openScopePicker(),
    }));
  }

  function scopeHint() {
    if (state.scopeLoading) return '';
    if (state.scope && state.scope.truncated) {
      // 宿主枚举有封顶：分类太大时只取到前若干本。绝不能当成完整分类去跑。
      const total = Number(state.scope.total || 0);
      return `该分类共有 ${total || '更多'} 本世界书，本次只取到 ${state.scope.count} 本（已到上限）。`
        + '请改用批量选中分批处理，或先选子分类。';
    }
    if (state.scope) {
      if (state.scope.huge) {
        return '条目较多，建议先小范围试跑；作业可分次提交。';
      }
      return taskId === 'polish'
        ? '将对上述世界书里勾选的条目逐一改写，结果在「结果与审批」里逐条确认。'
        : '提交后会在本面板显示进度，完成后跳到「结果与审批」逐条确认。';
    }
    if (taskId === 'propose') return '这是整库任务，不需要选择作用域。';
    return '点右侧「更改」选择要处理的内容，或从右键菜单 / 批量菜单直接进入。';
  }

  // ---------------------------------------------------------------- 作用域选择器

  let pickerOpen = false;
  const pickerHost = el('div', 'plugin-stm-ai-assistant-picker-host');
  root.insertBefore(pickerHost, scopeHost.nextSibling);

  function openScopePicker() {
    pickerOpen = !pickerOpen;
    clear(pickerHost);
    if (!pickerOpen) return;

    const panel = el('div', 'plugin-stm-ai-assistant-picker');
    const tabs = el('div', 'plugin-stm-ai-assistant-picker-tabs');
    const body = el('div', 'plugin-stm-ai-assistant-picker-body');
    panel.append(tabs, body);

    const renderHostSelection = () => {
      clear(body);
      const host = readHostSelection(api);
      if (!host.ids.length) {
        body.append(emptyState('主程序当前没有勾选任何内容。'));
        return;
      }
      body.append(el(
        'p',
        'plugin-stm-ai-assistant-picker-note',
        `主程序当前勾选 ${host.ids.length} 项（视图：${host.mode || '未知'}）。`,
      ));
      const use = api.ui.button({
        label: `使用这 ${host.ids.length} 项`,
        variant: 'primary',
        onClick: () => {
          pickerOpen = false;
          clear(pickerHost);
          loadScope({ kind: 'selection', mode: host.mode || 'cards', ids: host.ids });
        },
      });
      use.dataset.marqueeIgnore = 'true';
      body.append(use);
    };

    const renderByCategory = () => {
      clear(body);
      const pathInput = input('', 'text', '分类路径，留空表示根目录（全库）');
      const search = input('', 'text', '搜索卡片名');
      const list = el('div', 'plugin-stm-ai-assistant-picker-list');
      const confirm = api.ui.button({
        label: '按该分类确定作用域',
        variant: 'primary',
        onClick: () => {
          pickerOpen = false;
          clear(pickerHost);
          loadScope({ kind: 'folder', mode: 'cards', path: pathInput.value.trim() });
        },
      });
      confirm.dataset.marqueeIgnore = 'true';
      body.append(field('分类', pathInput), field('搜索（可选）', search), confirm, list);

      const runSearch = async () => {
        clear(list);
        list.append(skeleton('搜索中…'));
        try {
          const query = search.value.trim();
          const data = await client.cards(query);
          const items = data.items || data.cards || [];
          clear(list);
          if (!items.length) {
            list.append(emptyState('没有匹配的卡片。'));
            return;
          }
          items.slice(0, 50).forEach((card) => {
            const line = el('label', 'plugin-stm-ai-assistant-picker-item');
            line.append(el('span', '', `${card.name || card.id} · ${card.category || '根目录'}`));
            line.addEventListener('click', () => {
              pickerOpen = false;
              clear(pickerHost);
              loadScope({ kind: 'card', mode: 'cards', id: String(card.id) });
            });
            list.append(line);
          });
        } catch (error) {
          clear(list);
          list.append(banner(`搜索失败：${error.message}`, 'error'));
        }
      };
      search.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') runSearch();
      });
      runSearch();
    };

    const tabsDef = [
      { id: 'host', label: '主程序选中项', render: renderHostSelection },
      { id: 'category', label: '按分类 / 搜索', render: renderByCategory },
    ];

    let active = 'host';
    const buttons = {};
    tabsDef.forEach((tab) => {
      const button = el('button', 'plugin-stm-ai-assistant-picker-tab', tab.label);
      button.type = 'button';
      button.dataset.marqueeIgnore = 'true';
      button.addEventListener('click', () => {
        active = tab.id;
        Object.entries(buttons).forEach(([id, node]) => {
          node.classList.toggle('is-active', id === active);
        });
        tab.render();
      });
      buttons[tab.id] = button;
      tabs.append(button);
    });
    buttons.host.classList.add('is-active');
    tabsDef[0].render();

    pickerHost.append(panel);
  }

  // ---------------------------------------------------------------- 预览

  async function runPreview() {
    clear(previewHost);
    if (!state.scope && taskId !== 'propose') {
      previewHost.append(banner('请先确定作用域', 'warn'));
      return;
    }
    previewHost.append(skeleton('正在生成预览…'));
    try {
      const payload = extras.previewPayload ? extras.previewPayload(state) : {};
      const data = await previewRequest(payload);
      clear(previewHost);
      previewHost.append(renderPreviewData(data));
    } catch (error) {
      clear(previewHost);
      previewHost.append(banner(`预览失败：${error.message}`, 'error'));
    }
  }

  async function previewRequest(payload) {
    if (taskId === 'analyze') {
      const cardId = firstCardId(state);
      return client.preview({
        card_id: cardId,
        provider_id: state.providerId,
        model: state.model,
        profile_id: state.profileId,
        ...payload,
      });
    }
    if (taskId === 'polish') {
      return client.listWorldbookEntries(payload);
    }
    if (taskId === 'propose') {
      return client.previewProposalSample(payload);
    }
    return { note: '该任务没有独立预览，直接提交即可。' };
  }

  function renderPreviewData(data) {
    const wrap = el('div', 'plugin-stm-ai-assistant-preview');
    if (data && data.messages) {
      const head = statsRow([
        { label: '档案', value: String((data.profile || {}).name || data.profile_id || '-') },
        { label: '模型', value: String((data.provider || {}).model || '-') },
        { label: 'prompt', value: `${formatNumber((data.tokens || {}).total || 0)} token` },
      ]);
      wrap.append(head);
      const open = api.ui.button({
        label: '查看完整 Prompt',
        variant: 'secondary',
        onClick: () => context.openPromptPreview(data.messages || []),
      });
      open.dataset.marqueeIgnore = 'true';
      wrap.append(open);
      return previewSection({ primary: wrap });
    }
    if (data && Array.isArray(data.items)) {
      wrap.append(statsRow([
        { label: '可选条目', value: formatNumber(data.items.length) },
        { label: '总计 token', value: formatNumber((data.tokens || {}).total || 0) },
      ]));
      extras.renderPreview && extras.renderPreview(wrap, data);
      return previewSection({ primary: wrap });
    }
    if (data && data.sample) {
      wrap.append(statsRow([
        { label: '采样张数', value: formatNumber(data.sample.count || 0) },
        { label: '采样 token', value: formatNumber(data.sample.tokens || 0) },
      ]));
      return previewSection({ primary: wrap });
    }
    wrap.append(el('p', 'plugin-stm-ai-assistant-note', data.note || '预览已就绪。'));
    return previewSection({ primary: wrap });
  }

  // ---------------------------------------------------------------- 提交

  async function submit() {
    if (state.scope && state.scope.truncated) {
      clear(alertHost);
      alertHost.append(banner(
        '作用域不完整：该分类的世界书数量超过单次枚举上限。请缩小范围（选子分类或改用批量选中）再提交。',
        'error',
      ));
      return;
    }
    if (state.running) return;
    if (!state.scope && taskId !== 'propose') {
      api.host.toast('请先确定作用域', 2400, 'close');
      return;
    }
    if (!state.providerId) {
      api.host.toast('请先在「设置」里配置 Provider', 3200, 'settings-help-entry');
      return;
    }

    clear(previewHost);
    clear(resultHost);
    state.running = true;
    submitBtn.disabled = true;
    progress.show();
    progress.setRunning(true);
    progress.update({ percent: 0, completed: 0, total: state.scope ? state.scope.count : 1, message: '提交中…' });

    await runTaskJob({
      api,
      taskId,
      submit: () => extras.submit(state, {
        provider_id: state.providerId,
        model: state.model,
        profile_id: state.profileId,
        apply_mode: state.applyMode,
        ...scopePayload(state.scope),
      }),
      onProgress(event) {
        const total = event.total || (state.scope ? state.scope.count : 1);
        progress.update({
          percent: event.percent,
          completed: event.completed,
          total,
          failed: event.failed,
          message: jobStatusLabel(event.status),
        });
        // 同步到入口：宿主的批量菜单 / 右键菜单会画进度条，
        // 用户在别的落点也能看到「这个任务正在跑」。
        context.setTaskProgress && context.setTaskProgress(taskId, { done: event.completed, total });
      },
      onDone({ runId, job, response }) {
        state.running = false;
        state.runId = runId;
        submitBtn.disabled = false;
        progress.setRunning(false);
        const status = String((job && job.status) || 'completed');
        const counts = normalizeProgress(job);
        progress.update({
          percent: 100,
          completed: counts.total || state.scope?.count || 1,
          total: counts.total || state.scope?.count || 1,
          failed: counts.failed,
          message: jobStatusLabel(status),
        });
        openResultBtn.hidden = false;
        renderResultHint();
        // 部分卡片被跳过时必须说出来：否则用户以为「全都处理了」，
        // 回头发现少了卡还以为是作业丢了（内容未变化 / 读不到会走这条路）。
        const skippedCount = Number((response && response.skipped) || 0);
        if (skippedCount > 0) {
          resultHost.append(banner(
            `另有 ${formatNumber(skippedCount)} 张卡片没有提交（内容未变化或读取失败）。`,
            'warn',
          ));
        }
        context.setTaskProgress && context.setTaskProgress(taskId, null);
        context.notifyReview && context.notifyReview();
        if (status === 'completed') api.host.toast('作业已完成，去「结果与审批」确认', 3200, 'check');
        else if (status === 'failed') api.host.toast('作业失败，详情见进度', 3600, 'close');
      },
      onError(error, info) {
        state.running = false;
        submitBtn.disabled = false;
        progress.hide();
        context.setTaskProgress && context.setTaskProgress(taskId, null);
        clear(resultHost);
        // 空提交是幂等判定 / 读不到卡片的结果，不是「提交失败」：
        // 给出原因明细和「仍然重新打标」的出口，别让用户对着红条无从下手。
        if (info && info.empty) {
          resultHost.append(renderNothingToProcess(error));
          return;
        }
        resultHost.append(banner(`提交失败：${error.message}`, 'error'));
      },
    });
  }

  function renderResultHint() {
    clear(resultHost);
    const runId = state.runId || state.lastRunId;
    if (!runId) return;
    const bar = el('div', 'plugin-stm-ai-assistant-result-hint');
    bar.append(el('span', '', `最近一次作业：${runId.slice(0, 8)}`));
    const open = api.ui.button({
      label: '打开结果',
      variant: 'secondary',
      onClick: () => openReview(runId),
    });
    open.dataset.marqueeIgnore = 'true';
    bar.append(open);
    resultHost.append(bar);
    openResultBtn.hidden = false;
  }

  /**
   * 「这次没有可提交的卡片」的说明块。
   *
   * 后端把原因拆成 `unchanged` / `card_unreadable` 两类返回：
   * - 内容未变化时给一条「仍然重新打标」的出口 —— 用户已经选好了卡再点开始，
   *   那就是明确想跑，不该被幂等判定拦死（原来只能去猜「取消勾选跳过…」）；
   * - 读取失败时说明是文件层面的问题，让用户回去查路径，而不是反复重试。
   */
  function renderNothingToProcess(error) {
    const payload = (error && error.payload) || {};
    const details = Array.isArray(payload.details) ? payload.details : [];
    const reasons = (payload.reasons && typeof payload.reasons === 'object') ? payload.reasons : {};
    const unchanged = Number(reasons.unchanged || 0);

    const actions = [];
    if (unchanged > 0 && taskId === 'analyze') {
      const retry = api.ui.button({
        label: '仍然重新打标',
        variant: 'primary',
        onClick: () => retryWithForce(),
      });
      retry.dataset.marqueeIgnore = 'true';
      actions.push(retry);
    }

    const wrap = el('div', 'plugin-stm-ai-assistant-nothing-to-process');
    wrap.append(banner(
      String((error && error.message) || '没有需要处理的卡片'),
      unchanged > 0 ? 'warn' : 'error',
      actions,
    ));
    details.slice(0, 10).forEach((item) => {
      wrap.append(el(
        'div',
        'plugin-stm-ai-assistant-note',
        `${item.card_id || '(未知卡片)'} · ${skipReasonLabel(item)}`,
      ));
    });
    if (details.length > 10) {
      wrap.append(el(
        'div',
        'plugin-stm-ai-assistant-note',
        `……另有 ${details.length - 10} 张未列出`,
      ));
    }
    return wrap;
  }

  /** 跳过原因 → 一句人话（后端 reason 是稳定标识，不直接当文案）。 */
  function skipReasonLabel(item) {
    const reason = String((item && item.reason) || '');
    if (reason === 'unchanged') {
      const parts = ['内容未变化'];
      const who = [item.profile_id, item.model].filter(Boolean).join(' / ');
      if (who) parts.push(`上次：${who}`);
      const at = formatTime(Number(item.analyzed_at || 0));
      if (at && at !== '-') parts.push(at);
      return parts.join(' · ');
    }
    if (reason === 'card_unreadable') return '读取失败（文件可能已移动 / 改名 / 损坏）';
    return reason || '已跳过';
  }

  /** 这一次提交绕过「内容未变化」判定（`force` 只对本次生效）。 */
  function retryWithForce() {
    if (state.running) return;
    state.forceOnce = true;
    submit().finally(() => { state.forceOnce = false; });
  }

  function openReview(runId) {
    context.openReview ? context.openReview(runId) : api.surfaces.open('review', {
      source: 'task',
      runId,
    });
  }

  async function cancelRun() {
    const runId = state.runId;
    if (!runId) return;
    try {
      await client.cancelJob(runId);
      api.host.toast('已请求取消', 2400, 'check');
    } catch (error) {
      api.host.toast(`取消失败：${error.message}`, 3200, 'close');
    }
  }

  // ---------------------------------------------------------------- 参数数据

  function syncProviderModel() {
    const provider = state.providers.find((item) => item.id === state.providerId);
    if (provider && !modelInput.value.trim()) {
      state.model = provider.model || '';
      modelInput.value = state.model;
      context.model = state.model;
    }
  }

  function renderProviderOptions() {
    providerSelect.setOptions(
      state.providers.map((item) => ({
        value: item.id,
        label: `${item.name || item.id}${item.is_default ? '（默认）' : ''}`,
      })),
    );
    if (state.providerId && state.providers.some((item) => item.id === state.providerId)) {
      providerSelect.value = state.providerId;
    } else {
      const fallback = state.providers.find((item) => item.is_default) || state.providers[0];
      state.providerId = fallback ? fallback.id : '';
      context.providerId = state.providerId;
      if (fallback) providerSelect.value = fallback.id;
    }
    if (!modelInput.value.trim()) {
      const provider = state.providers.find((item) => item.id === state.providerId);
      state.model = (provider && provider.model) || '';
      modelInput.value = state.model;
    }
  }

  function renderProfileOptions() {
    // `/profiles` 返回的是 `{ index: {id, name, builtin}, system_preview }`。
    // 旧代码直接读 `profile.id` / `profile.name`，于是每个选项都渲染成
    // 「undefined · 通用」且 value 为空 —— 选了等于没选。
    const flat = state.profiles.map((profile) => {
      const index = (profile && profile.index) || profile || {};
      return {
        id: String(index.id || ''),
        name: String(index.name || index.id || ''),
        builtin: Boolean(index.builtin),
        task: String(index.task || ''),
      };
    }).filter((profile) => profile.id);

    const usable = flat.filter((profile) => {
      if (candidateMatchesTask(profile, taskId)) return true;
      // 内置功能档案（id 形如 builtin-classify）永远是本任务的候选
      return profile.id.startsWith(`builtin-${taskId === 'polish' ? 'wi-rewrite' : taskId}`);
    });
    const options = [{ value: '', label: '（按任务自动匹配）' }]
      .concat(usable.map((profile) => ({
        value: profile.id,
        label: `${profile.name}${profile.builtin ? '（内置）' : ''}`,
      })));
    profileSelect.setOptions(options);
    profileSelect.value = state.profileId;
  }

  function candidateMatchesTask(profile, id) {
    const declared = String((profile && profile.task) || '');
    if (!declared) return true;         // 未声明任务的老档案：都可选，交给后端校验
    if (id === 'polish') return declared === 'wi_rewrite';
    return declared === id || declared === 'agent';
  }

  // ---------------------------------------------------------------- 生命周期

  async function refresh() {
    renderScope();
    renderActions();
    renderResultHint();
    try {
      const [providersData, profilesData, statusData] = await Promise.all([
        client.providers(),
        client.profiles(),
        client.status(),
      ]);
      state.providers = providersData.items || [];
      state.profiles = profilesData.items || [];
      renderProviderOptions();
      renderProfileOptions();
      const config = statusData.config || {};
      if (!state.profileId) {
        state.profileId = String((config.task_profiles || {})[taskId] || '');
      }
      if (config.apply_mode && state.applyMode === 'review') {
        state.applyMode = config.apply_mode === 'auto' ? 'auto' : 'review';
        applySelect.value = state.applyMode;
      }
    } catch (error) {
      clear(alertHost);
      alertHost.append(banner(`读取运行参数失败：${error.message}`, 'warn'));
    }
  }

  function renderActions() {
    const ready = taskId === 'propose' ? true : Boolean(state.scope && !state.scope.empty);
    submitBtn.disabled = !ready || state.running;
    submitBtn.textContent = ready ? taskActionLabel(task, state.scope) : '开始';
    previewBtn.disabled = !ready && taskId !== 'propose';
  }

  return {
    element: root,
    refresh,
    /** 入口打开时调用：把宿主的 `ctx.target` 变成面板里的作用域。 */
    applyContext(ctx) {
      const target = normalizeTarget(ctx);
      const source = String((ctx && ctx.source) || '');
      if (target) {
        loadScope(target);
      } else if (source === 'dock' || source === 'shortcut') {
        // 坞/快捷键打开：不继承上一次的作用域（防「右键 12 张残留到下次」）
        loadScope(null);
      } else {
        loadScope(null);
      }
      if (ctx && ctx.runId) {
        state.runId = String(ctx.runId);
        renderResultHint();
      }
    },
    focusRun(runId) {
      state.runId = String(runId || '');
      renderResultHint();
    },
    destroy() {
      clear(root);
    },
  };
}

// ---------------------------------------------------------------- 各任务专属参数

function buildTaskOptions(taskId, host, state, client, ui) {
  const { select } = ui;
  if (taskId === 'analyze') return buildAnalyzeOptions(host, state, client);
  if (taskId === 'polish') return buildPolishOptions(host, state, client, select);
  if (taskId === 'classify') return buildClassifyOptions(host, state, client);
  if (taskId === 'propose') return buildProposeOptions(host, state, client, select);
  return { submit: () => Promise.reject(new Error(`未知任务：${taskId}`)) };
}

function firstCardId(state) {
  const items = (state.scope && state.scope.items) || [];
  return items.length ? String(items[0].id || '') : '';
}

function buildAnalyzeOptions(host, state, client) {
  const skipUnchanged = checkbox(true, '跳过内容未变化的卡片');
  const budget = field(
    '预算上限（token，0 = 不限）',
    input('0', 'number', '0'),
  );
  host.append(skipUnchanged, budget);

  return {
    submit: (current, base) => {
      const cards = (current.scope && current.scope.items) || [];
      if (!cards.length) return Promise.reject(new Error('作用域里没有卡片'));
      return client.submitAnalyze({
        ...base,
        card_ids: cards.map((card) => card.id),
        mode: 'both',
        skip_unchanged: skipUnchanged.querySelector('input').checked,
        // 手动选卡时用户点「开始」就是明确意图：空提交的说明块里再点
        // 「仍然重新打标」会置 `forceOnce`，只对这一次提交生效。
        force: Boolean(current.forceOnce),
        budget_tokens: Number(budget.querySelector('input').value) || 0,
      });
    },
  };
}

//: 落地策略（§6.1）：UI 上只暴露三选一，底层是两个正交参数。
const LANDING_POLICIES = [
  { value: 'review', label: '先看再落地（少量条目推荐）' },
  { value: 'enforce', label: '检查通过的自动落地（批量推荐）' },
  { value: 'off', label: '全部落地，不做检查' },
];

const LANDING_PARAMS = {
  review: { apply_mode: 'review', checker: 'advise' },
  enforce: { apply_mode: 'auto', checker: 'enforce' },
  off: { apply_mode: 'auto', checker: 'off' },
};

function buildPolishOptions(host, state, client, select) {
  // 「改写格式」（YAML / 纯文本）下拉已删除（§2.2）。
  // 这个选择对用户没有可预期收益：选 YAML 只会让模型给正文套上
  // `名称/关键词/原文: |-` 的外壳，而外壳**没有任何地方会拆**——
  // 它会被原样写进世界书条目正文，同时把 token 全算在压缩率上。
  // 格式意图现在写在提示词里（`wicheck.REWRITE_OUTPUT_RULE`），用户不用理解这个开关。

  // 落地策略（C21 / §6.1）：把 apply_mode + checker 合成一个用户看得懂的三选一。
  // 默认按作用域大小自动切 —— 批量没人愿意逐条点，单条则要保留手动裁决。
  const policySelect = select(LANDING_POLICIES, 'review', () => {
    state.policy = policySelect.value;
    state.policyTouched = true;
  });
  state.policy = 'review';
  state.policyTouched = false;
  host.append(field(
    '落地策略',
    policySelect,
    '「检查通过的自动落地」只写没有问题的条目；「全部落地」不检查，出问题自己负责。'
      + '批量时默认自动、少量时默认先看。',
  ));

  // 前置过滤（§7）默认把数据/代码类条目挡在批次外。含双花括号宏的散文条目
  // 也会命中（宏是程序注入用的，实测占全库四成多），所以必须给一个「强制处理」
  // 的出口 —— 否则用户在面板里根本改不了这些条目。
  const forceFiltered = checkbox(
    false,
    '连命中前置过滤的条目一起处理（代码 / 宏 / 变量初始化：可能被改坏，请逐条复核）',
  );
  host.append(forceFiltered);

  return {
    /** 预取条目清单：世界书任务的作用域是文件，条目在面板内按书分组勾选。 */
    previewPayload: (current) => {
      const books = (current.scope && current.scope.items) || [];
      return {
        worldbook_source: books.map((book) => ({
          source_type: book.source_type || 'global',
          file_path: book.file_path || '',
          card_id: book.card_id || '',
        })),
      };
    },
    renderPreview(wrap, data) {
      const books = Array.isArray(data.books) ? data.books : [];
      if (!books.length) {
        wrap.append(emptyState('这本书没有可改写的条目。', null));
        return;
      }
      // 预览只读了前 N 本（逐本读文件，上千本会卡住面板）：如实说明，
      // 别让用户以为「分类里就这么多书」。
      if (data.books_truncated) {
        wrap.append(banner(
          `预览只读取了前 ${books.length} 本（该作用域共 ${data.books_total || '更多'} 本）。`
            + '提交时仍会处理全部，但勾选只能针对这前几本 —— 数量大时建议分批。',
          'warn',
        ));
      }
      // 按书分组：同名条目（uid 在各自文件里独立编号）必须能区分归属
      books.forEach((book) => {
        const source = book.source || {};
        const label = source.file_path || source.card_id || '（未知来源）';
        const group = el('div', 'plugin-stm-ai-assistant-book-group');
        const head = el('div', 'plugin-stm-ai-assistant-subtitle');
        // 前置过滤（§7）：先把「有几条会被跳过」摆在书头上，
        // 用户才不会勾了 20 条、跑完发现只处理了 12 条。
        const protectedCount = (book.items || []).filter((entry) => entry.skip_reason).length;
        const protectedNote = protectedCount
          ? `（其中 ${protectedCount} 条会被前置过滤跳过）`
          : '';
        head.textContent = book.readable === false
          ? `${label} · 读取失败：${book.message || ''}`
          : `${label} · ${book.total} 条${protectedNote}`;
        if (book.readable === false) head.classList.add('is-error');
        group.append(head);

        if (book.readable !== false) {
          const list = el('div', 'plugin-stm-ai-assistant-checks');
          (book.items || []).forEach((entry) => {
            const uid = String(entry.uid);
            const skipped = entry.skip_reason
              ? ` · 会被跳过：${entry.skip_label || entry.skip_reason}`
              : '';
            const box = checkbox(
              Boolean(entry.enabled),
              `${entry.name || '(未命名)'} · ${formatNumber(entry.tokens)} token${skipped}`,
            );
            box.dataset.uid = uid;
            // 勾选回传时需要知道属于哪本书
            box.dataset.book = label;
            if (!entry.enabled) box.classList.add('is-disabled');
            if (entry.skip_reason) {
              box.classList.add('is-protected');
              box.dataset.skipReason = String(entry.skip_reason);
            }
            list.append(box);
          });
          group.append(list);
        }
        wrap.append(group);
      });
    },
    submit: (current, base) => {
      const books = (current.scope && current.scope.items) || [];
      if (!books.length) return Promise.reject(new Error('作用域里没有世界书'));

      const boxes = Array.from(host.querySelectorAll('input[type="checkbox"]'))
        .filter((node) => node.checked && node.dataset.uid);
      if (!boxes.length) {
        return Promise.reject(new Error('请先点「预览」读取条目，并勾选要改写的内容'));
      }

      // 条目 → 所属书：同一批作业可以横跨多本书，后端按 worldbook_source 分组回写
      const wantedUids = new Set(boxes.map((node) => String(node.dataset.uid)));
      const sources = books
        .filter((book) => book.source_type !== 'embedded' || book.card_id)
        .map((book) => ({
          source_type: book.source_type || 'global',
          file_path: book.file_path || '',
          card_id: book.card_id || '',
        }));

      // 落地策略：用户动过就以用户为准；没动过则按待处理条目数自动切
      // （批量默认「检查通过的自动落地」，单条/少量默认「先看再落地」）。
      const policyValue = state.policyTouched
        ? policySelect.value
        : (wantedUids.size > 5 ? 'enforce' : 'review');
      const policyParams = LANDING_PARAMS[policyValue] || LANDING_PARAMS.review;

      return client.submitWiRewrite({
        ...base,
        apply_mode: policyParams.apply_mode,
        checker: policyParams.checker,
        card_id: books.length === 1 ? (books[0].card_id || '') : '',
        worldbook_source: sources,
        entry_uids: Array.from(wantedUids),
        force: forceFiltered.querySelector('input').checked,
      });
    },
  };
}

function buildClassifyOptions(host, state, client) {
  const allowNew = checkbox(false, '允许模型新建分类');
  const schemeHost = el('div', 'plugin-stm-ai-assistant-checks');
  const note = el('div', 'plugin-stm-ai-assistant-note', '先读取分类，勾选允许的目标；不勾任何分类则必须允许新建。');
  const loadBtn = el('button', 'plugin-stm-ai-assistant-chip', '读取现有分类');
  loadBtn.type = 'button';
  loadBtn.dataset.marqueeIgnore = 'true';
  loadBtn.addEventListener('click', () => load());
  host.append(note, loadBtn, schemeHost, allowNew);

  async function load() {
    clear(schemeHost);
    schemeHost.append(skeleton('读取中…'));
    try {
      const data = await client.listCategories();
      const items = data.items || [];
      clear(schemeHost);
      if (!items.length) {
        schemeHost.append(emptyState('主程序还没有任何分类。'));
        return;
      }
      items.forEach((item) => {
        // 宿主新旧两种形状都要认：旧宿主返回字符串数组，新宿主返回
        // `{path, name, count}` 对象数组（H4）。以前只读 `item.path || item.name`，
        // 字符串项被整条跳过 → 「读取现有分类」永远 0 个且不报错（P0-6）。
        const isObject = item && typeof item === 'object';
        const path = String(isObject ? (item.path || item.name || '') : (item || ''));
        if (!path) return;
        const count = isObject ? Number(item.count || 0) : 0;
        const box = checkbox(true, `${path}（${count}）`);
        box.dataset.path = path;
        schemeHost.append(box);
      });
    } catch (error) {
      clear(schemeHost);
      schemeHost.append(banner(`读取分类失败：${error.message}`, 'error'));
    }
  }

  return {
    submit: (current, base) => {
      const cards = (current.scope && current.scope.items) || [];
      if (!cards.length) return Promise.reject(new Error('作用域里没有卡片'));
      const scheme = Array.from(schemeHost.querySelectorAll('input[type="checkbox"]'))
        .filter((node) => node.checked && node.dataset.path)
        .map((node) => ({ name: node.dataset.path }));
      const allow = allowNew.querySelector('input').checked;
      if (!scheme.length && !allow) {
        return Promise.reject(new Error('请先读取分类，或允许模型新建分类'));
      }
      return client.submitClassify({
        ...base,
        card_ids: cards.map((card) => card.id),
        scheme,
        allow_new_category: allow,
      });
    },
  };
}

function buildProposeOptions(host, state, client, select) {
  const modeSelect = select([
    { value: 'sample', label: '采样分析（快，成本低）' },
    { value: 'agent', label: 'Agent 深读（慢，更准）' },
  ], 'sample', () => {
    state.proposeMode = modeSelect.value;
    sync();
  });
  state.proposeMode = 'sample';

  const sampleSize = field('采样张数', input('30', 'number', '30'));
  const sampleBudget = field('采样 token 预算', input('4000', 'number', '4000'));
  // 留空 = 按库规模自适应（后端 max(4, min(12, ceil(卡数/6)))）。
  // 旧版发的是 `max_rounds`，后端只读 `max_iterations`，等于永远拿不到用户设置。
  const agentRounds = field('Agent 最多轮次', input('', 'number', '留空 = 按库规模自适应'));
  const agentReads = field('Agent 读取配额', input('40', 'number', '40'));

  host.append(field('分析方式', modeSelect, ''), sampleSize, sampleBudget, agentRounds, agentReads);

  function sync() {
    const agent = state.proposeMode === 'agent';
    sampleSize.hidden = agent;
    sampleBudget.hidden = agent;
    agentRounds.hidden = !agent;
    agentReads.hidden = !agent;
  }
  sync();

  return {
    previewPayload: () => ({
      sample_size: Number(sampleSize.querySelector('input').value) || 30,
      sample_budget_tokens: Number(sampleBudget.querySelector('input').value) || 4000,
    }),
    submit: (current, base) => {
      if (state.proposeMode === 'agent') {
        const rounds = Number(agentRounds.querySelector('input').value);
        const budget = { max_reads: Number(agentReads.querySelector('input').value) || 40 };
        // 只在用户真的填了轮次时才带上；留空交给后端按库规模自适应
        if (Number.isFinite(rounds) && rounds > 0) budget.max_iterations = rounds;
        return client.submitAgent({ ...base, budget });
      }
      return client.submitProposal({
        ...base,
        options: {
          sample_size: Number(sampleSize.querySelector('input').value) || 30,
          sample_budget_tokens: Number(sampleBudget.querySelector('input').value) || 4000,
        },
      });
    },
  };
}

export { estimateRow, describeScope };
