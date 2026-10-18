/**
 * 工作台：选卡 → 预览 → 提交作业 → 实时进度。
 *
 * 选中集合的读取集中在 `selection.js`（唯一的 Provisional 耦合点）；入口的可用/
 * 置灰由宿主按 `entries[].when.selection` 求值，不再由本视图判断。
 */

import {
  checkbox,
  clear,
  createUi,
  el,
  field,
  formatCost,
  formatNumber,
  input,
} from '../dom.js';
import { readSelectedCardIds } from '../selection.js';

const MODE_OPTIONS = [
  { value: 'both', label: '概览 + 标签' },
  { value: 'summary', label: '仅概览' },
  { value: 'tags', label: '仅标签' },
];

const APPLY_OPTIONS = [
  { value: 'review', label: '逐条审批后应用（推荐）' },
  { value: 'auto', label: '生成后直接写入' },
];

export function createWorkbench(api, client, context) {
  const { badge, button, section, select } = createUi(api);
  const root = el('div', 'plugin-stm-ai-assistant-view');

  const state = {
    cards: [],
    cardQuery: '',
    pickerOpen: false,
    preview: null,
    progress: null,
    logs: [],
    stopSubscribe: null,
  };

  // ---------------------------------------------------------------- 卡片

  const cardsSection = section('1 · 选择卡片', '默认读取主程序当前勾选的卡片，也可以在这里手动挑选。');
  const selectionInfo = el('div', 'plugin-stm-ai-assistant-selection');
  const picker = el('div', 'plugin-stm-ai-assistant-picker');

  const cardActions = el('div', 'plugin-stm-ai-assistant-row');
  cardActions.append(
    button('读取主程序选择', 'is-secondary', () => {
      readMainSelection();
      renderCards();
    }),
    button('清空', 'is-secondary', () => {
      context.selectedCardIds = [];
      // 手动清空 = 放弃入口带来的作用域，否则 renderCards 会一直显示旧作用域
      context.openTarget = null;
      renderCards();
    }),
    button('手动选择', 'is-secondary', () => {
      state.pickerOpen = !state.pickerOpen;
      if (state.pickerOpen && !state.cards.length) loadCards();
      renderCards();
    }),
  );

  cardsSection.append(selectionInfo, cardActions, picker);

  function readMainSelection() {
    // 用户显式改选 → 放弃入口作用域，回到「主程序当前选中集」语义
    context.openTarget = null;
    context.selectedCardIds = readSelectedCardIds(api);
  }

  async function loadCards() {
    try {
      const data = await client.cards(state.cardQuery);
      state.cards = data.items || [];
    } catch (error) {
      state.cards = [];
      api.host.toast(`读取卡片列表失败：${error.message}`, 3600, 'close');
    }
    renderCards();
  }

  function renderCards() {
    const count = context.selectedCardIds.length;
    clear(selectionInfo);

    // 入口带来的作用域（右键分类 / 批量选中）优先展示：用户点「分析此分类」后
    // 必须能看出这次分析的范围，而不是只看到「已选 N 张」。
    const target = context.openTarget;
    if (target) {
      const label = target.kind === 'folder'
        ? `分类「${target.path === '' ? '根目录' : target.path}」（含子分类）`
        : target.kind === 'selection'
          ? '批量选中的卡片'
          : '右键的卡片';
      selectionInfo.append(
        badge(`作用域 ${label}`, count ? 'is-ok' : 'is-muted'),
        el('span', 'plugin-stm-ai-assistant-hint', `共 ${count} 张待分析`),
      );
      if (!count) {
        selectionInfo.append(el('p', 'plugin-stm-ai-assistant-hint',
          '该作用域下没有可分析的卡片；可改用「读取主程序选择」或「手动选择」。'));
      }
      picker.classList.add('is-hidden');
      return;
    }

    selectionInfo.append(
      badge(`已选 ${count} 张`, count ? 'is-ok' : 'is-muted'),
      el('span', 'plugin-stm-ai-assistant-hint',
        count ? context.selectedCardIds.slice(0, 3).join('、') + (count > 3 ? ' …' : '') : '尚未选择卡片'),
    );

    picker.classList.toggle('is-hidden', !state.pickerOpen);
    if (!state.pickerOpen) return;

    clear(picker);
    const searchRow = el('div', 'plugin-stm-ai-assistant-row');
    const searchInput = input(state.cardQuery, 'text', '按名称过滤');
    searchRow.append(
      searchInput,
      button('搜索', 'is-secondary', () => {
        state.cardQuery = searchInput.value.trim();
        loadCards();
      }),
    );
    picker.append(searchRow);

    const list = el('div', 'plugin-stm-ai-assistant-card-list');
    const selected = new Set(context.selectedCardIds);
    state.cards.slice(0, 300).forEach((card) => {
      const row = el('label', 'plugin-stm-ai-assistant-card-item');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = selected.has(card.card_id);
      box.dataset.marqueeIgnore = 'true';
      box.addEventListener('change', () => {
        const current = new Set(context.selectedCardIds);
        if (box.checked) current.add(card.card_id);
        else current.delete(card.card_id);
        context.selectedCardIds = Array.from(current);
        renderCards();
      });
      row.append(box, el('span', '', card.name || card.card_id));
      list.append(row);
    });
    if (!state.cards.length) list.append(el('p', 'plugin-stm-ai-assistant-hint', '（没有可选的卡片）'));
    picker.append(list);
  }

  // ---------------------------------------------------------------- 参数

  const optionsSection = section('2 · 运行参数');
  const optionsGrid = el('div', 'plugin-stm-ai-assistant-grid');

  const providerSelect = select([], context.providerId, () => {
    context.providerId = providerSelect.value;
    syncProvider();
    renderPreview();
  });
  const modelInput = input(context.model, 'text', '留空则用 provider 的默认模型');
  modelInput.addEventListener('change', () => {
    context.model = modelInput.value.trim();
    renderPreview();
  });
  const profileSelect = select([{ value: '', label: '（按模型自动匹配）' }], context.profileId, () => {
    context.profileId = profileSelect.value;
    renderPreview();
  });
  const modeSelect = select(MODE_OPTIONS, context.mode, () => {
    context.mode = modeSelect.value;
  });
  const applySelect = select(APPLY_OPTIONS, context.applyMode, () => {
    context.applyMode = applySelect.value;
  });

  optionsGrid.append(
    field('Provider', providerSelect),
    field('模型', modelInput),
    field('提示词档案', profileSelect),
    field('分析目标', modeSelect),
    field('写回方式', applySelect),
  );

  const toggles = el('div', 'plugin-stm-ai-assistant-row');
  toggles.append(
    checkbox(context.dryRun, '仅预览，不写入（dry-run）', (event) => {
      context.dryRun = event.target.checked;
    }),
    checkbox(context.force, '忽略幂等，强制重跑', (event) => {
      context.force = event.target.checked;
    }),
  );

  optionsSection.append(optionsGrid, toggles);

  // ---------------------------------------------------------------- 预览

  const previewSection = section('3 · 预览与执行', '预览不会消耗 API 配额，也不会写入任何状态。');
  const previewOutput = el('div', 'plugin-stm-ai-assistant-preview');
  const actionRow = el('div', 'plugin-stm-ai-assistant-row');

  const previewBtn = button('预览 Prompt', 'is-secondary', runPreview);
  const submitBtn = button('开始分析', 'is-primary', submit);
  const cancelBtn = button('取消作业', 'is-danger is-hidden', async () => {
    if (!context.runId) return;
    try {
      await client.cancelJob(context.runId);
      api.host.toast('已请求取消', 2400, 'forbidden');
    } catch (error) {
      api.host.toast(error.message, 3200, 'close');
    }
  });

  actionRow.append(previewBtn, submitBtn, cancelBtn);
  previewSection.append(actionRow, previewOutput);

  // ---------------------------------------------------------------- 进度

  const progressSection = section('4 · 进度');
  const progressBody = el('div', 'plugin-stm-ai-assistant-progress');
  progressSection.append(progressBody);

  // ---------------------------------------------------------------- 世界书改写

  const wiSection = section(
    '5 · 世界书改写',
    '逐条精简世界书条目。格式只是提示词要求，不做格式校验。',
  );

  const wiList = el('div', 'plugin-stm-ai-assistant-checks');
  const wiOutput = el('div', 'plugin-stm-ai-assistant-preview');
  const wiLoadBtn = button('读取世界书条目', 'is-secondary', loadWiEntries);
  const wiApplyMode = select(APPLY_OPTIONS, 'review');
  const wiFormat = select([
    { value: 'yaml', label: 'YAML（推荐，省 token）' },
    { value: 'text', label: '保持纯文本' },
  ], 'yaml');
  const wiSubmitBtn = button('开始改写', 'is-primary', submitWiRewrite);

  const wiControls = el('div', 'plugin-stm-ai-assistant-row');
  wiControls.append(wiLoadBtn, wiFormat, wiApplyMode, wiSubmitBtn);

  wiSection.append(
    el('p', 'plugin-stm-ai-assistant-hint',
      '只处理**第一张选中卡片**的内嵌世界书。逐条改写后做内容校验'
      + '（宏、HTML、压缩比、长度比、专有名词、调度字段），不通过不写回。'),
    wiControls,
    wiList,
    wiOutput,
  );

  const classifySection = section(
    '6 · 自动归类',
    '给卡片判断所属分类并移动目录。模型只能在方案内选。',
  );

  const proposeSection = section(
    '7 · 结构提议（分析卡片库）',
    'AI 分析整个卡片库，提议分类体系与标签池。只创建结构，不动卡片。',
  );

  const proposeMode = select([
    { value: 'sample', label: '一次性采样（快，便宜）' },
    { value: 'agent', label: 'Agent 分批读取（慢，看得更全）' },
  ], 'sample');

  const proposeSize = field('采样张数', input('30', 'number', '30'));
  const proposeBudget = field('采样 token 预算', input('4000', 'number', '4000'));
  const proposeRounds = field('Agent 最多轮次', input('6', 'number', '6'));
  const proposeReads = field('Agent 读取配额', input('40', 'number', '40'));
  const proposePreviewBtn = button('预览采样', 'is-secondary', previewProposal);
  const proposeSubmitBtn = button('生成提议', 'is-primary', submitProposal);

  const proposeControls = el('div', 'plugin-stm-ai-assistant-row');
  proposeControls.append(proposeMode, proposePreviewBtn, proposeSubmitBtn);

  const proposeFields = el('div', 'plugin-stm-ai-assistant-row');
  proposeFields.append(proposeSize, proposeBudget, proposeRounds, proposeReads);

  const proposeOutput = el('div', 'plugin-stm-ai-assistant-preview');

  function syncProposeMode() {
    const isAgent = proposeMode.value === 'agent';
    proposeSize.classList.toggle('is-hidden', isAgent);
    proposeBudget.classList.toggle('is-hidden', isAgent);
    proposeRounds.classList.toggle('is-hidden', !isAgent);
    proposeReads.classList.toggle('is-hidden', !isAgent);
    proposePreviewBtn.classList.toggle('is-hidden', isAgent);
  }
  proposeMode.addEventListener('change', syncProposeMode);

  proposeSection.append(
    el('p', 'plugin-stm-ai-assistant-hint',
      '两种模式产出**同一种结果**（分类方案 + 标签池 + 合并建议），落地方式也一样。'
      + '差别只在「怎么看卡片库」：采样模式只读 30 张样本；'
      + 'Agent 模式先看全库目录，再自己决定分批读哪些卡。'
      + '卡片很多、类型跨度大时用 Agent。'),
    proposeControls,
    proposeFields,
    proposeOutput,
  );

  root.append(cardsSection, optionsSection, previewSection, progressSection,
              wiSection, classifySection, proposeSection);

  // ---------------------------------------------------------------- 结构提议

  function proposeOptions() {
    return {
      sample_size: Number(proposeSize.querySelector('input').value) || 30,
      sample_budget_tokens: Number(proposeBudget.querySelector('input').value) || 4000,
    };
  }

  function agentBudget() {
    return {
      max_iterations: Number(proposeRounds.querySelector('input').value) || 6,
      max_reads: Number(proposeReads.querySelector('input').value) || 40,
    };
  }

  async function previewProposal() {
    proposePreviewBtn.disabled = true;
    try {
      const data = await client.previewProposalSample({ options: proposeOptions() });
      const sample = data.sample || {};
      const lines = [
        `采样 ${formatNumber(sample.sampled)}/${formatNumber(sample.total)} 张`
        + ` · ${formatNumber(sample.tokens)}/${formatNumber(sample.budget_tokens)} token`,
      ];
      if (sample.capped) {
        lines.push('（卡片数超过扫描上限，样本可能有偏）');
      }
      if ((data.existing || {}).categories) {
        lines.push(`现有分类 ${((data.existing || {}).categories || []).length} 个`
          + ` · 现有标签 ${formatNumber((data.existing || {}).tag_count)} 个`);
      }
      proposeOutput.textContent = lines.join('\n');
      appendLog({ level: 'info', message: '采样预览已生成，可在下方核对样本' });
    } catch (error) {
      proposeOutput.textContent = `预览失败：${error.message}`;
    } finally {
      proposePreviewBtn.disabled = false;
    }
  }

  async function submitProposal() {
    const isAgent = proposeMode.value === 'agent';
    proposeSubmitBtn.disabled = true;
    try {
      const data = isAgent
        ? await client.submitAgent({
          provider_id: context.providerId,
          model: context.model,
          budget: agentBudget(),
        })
        : await client.submitProposal({
          provider_id: context.providerId,
          model: context.model,
          options: proposeOptions(),
        });
      context.runId = data.run_id;
      proposeOutput.textContent = isAgent
        ? 'Agent 已启动：它会先看全库目录，再决定分批读哪些卡，'
          + '最多 ' + (agentBudget().max_iterations) + ' 轮、'
          + agentBudget().max_reads + ' 张。进度见下方「进度」。'
        : '提议已提交。完成后到「结果与审批」查看：\n'
          + '分类方案、标签池建议、标签合并建议都会列在那里，'
          + '确认后再创建目录与标签。';
      api.host.toast(isAgent ? 'Agent 已启动' : '结构提议已提交', 2600, 'check');
    } catch (error) {
      proposeOutput.textContent = `提交失败：${error.message}`;
    } finally {
      proposeSubmitBtn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 自动归类

  const classifyList = el('div', 'plugin-stm-ai-assistant-checks');
  const classifyOutput = el('div', 'plugin-stm-ai-assistant-preview');
  const classifyLoadBtn = button('读取现有分类', 'is-secondary', loadCategories);
  const classifyApplyMode = select(APPLY_OPTIONS, 'review');
  const classifyAllowNew = checkbox(false, '允许模型新建分类');
  const classifySubmitBtn = button('开始归类', 'is-primary', submitClassify);

  const classifyControls = el('div', 'plugin-stm-ai-assistant-row');
  classifyControls.append(classifyLoadBtn, classifyApplyMode, classifyAllowNew,
                          classifySubmitBtn);

  classifySection.append(
    el('p', 'plugin-stm-ai-assistant-hint',
      '给选中的卡片判断所属分类并移动目录。**模型只能在下面的分类里选**——'
      + '除非你勾选「允许新建」，否则方案外的分类会被拒绝，卡片不会被移动。'),
    classifyControls,
    classifyList,
    classifyOutput,
  );

  async function loadCategories() {
    clear(classifyList);
    try {
      const data = await client.listCategories();
      const items = data.items || [];
      if (!items.length) {
        classifyOutput.textContent = '主程序还没有分类。可勾选「允许模型新建分类」让它提议。';
        return;
      }
      items.forEach((path) => {
        const box = checkbox(true, '');
        box.dataset.path = path;
        const wrap = el('label', 'plugin-stm-ai-assistant-wi-item');
        wrap.append(box, el('span', '', path));
        classifyList.append(wrap);
      });
      classifyOutput.textContent = `已读取 ${items.length} 个分类，取消勾选可排除。`;
    } catch (error) {
      classifyOutput.textContent = `读取失败：${error.message}`;
    }
  }

  async function submitClassify() {
    if (!context.selectedCardIds.length) {
      api.host.toast('请先选择卡片', 2400, 'close');
      return;
    }

    const scheme = Array.from(classifyList.querySelectorAll('input[type="checkbox"]'))
      .filter((node) => node.checked && node.dataset.path)
      .map((node) => ({ name: node.dataset.path }));

    const allowNew = classifyAllowNew.querySelector('input').checked;
    if (!scheme.length && !allowNew) {
      api.host.toast('请先读取分类，或允许模型新建分类', 3200, 'close');
      return;
    }

    classifySubmitBtn.disabled = true;
    try {
      const data = await client.submitClassify({
        card_ids: context.selectedCardIds,
        scheme,
        allow_new_category: allowNew,
        provider_id: context.providerId,
        model: context.model,
        profile_id: context.profileId || 'builtin-classify',
        apply_mode: classifyApplyMode.value,
      });
      context.runId = data.run_id;
      classifyOutput.textContent = `已提交 ${data.total} 张，方案 ${data.scheme_size} 个分类。`
        + '进度见下方「进度」，结果在「结果与审批」。';
      api.host.toast('自动归类已提交', 2600, 'check');
    } catch (error) {
      classifyOutput.textContent = `提交失败：${error.message}`;
    } finally {
      classifySubmitBtn.disabled = false;
    }
  }

  async function loadWiEntries() {
    const cardId = context.selectedCardIds[0];
    if (!cardId) {
      api.host.toast('请先选择一张卡片', 2400, 'close');
      return;
    }
    clear(wiList);
    try {
      const data = await client.listWorldbookEntries({ card_id: cardId });
      const items = data.items || [];
      if (!items.length) {
        wiOutput.textContent = '这张卡片没有内嵌世界书条目。';
        return;
      }

      items.forEach((entry) => {
        const box = checkbox(Boolean(entry.enabled), '');
        box.dataset.uid = entry.uid;
        const label = `${entry.name || '(未命名)'} · ${formatNumber(entry.tokens)} token`;
        const wrap = el('label', 'plugin-stm-ai-assistant-wi-item');
        wrap.append(box, el('span', '', label));
        if (!entry.enabled) wrap.classList.add('is-disabled');
        wiList.append(wrap);
      });      wiOutput.textContent = `已读取 ${items.length} 条，勾选要改写的条目。`;
    } catch (error) {
      wiOutput.textContent = `读取失败：${error.message}`;
    }
  }

  async function submitWiRewrite() {
    const cardId = context.selectedCardIds[0];
    if (!cardId) {
      api.host.toast('请先选择一张卡片', 2400, 'close');
      return;
    }

    const boxes = Array.from(wiList.querySelectorAll('input[type="checkbox"]'));
    const uids = boxes
      .filter((node) => node.checked && node.dataset.uid)
      .map((node) => node.dataset.uid);

    wiSubmitBtn.disabled = true;
    try {
      const data = await client.submitWiRewrite({
        card_id: cardId,
        entry_uids: uids,
        provider_id: context.providerId,
        model: context.model,
        profile_id: context.profileId || 'builtin-wi-rewrite',
        apply_mode: wiApplyMode.value,
        target_format: wiFormat.value,
      });
      context.runId = data.run_id;
      wiOutput.textContent = `已提交 ${data.total} 条，目标格式 ${data.target_format}。`
        + '进度见下方「进度」，结果在「结果与审批」。';
      api.host.toast('世界书改写已提交', 2600, 'check');
    } catch (error) {
      wiOutput.textContent = `提交失败：${error.message}`;
    } finally {
      wiSubmitBtn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- 逻辑

  function syncProvider() {
    const provider = state.providers.find((item) => item.id === context.providerId);
    if (provider && !modelInput.value.trim()) {
      context.model = provider.model || '';
      modelInput.value = context.model;
    }
  }

  function renderPreview() {
    if (!state.preview) return;
    clear(previewOutput);

    const head = el('div', 'plugin-stm-ai-assistant-row');
    head.append(
      badge(`档案 ${state.preview.profile.id}`, 'is-ok'),
      badge(`模型 ${state.preview.provider.model || '(未设)'}`),
      badge(`prompt ${formatNumber(state.preview.tokens.total)} token`),
      badge(`成本 ${formatCost(state.preview.cost)}`),
    );
    previewOutput.append(head);

    const worldbook = state.preview.worldbook || {};
    previewOutput.append(el(
      'p', 'plugin-stm-ai-assistant-hint',
      `世界书：${worldbook.count} 条 · 总 ${formatNumber(worldbook.total_tokens)} token · `
      + `节选 ${formatNumber(worldbook.excerpt_tokens)} token${worldbook.truncated ? '（已裁剪）' : ''}`,
    ));

    // 指定的条目没找到时要说清楚，否则用户以为发出去了
    if ((worldbook.pinned_missing || []).length) {
      previewOutput.append(el('p', 'plugin-stm-ai-assistant-warning',
        `勾选的世界书条目不存在或已停用：${worldbook.pinned_missing.join('、')}`));
    }

    const missing = state.preview.missing || {};
    if ((missing.required || []).length) {
      previewOutput.append(el('p', 'plugin-stm-ai-assistant-warning',
        `缺失必需变量：${missing.required.join('、')}`));
    }

    // 标签池：预算与隔离分类的硬过滤是两件最容易困惑的事，都要显示
    const pool = state.preview.tag_pool;
    if (pool && (pool.available || pool.budget_tokens)) {
      const bits = [
        `候选 ${formatNumber(pool.available)} 个`,
        `列出 ${formatNumber((pool.listed || []).length)} 个`,
      ];
      if ((pool.dropped || []).length) {
        bits.push(`预算外 ${formatNumber(pool.dropped.length)} 个`);
      }
      bits.push(`${formatNumber(pool.tokens)}/${formatNumber(pool.budget_tokens)} token`);

      previewOutput.append(el(
        'p', 'plugin-stm-ai-assistant-hint', `标签池：${bits.join(' · ')}`,
      ));

      if ((pool.isolated_categories || []).length) {
        previewOutput.append(el(
          'p', 'plugin-stm-ai-assistant-hint',
          `隔离分类（硬过滤，模型输出也不会写入）：${pool.isolated_categories.join('、')}`,
        ));
      }
    }

    // 选择器与模板的不一致（空值警告 / 白勾提示）
    const selector = state.preview.selector || {};
    if ((selector.gaps || []).length) {
      const box = el('div', 'plugin-stm-ai-assistant-warning');
      box.append(el('div', 'plugin-stm-ai-assistant-label', '选择器提示'));
      const list = el('ul', 'plugin-stm-ai-assistant-gaps');
      selector.gaps.forEach((gap) => {
        const item = el('li', '', gap);
        list.append(item);
      });
      box.append(list);
      previewOutput.append(box);
    }

    // token 花在哪：按变量降序，前几项就占了绝大部分
    const byBlock = (state.preview.tokens || {}).by_block;
    if (byBlock && (byBlock.items || []).length) {
      const top = byBlock.items.slice(0, 8);
      const lines = top.map((item) => {
        const label = item.empty ? '（空）' : `${formatNumber(item.tokens)} token`;
        const times = item.count > 1 ? ` ×${item.count}` : '';
        return `${item.name}${times}：${label}`;
      });
      if (byBlock.items.length > top.length) {
        lines.push(`…另 ${byBlock.items.length - top.length} 项`);
      }
      lines.push(`模板骨架：${formatNumber(byBlock.template_overhead)} token`);
      previewOutput.append(el(
        'pre', 'plugin-stm-ai-assistant-budget', `token 明细\n${lines.join('\n')}`,
      ));
    }

    const messages = state.preview.messages || [];
    messages.slice(0, 2).forEach((message) => {
      const block = el('div', 'plugin-stm-ai-assistant-message');
      block.append(el('div', 'plugin-stm-ai-assistant-message-role', message.role));
      block.append(el('pre', 'plugin-stm-ai-assistant-message-body', message.content));
      previewOutput.append(block);
    });

    // 全文交给宿主的 modal surface（次级内容不再塞在页签里造成长滚动）
    if (messages.length && typeof context.openPromptPreview === 'function') {
      previewOutput.append(button('在弹层中查看全文', 'is-secondary', () => {
        context.openPromptPreview(messages);
      }));
    }
  }

  async function runPreview() {
    if (!context.selectedCardIds.length) {
      api.host.toast('请先选择卡片', 2400, 'close');
      return;
    }
    previewBtn.disabled = true;
    try {
      state.preview = await client.preview({
        card_id: context.selectedCardIds[0],
        provider_id: context.providerId,
        model: context.model,
        profile_id: context.profileId,
      });
      renderPreview();
    } catch (error) {
      api.host.toast(`预览失败：${error.message}`, 3600, 'close');
    } finally {
      previewBtn.disabled = false;
    }
  }

  function appendLog(payload) {
    if (!payload) return;
    state.logs.push(payload);
    if (state.logs.length > 40) state.logs.shift();
    renderProgress();
  }

  function renderProgress() {
    clear(progressBody);
    const progress = state.progress;

    if (!progress && !state.logs.length) {
      progressBody.append(el('p', 'plugin-stm-ai-assistant-hint', '尚未提交作业'));
      return;
    }

    if (progress) {
      const row = el('div', 'plugin-stm-ai-assistant-row');
      row.append(
        badge(`状态 ${progress.status || '-'}`, progress.status === 'failed' ? 'is-error' : 'is-ok'),
        badge(`完成 ${formatNumber(progress.completed)} / ${formatNumber(progress.total)}`),
        badge(`失败 ${formatNumber(progress.failed)}`, progress.failed ? 'is-error' : 'is-muted'),
        badge(`跳过 ${formatNumber(progress.skipped)}`, 'is-muted'),
      );
      progressBody.append(row);
      if (progress.message) {
        progressBody.append(el('p', 'plugin-stm-ai-assistant-hint', progress.message));
      }
    }

    const logList = el('div', 'plugin-stm-ai-assistant-logs');
    state.logs.slice(-8).forEach((entry) => {
      logList.append(el('div', `plugin-stm-ai-assistant-log is-${entry.level || 'info'}`,
        entry.message || ''));
    });
    progressBody.append(logList);
  }

  /** 作业进度同时反映到坞入口（`setProgress` / `setBusy` / `setBadge`）。 */
  function syncEntryProgress(progress) {
    const total = Number(progress && progress.total) || 0;
    if (!total) {
      api.entries.setProgress('workbench', null);
      return;
    }
    api.entries.setProgress('workbench', {
      done: Number(progress.completed) || 0,
      total,
    });
  }

  function subscribe(runId) {
    if (state.stopSubscribe) state.stopSubscribe();
    api.entries.setBusy('workbench', true);
    state.stopSubscribe = api.subscribeJob(runId, {
      snapshot: (payload) => {
        state.progress = payload;
        syncEntryProgress(payload);
        renderProgress();
      },
      item: (payload) => {
        appendLog({
          level: payload && payload.status === 'failed' ? 'error' : 'info',
          message: `${payload && payload.item_key} → ${payload && payload.status}`,
        });
      },
      log: (payload) => appendLog(payload),
      done: (payload) => {
        state.progress = payload;
        api.entries.setBusy('workbench', false);
        api.entries.setProgress('workbench', null);
        renderProgress();
        cancelBtn.classList.add('is-hidden');
        api.host.toast('作业已结束，可在「结果与审批」里查看', 3200, 'check');
        context.notifyReview && context.notifyReview();
      },
      error: (payload) => {
        api.entries.setBusy('workbench', false);
        appendLog({ level: 'error', message: (payload && payload.msg) || 'SSE 异常' });
      },
    });
  }

  async function submit() {
    if (!context.selectedCardIds.length) {
      api.host.toast('请先选择卡片', 2400, 'close');
      return;
    }

    submitBtn.disabled = true;
    state.logs = [];
    try {
      const data = await client.submitAnalyze({
        card_ids: context.selectedCardIds,
        mode: context.mode,
        provider_id: context.providerId,
        model: context.model,
        profile_id: context.profileId,
        apply_mode: context.applyMode,
        dry_run: context.dryRun,
        force: context.force,
      });

      if (!data.run_id) {
        api.host.toast(data.message || '没有需要处理的卡片', 3600, 'settings-help-entry');
        return;
      }

      context.runId = data.run_id;
      state.progress = { status: 'queued', total: data.total, completed: 0, failed: 0, skipped: 0 };
      syncEntryProgress(state.progress);
      cancelBtn.classList.remove('is-hidden');
      renderProgress();

      if (data.skipped) {
        appendLog({ level: 'info', message: `已跳过 ${data.skipped} 张（内容未变化或不可读）` });
      }
      if (data.estimate) {
        appendLog({
          level: 'info',
          message: `预估 prompt ${formatNumber(data.estimate.prompt_tokens)} token`
            + ` · 成本 ${formatCost(data.estimate.cost)}`,
        });
      }

      api.host.toast(`作业已创建：${data.run_id.slice(0, 8)}`, 3000, 'check');
      subscribe(data.run_id);
      context.notifyReview && context.notifyReview();
    } catch (error) {
      api.host.toast(`提交失败：${error.message}`, 4000, 'close');
    } finally {
      submitBtn.disabled = false;
    }
  }

  async function refresh() {
    try {
      const data = await client.providers();
      state.providers = data.items || [];
      const options = state.providers.map((item) => ({
        value: item.id,
        label: `${item.name || item.id}${item.is_default ? '（默认）' : ''}`,
      }));
      if (!options.length) options.push({ value: '', label: '（尚未配置 provider）' });

      const current = context.providerId
        || (state.providers.find((item) => item.is_default) || {}).id
        || '';
      context.providerId = current;
      providerSelect.setOptions(options);
      providerSelect.value = current;
      syncProvider();
    } catch (error) {
      api.host.toast(`读取 provider 失败：${error.message}`, 3200, 'close');
    }

    try {
      const data = await client.profiles();
      const options = [{ value: '', label: '（按模型自动匹配）' }];
      (data.items || []).forEach((item) => {
        options.push({
          value: item.index.id,
          label: `${item.index.name} · ${item.index.model_match}${item.index.builtin ? ' · 内置' : ''}`,
        });
      });
      const current = context.profileId || data.active || '';
      context.profileId = current;
      profileSelect.setOptions(options);
      profileSelect.value = current;
    } catch (error) {
      api.host.toast(`读取档案失败：${error.message}`, 3200, 'close');
    }

    // 宿主尚未把选中集合交给插件，这里按需再读一次（见 selection.js 的说明）。
    // 入口带来作用域时**不**回读：右键分类的作用域与全局选中集无关，回读会把它覆盖掉。
    if (!context.selectedCardIds.length && !context.openTarget) readMainSelection();
    renderCards();
    renderProgress();
  }

  function destroy() {
    if (state.stopSubscribe) state.stopSubscribe();
    api.entries.setBusy('workbench', false);
    api.entries.setProgress('workbench', null);
    Object.values({ providerSelect, profileSelect, modeSelect, applySelect }).forEach((control) => {
      if (control && typeof control.destroy === 'function') control.destroy();
    });
  }

  return { element: root, refresh, destroy };
}
