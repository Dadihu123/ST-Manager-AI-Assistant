/**
 * 设置：provider 管理（密钥隔离 + 连通性自检）、用量台账、新标签建议、出站提示。
 *
 * 密钥纪律：`api_key` 与 `extra_headers` **永不回显**，输入框只显示「已设置 / 未设置」，
 * 保存时整体覆盖（留空表示清除）。
 */

import {
  checkbox,
  clear,
  createUi,
  el,
  field,
  formatNumber,
  formatTime,
  input,
  textarea,
} from '../dom.js';

const API_STYLES = [
  { value: 'openai_chat', label: 'OpenAI Chat Completions（/chat/completions）' },
  { value: 'openai_responses', label: 'OpenAI Responses（/responses）' },
  { value: 'anthropic_messages', label: 'Anthropic Messages（/messages）' },
];

/** 协议 → 该协议的鉴权/字段差异提示，显示在协议选择器下方。 */
const API_STYLE_HINTS = {
  openai_chat: '鉴权用 Authorization: Bearer；支持 stop 停止序列。',
  openai_responses: '鉴权同上；最大 token 字段为 max_output_tokens，且不支持 stop。',
  anthropic_messages: '鉴权用 x-api-key + anthropic-version；system 会移到顶层，'
    + 'stop 映射为 stop_sequences，temperature 上限为 1。',
};

const BLANK_PROVIDER = {
  id: '',
  name: '',
  base_url: 'https://api.openai.com/v1',
  model: '',
  api_style: 'openai_chat',
  connect_timeout: 10,
  request_timeout: 120,
  max_retries: 3,
  use_stream: false,
  allow_local: false,
  enabled: true,
};

export function createSettings(api, client, context) {
  const { badge, button, numberInput, section, select } = createUi(api);
  const root = el('div', 'plugin-stm-ai-assistant-view');
  const state = {
    providers: [], usage: null, status: null, suggestions: [], editing: null,
    settings: null,
    /** 建议队列里「已经在标签库里」而被隐藏的条数（只用于提示） */
    suggestionsHidden: 0,
    /** 标签池：分类白/黑名单 + 每分类规则 + 最大总数量 + 生效效果 */
    tagPool: null,
    tagPoolCategories: [],
    tagPoolInfo: {},
    tagPoolEffect: null,
    tagPoolWarnings: [],
    tagPoolMax: 8,
    tagPoolBudget: 1200,
    tagPoolRules: {},
    tagPoolDefaultConfidence: 0.5,
  };

  const overviewSection = section('概览');
  const overviewBox = el('div', 'plugin-stm-ai-assistant-row');

  const capabilitySection = section(
    '模型能力',
    '上下文长度与输出长度上限是全局口径：档案里 max_tokens 写 0 时跟随这里；'
    + '推理开关与思考等级只对 OpenAI 风格协议注入（Anthropic 的 thinking 语义不同，暂不注入）。',
  );
  const capabilityBox = el('div', 'plugin-stm-ai-assistant-capability');

  const providerSection = section('Provider', '每个供应商可配置独立的地址、模型与密钥；密钥存在主程序的 secrets.json（不回显）。');
  const providerList = el('div', 'plugin-stm-ai-assistant-provider-list');
  const providerEditor = el('div', 'plugin-stm-ai-assistant-provider-editor');
  let editorControls = [];

  // 标签池：决定 AI 能看到哪些标签（按标签分类只取 / 排除）。
  // 放在模型能力之后、Provider 之前 —— 它属于「AI 该看什么」，与模型能力同一层。
  const tagPoolSection = section(
    '标签池',
    '决定 AI 能看到哪些标签：按「标签分类」只取一部分，或排除某些分类。两项都留空 = 全部标签都可用。',
  );
  const tagPoolBox = el('div', 'plugin-stm-ai-assistant-tagpool');

  function destroyEditorControls() {
    editorControls.forEach((control) => {
      if (control && typeof control.destroy === 'function') control.destroy();
    });
    editorControls = [];
  }

  const usageSection = section('用量台账');
  const usageBox = el('div', 'plugin-stm-ai-assistant-usage');

  const suggestionSection = section('新标签建议', 'AI 提出的、标签库中不存在的标签；插件不会自动创建标签。');
  const suggestionBox = el('div', 'plugin-stm-ai-assistant-suggestions');

  const addProviderRow = el('div', 'plugin-stm-ai-assistant-row');
  addProviderRow.append(
    button('新增 Provider', 'is-secondary', () => startEdit(Object.assign({}, BLANK_PROVIDER))),
  );

  providerSection.append(providerList, addProviderRow, providerEditor);

  root.append(
    overviewSection, capabilitySection, tagPoolSection, providerSection, usageSection,
    suggestionSection,
  );
  overviewSection.append(overviewBox);
  capabilitySection.append(capabilityBox);
  tagPoolSection.append(tagPoolBox);

  // ---------------------------------------------------------------- 渲染

  function renderOverview() {
    clear(overviewBox);
    const status = state.status || {};
    overviewBox.append(
      badge(`v${(status.plugin && status.plugin.version) || '?'}`, 'is-muted'),
      badge(`档案 ${(status.profiles && status.profiles.count) || 0}`),
      badge(`Provider ${(status.providers && status.providers.count) || 0}`),
      badge(`待审批标签 ${status.suggestions || 0}`, (status.suggestions ? 'is-warn' : 'is-muted')),
    );
  }

  function renderCapabilities() {
    clear(capabilityBox);
    const data = state.settings || {};
    const caps = data.model_capabilities || {};
    const limits = data.limits || {};
    const effortOptions = [
      { value: 'auto', label: 'auto（供应商默认）' },
      { value: 'low', label: 'low（低）' },
      { value: 'medium', label: 'medium（中）' },
      { value: 'high', label: 'high（高）' },
    ];

    const fields = {};
    fields.context_length = numberInput(
      caps.context_length || (data.defaults || {}).context_length || 262144,
      { min: (limits.context_length || {}).min || 8192, step: 1 },
    );
    fields.max_output_tokens = numberInput(
      caps.max_output_tokens || (data.defaults || {}).max_output_tokens || 32768,
      { min: (limits.max_output_tokens || {}).min || 256, step: 1 },
    );
    fields.reasoning_enabled = checkbox(
      caps.reasoning_enabled !== false, '启用推理（关闭后请求带 reasoning_effort=none）',
    );
    fields.reasoning_effort = select(effortOptions, caps.reasoning_effort || 'auto');

    const grid = el('div', 'plugin-stm-ai-assistant-grid');
    grid.append(
      field('上下文长度（token）',
        fields.context_length,
        `模型窗口；默认 ${formatNumber((data.defaults || {}).context_length || 262144)}`),
      field('输出长度上限（token）',
        fields.max_output_tokens,
        `单次调用的最大输出；默认 ${formatNumber((data.defaults || {}).max_output_tokens || 32768)}。`
        + '档案 max_tokens 写 0 表示跟随此值'),
      field('思考等级', fields.reasoning_effort, '仅「启用推理」时生效'),
    );

    const actions = el('div', 'plugin-stm-ai-assistant-row');
    actions.append(
      fields.reasoning_enabled,
      button('保存模型能力', 'is-primary', () => saveCapabilities(fields)),
      button('恢复默认', 'is-secondary', () => resetCapabilities(fields)),
    );

    capabilityBox.append(grid, actions);
  }

  async function saveCapabilities(fields) {
    const payload = {
      context_length: Number(fields.context_length.querySelector('input').value) || 0,
      max_output_tokens: Number(fields.max_output_tokens.querySelector('input').value) || 0,
      reasoning_enabled: fields.reasoning_enabled.querySelector('input').checked,
      reasoning_effort: fields.reasoning_effort.value,
    };
    try {
      const data = await client.saveSettings(payload);
      state.settings = Object.assign({}, state.settings, { model_capabilities: data.model_capabilities });
      renderCapabilities();
      api.host.toast('模型能力已保存', 2400, 'check');
    } catch (error) {
      api.host.toast(`保存失败：${error.message}`, 4200, 'close');
    }
  }

  function resetCapabilities(fields) {
    const defaults = (state.settings && state.settings.defaults) || {};
    fields.context_length.querySelector('input').value = defaults.context_length || 262144;
    fields.max_output_tokens.querySelector('input').value = defaults.max_output_tokens || 32768;
    fields.reasoning_enabled.querySelector('input').checked = true;
    fields.reasoning_effort.value = defaults.reasoning_effort || 'auto';
  }

  // ---------------------------------------------------------------- 标签池

  /** 分类输入的分隔符：中英文逗号 / 分号 / 顿号 / 换行都认（用户不会只打英文逗号）。 */
  const CATEGORY_SPLIT = /[,，;；、\n\r]+/;

  function splitCategories(text) {
    const result = [];
    String(text || '').split(CATEGORY_SPLIT).forEach((part) => {
      const name = part.trim();
      if (name && !result.includes(name)) result.push(name);
    });
    return result;
  }

  function joinCategories(list) {
    return Array.from(new Set(list)).join('，');
  }

  /**
   * 字段标题旁的 `?`：点开在**字段下方**展开说明（不用绝对定位浮层——面板自建了
   * `overflow-y: auto` 的滚动区，浮层会被裁掉，与下拉菜单同一个坑）。
   *
   * 说明文字面向非程序员写：留空等于什么、多个怎么分隔、名字从哪来、会不会误伤。
   */
  function helpNote(title, paragraphs) {
    const panel = el('div', 'plugin-stm-ai-assistant-help-panel');
    panel.hidden = true;
    panel.append(el('strong', 'plugin-stm-ai-assistant-help-title', title));
    paragraphs.forEach((text) => {
      panel.append(el('p', 'plugin-stm-ai-assistant-help-line', text));
    });

    const trigger = el('button', 'plugin-stm-ai-assistant-help-btn', '?');
    trigger.type = 'button';
    trigger.title = `怎么填写：${title}`;
    trigger.setAttribute('aria-expanded', 'false');
    trigger.addEventListener('click', (event) => {
      // 阻止冒泡：外层是 label 时点按钮会同时把焦点丢给输入框
      event.preventDefault();
      event.stopPropagation();
      panel.hidden = !panel.hidden;
      trigger.setAttribute('aria-expanded', String(!panel.hidden));
    });

    return { button: trigger, panel };
  }

  function helpField(labelText, control, note, hint = '') {
    const wrapper = el('div', 'plugin-stm-ai-assistant-field');
    const head = el('div', 'plugin-stm-ai-assistant-field-head');
    head.append(el('span', 'plugin-stm-ai-assistant-field-label', labelText), note.button);
    wrapper.append(head, control);
    if (hint) wrapper.append(el('span', 'plugin-stm-ai-assistant-field-hint', hint));
    wrapper.append(note.panel);
    return wrapper;
  }

  const HELP_INCLUDE = [
    '留空 = 不限制：AI 可以从全部标签里挑。',
    '填了就只从这些分类里挑标签；「没有分类」的标签也会被排除。',
    '多个分类用逗号隔开即可（中文逗号、英文逗号、顿号、换行都行），例如：题材，玩法。',
    '名字要和标签库里的分类名完全一致。不确定就点下面分类右边的「只取」，会自动填进来。',
  ];

  const HELP_EXCLUDE = [
    '留空 = 不排除。',
    '填了以后，这些分类下的标签不会给 AI 看，AI 也不会把它们写进卡片。',
    '多个分类用逗号隔开，例如：成人，血腥。',
    '主程序里设成「隔离」的分类会自动排除，不用在这里再填一遍。',
    '这些分类是「冻结」的：连「建议删除标签」也不会出现在这里面的标签上，'
      + '你自己维护的标签不会被 AI 动。',
    '同一个分类不能同时填进「只取」和「排除」。',
  ];

  const HELP_RULES = [
    '置信度：AI 给每个标签打 0–1 分，低于这个值的标签不会被采用。'
      + '没有单独设置的分类默认按 0.5 算。',
    '设置数量：这个分类最多保留几个标签（按置信度从高到低取）。'
      + '填 0 = 这个分类一个都不要；留空 = 不单独限制。',
    '最大总数量：一次分析最多打几个标签，各分类的「设置数量」加起来不能超过它。',
    '没单独设置的分类：置信度按 0.5、数量只受总数量约束。',
  ];

  /** 分类行上的规则摘要（折叠状态下也能一眼看到门槛与配额）。 */
  function formatRuleSummary(rule, defaultConfidence) {
    const confidence = rule && rule.confidence_min;
    const quota = rule && rule.max_tags;
    const parts = [
      confidence == null
        ? `阈值 默认 ${Number(defaultConfidence).toFixed(2)}`
        : `阈值 ${Number(confidence).toFixed(2)}`,
    ];
    if (quota === 0) parts.push('不选');
    else if (quota == null) parts.push('数量不限');
    else parts.push(`最多 ${quota} 个`);
    return parts.join(' · ');
  }

  function renderTagPool() {
    clear(tagPoolBox);
    const pool = state.tagPool || {};
    const categories = state.tagPoolCategories || [];
    const info = state.tagPoolInfo || {};
    const defaultConfidence = Number(state.tagPoolDefaultConfidence ?? 0.5);
    // 工作副本：输入过程中改它，点「保存标签池」才落盘
    const rules = JSON.parse(JSON.stringify(state.tagPoolRules || {}));
    let maxTags = Number(state.tagPoolMax) || 8;

    // ---- 清理「孤儿规则」----
    // 分类在标签库里被改名或删掉后，它的规则仍然留在配置里：渲染遍历 categories
    // 所以看不见、也删不掉它，保存时却会把它重新 POST 回去，还被算进配额求和 ——
    // 结果是「已分配 N / 超出 M」永远对不上，且没有任何入口能清掉。
    // 这里在渲染前把它们摘出来，下面显式提示并允许一键清理。
    //
    // 守卫：分类清单还没加载到（`state.tagPoolCategories` 为空）时**一律不动**。
    // 否则首屏渲染会把全部规则当成孤儿清空，用户一点保存就丢光了。
    const categoriesLoaded = Array.isArray(state.tagPoolCategories)
      && state.tagPoolCategories.length > 0;
    const knownNames = new Set(categories.map((item) => item.name));
    const orphanRules = categoriesLoaded
      ? Object.keys(rules).filter((name) => !knownNames.has(name))
      : [];
    orphanRules.forEach((name) => { delete rules[name]; });

    // ---- 最大总数量 ----
    const maxInput = numberInput(
      maxTags,
      { min: 1, max: 20, step: 1 },
      (value) => {
        maxTags = Number(value) || maxTags;
        updateAllocation();
      },
    );
    const maxField = helpField(
      '最大总数量（一次最多打几个标签）',
      maxInput,
      helpNote('新的置信度 / 数量怎么用', HELP_RULES),
      '范围 1–20。各分类的「设置数量」之和不能超过它；总上限可以大于各分类之和。',
    );

    // ---- 标签池 token 预算：列多少标签给模型看 ----
    const budgetInput = numberInput(
      Number(state.tagPoolBudget) || 1200,
      { min: 200, max: 20000, step: 100 },
    );
    const budgetField = helpField(
      '标签池 token 预算',
      budgetInput,
      helpNote('这个预算影响什么', [
        '给 AI 列出标签库时最多花多少 token。默认 1200，大约能列 60–100 个标签。',
        '调大 = 模型能看到更多已有标签，减少「把已有标签当成新标签」；'
          + '代价是每次分析的 prompt 更长、更贵。',
        '预算外的标签不会列出（它们仍可被打标，只是模型没见过，可能提成「新标签」）。',
        '会被「模型能力 → 上下文长度」夹住；单个提示词档案里也能单独覆盖。',
      ]),
      '范围 200–20000。默认 1200 约 60–100 个标签；越大越贵。',
    );
    const limitsGrid = el('div', 'plugin-stm-ai-assistant-grid');
    limitsGrid.append(maxField, budgetField);

    // ---- 只取 / 排除 ----
    const includeBox = textarea(joinCategories(pool.categories_include || []), 2);
    includeBox.placeholder = '留空 = 不限制。例如：题材，玩法';
    const excludeBox = textarea(joinCategories(pool.categories_exclude || []), 2);
    excludeBox.placeholder = '留空 = 不排除。例如：成人，血腥';

    const grid = el('div', 'plugin-stm-ai-assistant-grid');
    grid.append(
      helpField(
        '只取这些分类（categories_include）',
        includeBox,
        helpNote('怎么填「只取这些分类」', HELP_INCLUDE),
        '分类名前后的空格会被忽略；留空 = 全部标签都可用。',
      ),
      helpField(
        '排除这些分类（categories_exclude）',
        excludeBox,
        helpNote('怎么填「排除这些分类」', HELP_EXCLUDE),
        '被排除的分类是硬过滤：AI 看不到，也写不进去。',
      ),
    );

    // ---- 分配情况（分类数量之和 vs 总上限）----
    const allocation = el('div', 'plugin-stm-ai-assistant-tagpool-allocation');

    function updateAllocation() {
      const quotas = [];
      Object.keys(rules).forEach((name) => {
        const quota = rules[name] && rules[name].max_tags;
        if (quota != null && Number.isFinite(Number(quota))) {
          quotas.push(`${name} ${Number(quota)}`);
        }
      });
      const total = Object.keys(rules).reduce((sum, name) => {
        const quota = rules[name] && rules[name].max_tags;
        return quota != null && Number.isFinite(Number(quota)) ? sum + Number(quota) : sum;
      }, 0);
      const over = total > maxTags;
      clear(allocation);
      allocation.classList.toggle('is-over', over);
      allocation.append(
        el(
          'span',
          'plugin-stm-ai-assistant-tagpool-allocation-text',
          `已分配 ${total} / 总上限 ${maxTags}（剩余 ${Math.max(0, maxTags - total)}）`,
        ),
        badge(over ? `超出 ${total - maxTags} 个` : '在范围内', over ? 'is-error' : 'is-ok'),
      );
      if (quotas.length) {
        allocation.append(el(
          'span',
          'plugin-stm-ai-assistant-field-hint',
          `分类数量：${quotas.join('、')}`
            + (over ? '（保存后实际按总上限截断，分类配额只作参考）' : ''),
        ));
      }
      // 孤儿规则（分类已改名/删除）会在下次保存时被清掉——先说清楚，别让用户以为丢了设置
      if (orphanRules.length) {
        allocation.append(el(
          'span',
          'plugin-stm-ai-assistant-field-hint',
          `另有 ${orphanRules.length} 个分类设置已失效（分类不存在了：${orphanRules.join('、')}），`
            + '保存时会一并清除。',
        ));
      }
    }

    // ---- 现有分类：点一下按钮就把名字填进对应输入框（不用手打）----
    // 这里的数量按「卡片库全部标签」统计：插件读的是宿主给的标签库，**包含**
    // 角色卡「隔离区」里那些卡片身上的标签，而管理器列表不显示隔离区，
    // 所以两边数量可能对不上 —— 直接在标题后括号提醒，免得用户以为插件算错了。
    const pickerHead = el('div', 'plugin-stm-ai-assistant-tagpool-picker-head');
    pickerHead.append(
      el(
        'span',
        'plugin-stm-ai-assistant-field-label',
        `现有标签分类（共 ${categories.length} 个）`,
      ),
      el(
        'span',
        'plugin-stm-ai-assistant-tagpool-picker-note',
        '（注意：下面的标签数量包含「角色卡-隔离区」里的标签 —— 管理器不显示隔离区，'
          + '核对数量时可能对不上，属正常现象）',
      ),
    );
    const picker = el('div', 'plugin-stm-ai-assistant-tagpool-categories');

    const rows = [];
    categories.forEach((item) => {
      const row = el('div', 'plugin-stm-ai-assistant-tagpool-category');
      const head = el('div', 'plugin-stm-ai-assistant-tagpool-category-head');
      const name = el('span', 'plugin-stm-ai-assistant-tagpool-category-name', item.name);
      const meta = el(
        'span',
        'plugin-stm-ai-assistant-tagpool-category-meta',
        `${item.tags || 0} 个标签`,
      );
      const summary = el(
        'span',
        'plugin-stm-ai-assistant-tagpool-rule-summary',
        formatRuleSummary(rules[item.name], defaultConfidence),
      );
      const stateBadge = el('span', 'plugin-stm-ai-assistant-tagpool-category-state');

      // ---- 展开的规则面板：置信度 + 设置数量 ----
      const panel = el('div', 'plugin-stm-ai-assistant-tagpool-rule-panel');
      panel.hidden = true;
      const rule = rules[item.name] || {};
      const confInput = numberInput(
        rule.confidence_min == null ? '' : rule.confidence_min,
        { min: 0, max: 1, step: 0.05, placeholder: `默认 ${defaultConfidence}` },
      );
      const quotaInput = numberInput(
        rule.max_tags == null ? '' : rule.max_tags,
        { min: 0, max: 20, step: 1, placeholder: '不限' },
      );

      function applyRule() {
        const confRaw = confInput.querySelector('input').value.trim();
        const quotaRaw = quotaInput.querySelector('input').value.trim();
        const next = {};
        if (confRaw !== '') next.confidence_min = Number(confRaw);
        if (quotaRaw !== '') next.max_tags = Number(quotaRaw);
        if (Object.keys(next).length) rules[item.name] = next;
        else delete rules[item.name];
        summary.textContent = formatRuleSummary(rules[item.name], defaultConfidence);
        updateAllocation();
      }

      confInput.querySelector('input').addEventListener('input', applyRule);
      quotaInput.querySelector('input').addEventListener('input', applyRule);
      const clearRule = button('清除本分类设置', 'is-secondary', () => {
        confInput.querySelector('input').value = '';
        quotaInput.querySelector('input').value = '';
        applyRule();
      });

      const panelGrid = el('div', 'plugin-stm-ai-assistant-grid');
      panelGrid.append(
        field('标签置信度', confInput, '低于它的标签不打；留空 = 默认 0.5（0–1）'),
        field('设置数量', quotaInput, '最多保留几个；0 = 一个都不选；留空 = 不限'),
      );
      panel.append(panelGrid, clearRule);

      const toggle = button('设置', 'is-secondary', () => {
        panel.hidden = !panel.hidden;
        toggle.textContent = panel.hidden ? '设置' : '收起';
      });
      const takeBtn = button('只取', 'is-secondary', () => {
        moveCategory(includeBox, excludeBox, item.name);
        paint();
      });
      const banBtn = button('排除', 'is-secondary', () => {
        moveCategory(excludeBox, includeBox, item.name);
        paint();
      });

      head.append(name, meta);
      if (item.isolated) head.append(badge('隔离', 'is-warn'));
      head.append(summary, stateBadge, toggle, takeBtn, banBtn);
      row.append(head, panel);
      picker.append(row);
      rows.push({ item, stateBadge, row });
    });

    if (!categories.length) {
      picker.append(el(
        'p',
        'plugin-stm-ai-assistant-hint',
        '（标签库里还没有分类：先到主程序的标签管理里给标签分组，这里才会出现可点的分类）',
      ));
    }

    /** 把输入框里的内容反映到分类行的状态标记上（只读展示，不改数据）。 */
    function paint() {
      const include = splitCategories(includeBox.value);
      const exclude = splitCategories(excludeBox.value);
      rows.forEach(({ item, stateBadge, row }) => {
        const taken = include.includes(item.name);
        const banned = exclude.includes(item.name);
        stateBadge.textContent = taken ? '✓ 只取' : (banned ? '✕ 排除' : '');
        stateBadge.className = 'plugin-stm-ai-assistant-tagpool-category-state'
          + (taken ? ' is-include' : '') + (banned ? ' is-exclude' : '');
        row.classList.toggle('is-include', taken);
        row.classList.toggle('is-exclude', banned);
      });
    }

    includeBox.addEventListener('input', paint);
    excludeBox.addEventListener('input', paint);
    paint();
    updateAllocation();

    // ---- 生效效果（保存后才知道到底挡住了什么）----
    const effectBox = el('div', 'plugin-stm-ai-assistant-tagpool-effect');
    const effect = state.tagPoolEffect;
    if (effect) {
      effectBox.append(badge(`可用标签 ${formatNumber(effect.allowed)}`, 'is-ok'));
      effectBox.append(badge(`被排除 ${formatNumber(effect.blocked)}`, effect.blocked ? 'is-warn' : 'is-muted'));
      if (effect.blocked_sample && effect.blocked_sample.length) {
        effectBox.append(el(
          'span',
          'plugin-stm-ai-assistant-field-hint',
          `被排除示例：${effect.blocked_sample.join('、')}`,
        ));
      }
    } else {
      const total = Number(info.total_tags || 0);
      effectBox.append(badge(`标签库共 ${formatNumber(total)} 个`, 'is-muted'));
      if (info.uncategorized) {
        effectBox.append(badge(
          `未分类 ${formatNumber(info.uncategorized)} 个`,
          'is-warn',
        ));
      }
    }

    const warnings = state.tagPoolWarnings || [];
    const warningBox = el('div', 'plugin-stm-ai-assistant-tagpool-warnings');
    warnings.forEach((text) => {
      warningBox.append(el('p', 'plugin-stm-ai-assistant-tagpool-warning', `⚠ ${text}`));
    });

    const actions = el('div', 'plugin-stm-ai-assistant-row');
    actions.append(
      button('保存标签池', 'is-primary', () => saveTagPool({
        includeBox,
        excludeBox,
        rules,
        maxTags: Number(maxInput.querySelector('input').value),
        budgetTokens: Number(budgetInput.querySelector('input').value),
      })),
      button('清空', 'is-secondary', () => {
        includeBox.value = '';
        excludeBox.value = '';
        paint();
      }),
    );

    tagPoolBox.append(
      limitsGrid, grid, pickerHead, picker, allocation, effectBox, warningBox, actions,
    );
  }

  /** 点分类按钮：加进目标框，并从另一个框里移除（两边互斥）。 */
  function moveCategory(targetBox, otherBox, name) {
    const target = splitCategories(targetBox.value);
    if (!target.includes(name)) target.push(name);
    targetBox.value = joinCategories(target);

    const other = splitCategories(otherBox.value).filter((item) => item !== name);
    if (other.length !== splitCategories(otherBox.value).length) {
      otherBox.value = joinCategories(other);
    }
  }

  async function saveTagPool({ includeBox, excludeBox, rules, maxTags, budgetTokens }) {
    const include = splitCategories(includeBox.value);
    const exclude = splitCategories(excludeBox.value);
    const overlap = include.filter((name) => exclude.includes(name));
    if (overlap.length) {
      await api.ui.confirm({
        title: '无法保存',
        message: `同一个分类不能同时「只取」和「排除」：${overlap.join('、')}`,
        danger: true,
      });
      return;
    }

    if (!Number.isInteger(maxTags) || maxTags < 1 || maxTags > 20) {
      await api.ui.confirm({
        title: '无法保存',
        message: '「最大总数量」要是 1–20 之间的整数。',
        danger: true,
      });
      return;
    }

    if (!Number.isInteger(budgetTokens) || budgetTokens < 200 || budgetTokens > 20000) {
      await api.ui.confirm({
        title: '无法保存',
        message: '「标签池 token 预算」要是 200–20000 之间的整数。',
        danger: true,
      });
      return;
    }

    // ---- 校验：置信度 0–1、设置数量 0–20，且各分类数量之和 ≤ 总上限 ----
    const problems = [];
    const breakdown = [];
    let quotaTotal = 0;
    Object.keys(rules).forEach((name) => {
      const rule = rules[name] || {};
      if (rule.confidence_min != null) {
        const confidence = Number(rule.confidence_min);
        if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
          problems.push(`「${name}」的置信度要在 0–1 之间（现在填的是 ${rule.confidence_min}）`);
        }
      }
      if (rule.max_tags != null) {
        const quota = Number(rule.max_tags);
        if (!Number.isInteger(quota) || quota < 0 || quota > 20) {
          problems.push(`「${name}」的设置数量要是 0–20 的整数（现在填的是 ${rule.max_tags}）`);
        } else {
          quotaTotal += quota;
          breakdown.push(`${name} ${quota}`);
        }
      }
    });

    if (problems.length) {
      await api.ui.confirm({
        title: '无法保存：有填错的数值',
        message: problems.join('\n'),
        danger: true,
      });
      return;
    }

    if (quotaTotal > maxTags) {
      // 用户选定的行为：**只提醒，不拦保存**。实际打标按总上限截断，
      // 各分类配额此时只是参考值。
      const proceed = await api.ui.confirm({
        title: '提醒：分类数量之和超过总上限',
        message: `最大总数量是 ${maxTags} 个，各分类的设置数量加起来是 ${quotaTotal} 个`
          + `（超出 ${quotaTotal - maxTags} 个）。\n\n当前分配：${breakdown.join('、')}\n\n`
          + `仍要保存吗？保存后实际打标会按总上限 ${maxTags} 个截断（按置信度从高到低），`
          + '各分类的「设置数量」只作参考。',
      });
      if (!proceed) return;
    }

    try {
      const data = await client.saveTagPool({
        categories_include: include,
        categories_exclude: exclude,
        max_tags: maxTags,
        budget_tokens: budgetTokens,
        rules,
      });
      state.tagPool = data.tag_pool || {};
      state.tagPoolCategories = data.categories || [];
      state.tagPoolMax = data.max_tags != null ? data.max_tags : maxTags;
      state.tagPoolBudget = data.budget_tokens != null ? data.budget_tokens : budgetTokens;
      state.tagPoolRules = data.rules || {};
      if (data.default_confidence != null) {
        state.tagPoolDefaultConfidence = data.default_confidence;
      }
      state.tagPoolInfo = {
        total_tags: data.total_tags,
        uncategorized: data.uncategorized,
        isolated: data.isolated || [],
      };
      state.tagPoolEffect = data.effect || null;
      state.tagPoolWarnings = data.warnings || [];
      renderTagPool();
      api.host.toast('标签池已保存（对新作业生效）', 2800, 'check');
    } catch (error) {
      await api.ui.confirm({
        title: '保存失败',
        message: error.message,
        danger: true,
      });
    }
  }

  function renderProviders() {
    clear(providerList);
    if (!state.providers.length) {
      providerList.append(el('p', 'plugin-stm-ai-assistant-hint', '（尚未配置 provider）'));
      return;
    }

    state.providers.forEach((item) => {
      const row = el('div', 'plugin-stm-ai-assistant-provider-item');
      const head = el('div', 'plugin-stm-ai-assistant-provider-head');
      head.append(
        badge(item.id, item.is_default ? 'is-ok' : 'is-muted'),
        el('span', 'plugin-stm-ai-assistant-provider-name', item.name || item.id),
        el('span', 'plugin-stm-ai-assistant-hint', `${item.base_url} · 模型 ${item.model || '(未设)'}`),
        badge(item.has_api_key ? '密钥已设置' : (item.has_fallback_key ? '使用默认密钥' : '密钥未设置'),
          (item.has_api_key || item.has_fallback_key) ? 'is-ok' : 'is-error'),
      );

      const actions = el('div', 'plugin-stm-ai-assistant-row');
      actions.append(
        button('编辑', 'is-secondary', () => startEdit(item)),
        button('测试连接', 'is-secondary', () => testProvider(item)),
      );
      if (!item.is_default) {
        actions.append(
          button('设为默认', 'is-secondary', () => setDefault(item)),
          button('删除', 'is-danger', () => removeProvider(item)),
        );
      }
      head.append(actions);
      row.append(head);
      providerList.append(row);
    });
  }

  function startEdit(provider) {
    state.editing = Object.assign({}, provider);
    renderEditor();
  }

  function renderEditor() {
    destroyEditorControls();
    clear(providerEditor);
    if (!state.editing) return;

    const draft = state.editing;
    const fields = {};

    const grid = el('div', 'plugin-stm-ai-assistant-grid');
    fields.id = input(draft.id, 'text', '小写字母/数字/连字符，如 openai');
    fields.name = input(draft.name || draft.id);
    fields.base_url = input(draft.base_url, 'text', '只填到 /v1 即可；协议路径会自动补');
    fields.model = input(draft.model, 'text', '如 gpt-4o-mini / claude-sonnet-4-5');
    fields.api_style = select(API_STYLES, draft.api_style);
    editorControls = [fields.api_style];
    fields.connect_timeout = numberInput(draft.connect_timeout, { min: 1, step: 1 });
    fields.request_timeout = numberInput(draft.request_timeout, { min: 1, step: 1 });
    fields.max_retries = numberInput(draft.max_retries, { min: 0, step: 1 });

    // 协议差异提示：跟着选择器变化，避免用户填了 Anthropic 地址却期待 stop 生效
    const styleHint = el('p', 'plugin-stm-ai-assistant-hint',
      API_STYLE_HINTS[draft.api_style] || '');
    fields.api_style.addEventListener('change', () => {
      styleHint.textContent = API_STYLE_HINTS[fields.api_style.value] || '';
    });

    // 模型列表：拉取失败必须能退回手填（很多网关不实现 /models）。
    //
    // 用插件自己的 `styledSelect` 而不是原生 `<select>`：原生 select 只能靠
    // `plugin-stm-ai-assistant-select` 这个 wrapper 类拿到透明底/无边框，
    // 拿不到 trigger 那套外观，和「接口协议」等下拉框放在一起明显不是一套控件。
    // 候选列表在拉取完成后用 `setOptions()` 填充，因此这里先给一个占位项。
    const modelRow = el('div', 'plugin-stm-ai-assistant-row');
    const modelSelect = select([{ value: '', label: '（尚未拉取）' }], '');
    // 下拉控件自己有 document 级监听器，随编辑器重建一起销毁（editorControls 契约）
    editorControls.push(modelSelect);
    modelSelect.addEventListener('change', () => {
      if (modelSelect.value) fields.model.value = modelSelect.value;
    });
    modelRow.append(
      modelSelect,
      button('拉取模型列表', 'is-secondary', () => fetchModels(fields, modelSelect)),
    );

    // 模型候选行需要横向空间（模型名很长，还要并排放拉取按钮），让它跨两列。
    const modelField = field(
      '模型候选', modelRow, '点「拉取模型列表」从 /models 读取；失败时直接手填上方',
    );
    modelField.classList.add('plugin-stm-ai-assistant-field--wide');

    grid.append(
      field('id', fields.id),
      field('名称', fields.name),
      field('API 地址', fields.base_url),
      field('默认模型', fields.model),
      modelField,
      field('接口协议', fields.api_style),
      styleHint,
      field('连接超时（秒）', fields.connect_timeout),
      field('读取超时（秒）', fields.request_timeout),
      field('最大重试次数', fields.max_retries),
    );

    const toggles = el('div', 'plugin-stm-ai-assistant-row');
    fields.use_stream = checkbox(draft.use_stream, '使用流式请求（可更快取消）');
    fields.allow_local = checkbox(draft.allow_local, '允许本地地址（Ollama 等）');
    fields.enabled = checkbox(draft.enabled !== false, '启用');
    toggles.append(fields.use_stream, fields.allow_local, fields.enabled);

    const secretsGrid = el('div', 'plugin-stm-ai-assistant-grid');
    const apiKeyPlaceholder = draft.has_api_key
      ? `已设置（${draft.api_key_hint || '掩码不可用'}），留空表示不修改`
      : '尚未设置';
    fields.api_key = input('', 'password', apiKeyPlaceholder);
    fields.extra_headers = textarea('', 3);
    fields.extra_headers.placeholder = draft.has_extra_headers
      ? '已设置（不回显）；留空表示不修改，填 {} 表示清除'
      : '可选：JSON 对象，如 {"X-Title": "ST-Manager"}';

    secretsGrid.append(
      field('API Key', fields.api_key, '只保存在服务端；环境变量 STM_PLUGIN_STM_AI_ASSISTANT_PROVIDER_<ID>_API_KEY 优先'),
      field('自定义请求头（JSON）', fields.extra_headers),
    );

    const actions = el('div', 'plugin-stm-ai-assistant-row');
    actions.append(
      button('保存', 'is-primary', () => saveProvider(fields)),
      button('取消', 'is-secondary', () => {
        state.editing = null;
        renderEditor();
      }),
    );

    providerEditor.append(grid, toggles, secretsGrid, actions);
  }

  // ---------------------------------------------------------------- 操作

  async function fetchModels(fields, modelSelect) {
    // 未保存的 provider 也能拉：地址 + 表单里刚输入的 Key 就够，不必先保存。
    // 后端 `/models` 是「草稿型」端点（不存在 provider 也能用），未填的字段留空即可。
    const baseUrl = fields.base_url.value.trim();
    if (!baseUrl) {
      api.host.toast('先填 API 地址', 3200, 'close');
      return;
    }

    const apiKey = fields.api_key.value.trim();
    const headers = fields.extra_headers.value.trim();
    const hasSavedKey = Boolean(state.editing.has_api_key || state.editing.has_fallback_key);
    if (!apiKey && !hasSavedKey) {
      // 没有已保存的 Key、也没有新输入的 Key：本地先拦，省一次注定失败的请求
      api.host.toast('先填 API Key（保存前也可以拉取）', 3600, 'close');
      return;
    }

    const label = modelSelect.querySelector('.plugin-stm-ai-assistant-select-label');
    if (label) label.textContent = '拉取中…';
    modelSelect.disabled = true;

    try {
      const data = await client.listModels({
        provider_id: fields.id.value.trim().toLowerCase(),
        base_url: baseUrl,
        api_style: fields.api_style.value,
        allow_local: fields.allow_local.querySelector('input').checked,
        connect_timeout: Number(fields.connect_timeout.querySelector('input').value) || undefined,
        request_timeout: Number(fields.request_timeout.querySelector('input').value) || undefined,
        // 明文 Key 只在本次请求里用，不进任何日志/响应；留空表示沿用已保存的密钥
        api_key: apiKey || undefined,
        extra_headers: headers || undefined,
      });

      if (!data.ok) {
        modelSelect.setOptions([{ value: '', label: '（拉取失败：请手填模型名）' }]);
        modelSelect.value = '';
        api.host.toast(`拉取模型失败：${data.message || '未知原因'}`, 5200, 'close');
        return;
      }

      const options = [{ value: '', label: `（${data.models.length} 个模型）` }]
        .concat(data.models.map((name) => ({ value: name, label: name })));
      modelSelect.setOptions(options);
      modelSelect.value = data.models.includes(fields.model.value.trim())
        ? fields.model.value.trim()
        : '';
      api.host.toast(`已拉取 ${data.models.length} 个模型`, 2600, 'check');
    } catch (error) {
      // 异常路径也要把下拉从「拉取中…」恢复到可手填状态，否则它会一直停在提示文案上
      modelSelect.setOptions([{ value: '', label: '（拉取失败：请手填模型名）' }]);
      modelSelect.value = '';
      api.host.toast(`拉取模型失败：${error.message}`, 4200, 'close');
    } finally {
      modelSelect.disabled = false;
    }
  }

  async function saveProvider(fields) {
    const provider = {
      id: fields.id.value.trim().toLowerCase(),
      name: fields.name.value.trim(),
      base_url: fields.base_url.value.trim(),
      model: fields.model.value.trim(),
      api_style: fields.api_style.value,
      connect_timeout: Number(fields.connect_timeout.querySelector('input').value) || 10,
      request_timeout: Number(fields.request_timeout.querySelector('input').value) || 120,
      max_retries: Number(fields.max_retries.querySelector('input').value) || 0,
      use_stream: fields.use_stream.querySelector('input').checked,
      allow_local: fields.allow_local.querySelector('input').checked,
      enabled: fields.enabled.querySelector('input').checked,
    };

    try {
      await client.saveProvider(provider);

      const apiKey = fields.api_key.value.trim();
      const headers = fields.extra_headers.value.trim();
      if (apiKey || headers) {
        const payload = {};
        if (apiKey) payload.api_key = apiKey;
        if (headers) payload.extra_headers = headers;
        await client.saveProviderSecrets(provider.id, payload);
      }

      api.host.toast('已保存', 2400, 'check');
      state.editing = null;
      await load();
    } catch (error) {
      const detail = (error.detail && error.detail.errors) || [];
      api.host.toast(`保存失败：${error.message}${detail.length ? `（${detail.join('；')}）` : ''}`, 6000, 'close');
    }
  }

  async function testProvider(item) {
    try {
      const data = await client.testProvider(item.id);
      if (data.ok) {
        api.host.toast(`连接正常：${data.latency_ms}ms · 模型 ${data.model}`, 3200, 'check');
      } else {
        api.host.toast(`连接失败：${data.error} ${data.message || ''}`, 5000, 'close');
      }
    } catch (error) {
      api.host.toast(`测试失败：${error.message}`, 4000, 'close');
    }
  }

  async function setDefault(item) {
    try {
      await client.setDefaultProvider(item.id);
      api.host.toast(`默认 provider 已切换为 ${item.id}`, 2600, 'check');
      await load();
    } catch (error) {
      api.host.toast(`设置失败：${error.message}`, 3600, 'close');
    }
  }

  async function removeProvider(item) {
    const confirmed = await api.ui.confirm({
      title: `删除 provider「${item.id}」？`,
      message: '其密钥也会被一并清除，此操作不可撤销。',
      danger: true,
    });
    if (!confirmed) return;
    try {
      await client.deleteProvider(item.id);
      api.host.toast('已删除', 2400, 'check');
      await load();
    } catch (error) {
      api.host.toast(`删除失败：${error.message}`, 3600, 'close');
    }
  }

  function renderUsage() {
    clear(usageBox);
    const ledger = (state.usage && state.usage.ledger) || {};
    usageBox.append(
      badge(`prompt ${formatNumber(ledger.total_prompt_tokens)}`, 'is-muted'),
      badge(`completion ${formatNumber(ledger.total_completion_tokens)}`, 'is-muted'),
      badge(`作业 ${formatNumber(ledger.jobs)}`, 'is-muted'),
    );

    const recent = (state.usage && state.usage.recent) || [];
    if (!recent.length) {
      usageBox.append(el('p', 'plugin-stm-ai-assistant-hint', '（暂无用量记录）'));
      return;
    }

    const list = el('div', 'plugin-stm-ai-assistant-usage-list');
    recent.slice().reverse().forEach((record) => {
      list.append(el('div', 'plugin-stm-ai-assistant-usage-item',
        `${formatTime(record.recorded_at)} · ${record.model || '-'} · `
        + `${formatNumber(record.prompt_tokens)}/${formatNumber(record.completion_tokens)} token · `
        + `${record.items || 0} 张`));
    });
    usageBox.append(list);
  }

  function renderSuggestions() {
    clear(suggestionBox);
    if (!state.suggestions.length) {
      suggestionBox.append(el('p', 'plugin-stm-ai-assistant-hint', '（暂无新标签建议）'));
      if (state.suggestionsHidden) {
        suggestionBox.append(el(
          'p',
          'plugin-stm-ai-assistant-hint',
          `另有 ${formatNumber(state.suggestionsHidden)} 条建议其实已经在标签库里，已隐藏（清空队列可一并移除）。`,
        ));
      }
      return;
    }

    const list = el('div', 'plugin-stm-ai-assistant-suggestion-list');
    state.suggestions.forEach((item) => {
      list.append(el('div', 'plugin-stm-ai-assistant-suggestion-item',
        `${item.name} · 出现 ${item.count} 次 · ${item.reason || ''}`));
    });

    const actions = el('div', 'plugin-stm-ai-assistant-row');
    actions.append(button('清空建议队列', 'is-danger', async () => {
      const confirmed = await api.ui.confirm({
        title: '清空全部新标签建议？',
        message: '建议队列会被完全清空，AI 曾提出的标签需要重新分析才能再次出现。',
        danger: true,
      });
      if (!confirmed) return;
      try {
        await client.clearSuggestions(null);
        api.host.toast('已清空', 2400, 'check');
        await load();
      } catch (error) {
        api.host.toast(error.message, 3600, 'close');
      }
    }));

    suggestionBox.append(list, actions);
    if (state.suggestionsHidden) {
      suggestionBox.append(el(
        'p',
        'plugin-stm-ai-assistant-hint',
        `另有 ${formatNumber(state.suggestionsHidden)} 条建议其实已经在标签库里，已隐藏（清空队列可一并移除）。`,
      ));
    }
  }

  async function load() {
    try {
      const [status, providers, usage, suggestions, settings, tagPool] = await Promise.all([
        client.status(), client.providers(), client.usage(), client.suggestions(),
        client.settings(),
        // 旧后端没有这个端点时不能让整个设置页读失败 —— 标签池是增量能力
        client.tagPool().catch(() => ({})),
      ]);
      state.status = status;
      state.providers = providers.items || [];
      state.usage = usage;
      state.suggestions = suggestions.items || [];
      state.suggestionsHidden = Number(suggestions.hidden_existing) || 0;
      state.settings = settings;

      state.tagPool = tagPool.tag_pool || null;
      state.tagPoolCategories = tagPool.categories || [];
      state.tagPoolMax = tagPool.max_tags != null ? tagPool.max_tags : 8;
      state.tagPoolBudget = tagPool.budget_tokens != null ? tagPool.budget_tokens : 1200;
      state.tagPoolRules = tagPool.rules || {};
      state.tagPoolDefaultConfidence = tagPool.default_confidence != null
        ? tagPool.default_confidence
        : 0.5;
      state.tagPoolInfo = {
        total_tags: tagPool.total_tags,
        uncategorized: tagPool.uncategorized,
        isolated: tagPool.isolated || [],
      };
      state.tagPoolEffect = tagPool.effect || null;
      state.tagPoolWarnings = tagPool.warnings || [];
    } catch (error) {
      api.host.toast(`读取设置失败：${error.message}`, 3600, 'close');
    }

    renderOverview();
    renderCapabilities();
    renderTagPool();
    renderProviders();
    renderEditor();
    renderUsage();
    renderSuggestions();

    if (state.status && state.status.outbound_acknowledged === false) {
      const confirmed = await api.ui.confirm({
        title: '出站内容提示',
        message: 'AI 分析会把卡片内容发送到你配置的 provider 地址。确认已知晓？',
      });
      if (confirmed) {
        try {
          await client.ackOutbound();
        } catch (error) {
          api.host.toast(error.message, 3200, 'close');
        }
      }
    }
  }

  return {
    element: root,
    refresh: load,
    destroy: destroyEditorControls,
  };
}
