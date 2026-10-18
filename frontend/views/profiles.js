/**
 * 提示词档案编辑器：按模型分档的 system / user 模板、破限段、格式与字数规则。
 *
 * 保存前会调用 `/prompt/validate` 做未知变量检查（模板里出现未知变量必须在保存时被拒绝，
 * 见设计文档 §5.4）。
 */

import {
  checkbox,
  clear,
  createUi,
  el,
  field,
  formatTime,
  input,
  textarea,
} from '../dom.js';

const MISSING_POLICIES = [
  { value: 'error', label: '缺失必需变量时报错' },
  { value: 'empty', label: '缺失一律渲染为空' },
];

export function createProfiles(api, client, context) {
  const { badge, button, numberInput, section, select } = createUi(api);
  const root = el('div', 'plugin-stm-ai-assistant-view');
  const state = {
    list: [],
    current: null,
    isNew: false,
    variables: [],
    activeId: '',
  };

  const listSection = section('提示词档案', '按模型匹配（通配或前缀），优先级越高越优先。点击条目即可切换编辑对象。');
  const listBox = el('div', 'plugin-stm-ai-assistant-profile-list');
  const listActions = el('div', 'plugin-stm-ai-assistant-row');
  const editorBox = el('div', 'plugin-stm-ai-assistant-profile-editor');
  const variableHint = el('p', 'plugin-stm-ai-assistant-hint');
  let editorControls = [];

  function destroyEditorControls() {
    editorControls.forEach((control) => {
      if (control && typeof control.destroy === 'function') control.destroy();
    });
    editorControls = [];
  }

  listActions.append(
    button('新建档案', 'is-secondary', () => newProfile()),
    button('导入 JSON', 'is-secondary', () => importProfile()),
    button('导出全部', 'is-secondary', () => exportProfiles()),
    button('恢复出厂', 'is-danger', () => resetProfiles()),
  );

  listSection.append(listBox, listActions, variableHint);
  root.append(listSection, editorBox);

  // ---------------------------------------------------------------- 列表

  function renderList() {
    clear(listBox);
    if (!state.list.length) {
      listBox.append(el('p', 'plugin-stm-ai-assistant-hint', '（没有档案）'));
      return;
    }

    state.list.forEach((entry) => {
      const item = entry.index || entry;
      const row = el('div', 'plugin-stm-ai-assistant-profile-item');
      const editing = state.current && state.current.id === item.id;
      const isActive = state.activeId === item.id;
      if (editing) row.classList.add('is-active');
      if (isActive) row.classList.add('is-current');
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      row.setAttribute('aria-pressed', editing ? 'true' : 'false');
      row.title = '点击切换编辑对象';

      const tags = el('span', 'plugin-stm-ai-assistant-profile-tags');
      tags.append(badge(item.id, item.builtin ? 'is-muted' : 'is-ok'));
      if (isActive) tags.append(badge('当前使用', 'is-ok'));

      const main = el('div', 'plugin-stm-ai-assistant-profile-main');
      main.append(
        el('span', 'plugin-stm-ai-assistant-profile-name', item.name),
        el('span', 'plugin-stm-ai-assistant-hint',
          `${item.model_match} · 优先级 ${item.priority} · v${item.version}`),
      );
      main.append(tags);

      const actions = el('div', 'plugin-stm-ai-assistant-row plugin-stm-ai-assistant-profile-actions');
      if (!isActive) {
        actions.append(button('设为当前', 'is-secondary', () => setActiveProfile(item.id)));
      }
      actions.append(
        button('编辑', 'is-secondary', () => openProfile(item.id)),
        button('导出', 'is-secondary', () => exportProfiles(item.id)),
        button('复制', 'is-secondary', () => duplicateProfile(item.id)),
        button('删除', 'is-danger', () => deleteProfile(item)),
      );

      row.append(main, actions);

      // 整行可点：点空白处即切换编辑对象（按钮自身的事件优先，不冒泡处理）
      const selectRow = (event) => {
        if (event && event.target && event.target.closest
          && event.target.closest('button, input, select, textarea, a')) {
          return;
        }
        openProfile(item.id);
      };
      row.addEventListener('click', selectRow);
      row.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openProfile(item.id);
        }
      });

      listBox.append(row);
    });
  }

  // ---------------------------------------------------------------- 编辑器

  function newProfile() {
    state.isNew = true;
    state.current = {
      id: 'my-profile',
      name: '我的档案',
      model_match: '*',
      priority: 50,
      missing_policy: 'error',
      system: '',
      system_prelude: '',
      system_suffix: '',
      user_template: '',
      user_prelude: '',
      user_suffix: '',
      assistant_prefill: '',
      // 留空 → 渲染时用 outputSchemaOf() 的默认值（与后端 DEFAULT_OUTPUT_SCHEMA 对齐）
      output_schema: null,
      content_selector: null,
      apply_actions: [
        { from: 'summary', action: 'write_card_note' },
        { from: 'tags', action: 'apply_tags', params: {} },
        { from: 'suggested_new_tags', action: 'queue_suggestions' },
      ],
      stop: [],
      temperature: 0.6,
      top_p: 1.0,
      max_tokens: 2048,
      extra_body: {},
    };
    renderList();
    renderEditor();
  }

  async function openProfile(profileId) {
    try {
      const data = await client.profile(profileId);
      state.current = data.profile;
      state.isNew = false;
      // 关键：立刻重绘列表，否则高亮要等下一次切页签才更新
      renderList();
      renderEditor();
    } catch (error) {
      api.host.toast(`读取档案失败：${error.message}`, 3600, 'close');
    }
  }

  /** 把某个档案设为「当前使用」（写入 ui_data.active_profile_id）。 */
  async function setActiveProfile(profileId) {
    try {
      await client.setActiveProfile(profileId);
      state.activeId = profileId;
      context.profileId = profileId;
      api.host.toast(`当前档案已切换为 ${profileId}`, 2600, 'check');
      renderList();
    } catch (error) {
      api.host.toast(`切换失败：${error.message}`, 3600, 'close');
    }
  }

  function renderEditor() {
    destroyEditorControls();
    clear(editorBox);
    if (!state.current) {
      editorBox.append(el('p', 'plugin-stm-ai-assistant-hint', '选择左侧档案开始编辑，或新建一个。'));
      return;
    }

    const profile = state.current;
    const fields = {};

    const basicGrid = el('div', 'plugin-stm-ai-assistant-grid');
    fields.id = input(profile.id, 'text', '小写字母、数字与连字符');
    fields.name = input(profile.name);
    fields.model_match = input(profile.model_match, 'text', '如 gpt-* / deepseek-* / *');
    fields.priority = numberInput(profile.priority, { step: 1 });
    fields.missing_policy = select(MISSING_POLICIES, profile.missing_policy);
    editorControls = [fields.missing_policy];

    basicGrid.append(
      field('id', fields.id, '内置档案的 id 不可更改（改了等于新建）'),
      field('名称', fields.name),
      field('模型匹配', fields.model_match, '含 * 或 ? 按通配匹配；否则按前缀匹配'),
      field('优先级', fields.priority, '数字越大越优先'),
      field('变量缺失策略', fields.missing_policy),
    );

    const promptGrid = el('div', 'plugin-stm-ai-assistant-stack');
    fields.system_prelude = textarea(profile.system_prelude, 3);
    fields.system = textarea(profile.system, 10);
    fields.system_suffix = textarea(profile.system_suffix, 3);
    fields.user_prelude = textarea(profile.user_prelude, 3);
    fields.user_template = textarea(profile.user_template, 14);
    fields.user_suffix = textarea(profile.user_suffix, 3);
    fields.assistant_prefill = input(profile.assistant_prefill, 'text', '如 {（强制模型进入 JSON）');

    promptGrid.append(
      field('系统前缀（破限段）', fields.system_prelude),
      field('系统提示词', fields.system),
      field('系统后缀（破限段）', fields.system_suffix),
      field('用户前缀', fields.user_prelude),
      field('用户模板', fields.user_template),
      field('用户后缀', fields.user_suffix),
      field('Assistant 预填', fields.assistant_prefill),
    );

    const rulesGrid = el('div', 'plugin-stm-ai-assistant-grid');
    // 输出 schema（v2：格式与长度规则已合并进来）
    const schema = outputSchemaOf(profile);
    const summaryProp = schema.properties.summary || {};
    const tagsProp = schema.properties.tags || {};

    fields.json_only = checkbox(schema.json_only !== false, '强制 JSON 输出');
    fields.strip_think = checkbox(schema.strip_think !== false, '剥离思考段');
    fields.wrappers = input((schema.forbidden_wrappers || []).join(','), 'text', '逗号分隔');
    fields.required_fields = input((schema.required || []).join(','), 'text', '逗号分隔');
    fields.summary_min = numberInput(summaryProp.min_chars || 0, { min: 0, step: 1 });
    fields.summary_max = numberInput(summaryProp.max_chars || 0, { min: 0, step: 1 });
    fields.tags_min = numberInput(tagsProp.min_items || 0, { min: 0, step: 1 });
    fields.tags_max = numberInput(tagsProp.max_items || 0, { min: 0, step: 1 });
    fields.confidence = numberInput(tagsProp.min_confidence || 0, { min: 0, max: 1, step: 0.05 });
    fields.temperature = numberInput(profile.temperature, { min: 0, max: 2, step: 0.05 });
    fields.top_p = numberInput(profile.top_p, { min: 0, max: 1, step: 0.05 });
    fields.max_tokens = numberInput(profile.max_tokens, { min: 1, step: 1 });
    fields.stop = textarea((profile.stop || []).join('\n'), 3);
    fields.extra_body = textarea(JSON.stringify(profile.extra_body || {}, null, 2), 4);
    fields.output_schema = textarea(JSON.stringify(schema, null, 2), 14);

    rulesGrid.append(
      field('概览下限（字）', fields.summary_min),
      field('概览上限（字）', fields.summary_max),
      field('标签下限', fields.tags_min),
      field('标签上限', fields.tags_max),
      field('置信度下限', fields.confidence),
      field('temperature', fields.temperature),
      field('top_p', fields.top_p),
      field('max_tokens', fields.max_tokens),
    );
    editorControls = editorControls.concat([
      fields.priority, fields.summary_min, fields.summary_max, fields.tags_min,
      fields.tags_max, fields.confidence, fields.temperature, fields.top_p,
      fields.max_tokens,
    ]);

    const toggles = el('div', 'plugin-stm-ai-assistant-row');
    toggles.append(fields.json_only, fields.strip_think);

    const formatGrid = el('div', 'plugin-stm-ai-assistant-grid');
    formatGrid.append(
      field('禁止的包裹', fields.wrappers, '逗号分隔，如 ```json,```'),
      field('必需字段', fields.required_fields, '逗号分隔'),
    );

    const stopGrid = el('div', 'plugin-stm-ai-assistant-grid');
    stopGrid.append(
      field('stop 序列（每行一个）', fields.stop),
      field('extra_body（JSON）', fields.extra_body, '合并进请求体，用于供应商特有参数'),
    );

    // 高级：完整 schema 与写回动作。上面的控件改的是同一份 schema 的常用项，
    // 这里给需要 enum / 嵌套对象 / 自定义动作的场景留出口。
    const advanced = el('details', 'plugin-stm-ai-assistant-advanced');
    advanced.append(el('summary', '', '高级：完整 output_schema 与写回动作'));
    advanced.append(field(
      'output_schema（JSON）', fields.output_schema,
      '上面的字数/标签上限是这个对象里的摘要；手改后以这里为准',
    ));
    fields.apply_actions = textarea(
      JSON.stringify(profile.apply_actions || [], null, 2), 8,
    );
    advanced.append(field(
      'apply_actions（JSON）', fields.apply_actions,
      '模型答完后按顺序执行的动作：write_card_note / apply_tags / write_card_fields / '
      + 'write_worldbook / queue_suggestions / move_cards',
    ));

    const selectorBox = buildSelectorSection(profile);
    const poolBox = buildTagPoolSection(profile);

    const actions = el('div', 'plugin-stm-ai-assistant-row');
    const saveBtn = button('保存', 'is-primary', () => saveProfile(fields, selectorBox, poolBox));
    const validateBtn = button('校验变量', 'is-secondary', () => validateTemplates(fields));
    const previewBtn = button('用当前档案预览', 'is-secondary',
      () => previewWith(fields, selectorBox, poolBox));
    actions.append(saveBtn, validateBtn, previewBtn);

    if (profile.updated_at) {
      actions.append(el('span', 'plugin-stm-ai-assistant-hint', `更新于 ${formatTime(profile.updated_at)}`));
    }

    editorBox.append(
      basicGrid, promptGrid, toggles, formatGrid, rulesGrid, stopGrid,
      advanced, selectorBox, poolBox, actions,
    );
  }

  // -------------------------------------------------------------- 内容选择器

  const SELECTABLE_FIELDS = [
    ['description', '简介'],
    ['personality', '性格'],
    ['scenario', '场景'],
    ['first_mes', '开场白'],
    ['mes_example', '对话示例'],
    ['creator', '作者'],
    ['creator_notes', '作者备注'],
    ['system_prompt', '系统提示词'],
    ['post_history_instructions', '历史后置指令'],
    ['tags', '现有标签'],
  ];

  const EXTRA_OPTIONS = [
    ['regex', '正则脚本（摘要）'],
    ['st_scripts', '酒馆助手脚本（摘要）'],
    ['local_note', '本地备注（主程序的「概览」）'],
  ];

  function selectorOf(profile) {
    const raw = profile && profile.content_selector;
    const spec = raw && typeof raw === 'object' ? raw : {};
    return {
      card_fields: Array.isArray(spec.card_fields)
        ? spec.card_fields.slice()
        : SELECTABLE_FIELDS.map(([name]) => name),
      alternate_greetings: spec.alternate_greetings || { mode: 'none', indexes: [] },
      extras: Array.isArray(spec.extras) ? spec.extras.slice() : [],
      worldbook: spec.worldbook || { mode: 'auto', entry_uids: [], budget_tokens: 0 },
    };
  }

  function buildSelectorSection(profile) {
    const spec = selectorOf(profile);
    const box = el('div', 'plugin-stm-ai-assistant-selector');

    box.append(el('h4', '', '内容选择器'));
    box.append(el('p', 'plugin-stm-ai-assistant-hint',
      '决定哪些卡片信息真的进入提示词。未勾选的字段会渲染为空——'
      + '如果模板引用了它，预览里会给出警告。勾了但模板没引用的也会提示，避免白花 token。'));

    // ---- 正文字段 ----
    const fieldRow = el('div', 'plugin-stm-ai-assistant-checks');
    const fieldBoxes = {};
    SELECTABLE_FIELDS.forEach(([name, label]) => {
      const checked = spec.card_fields.includes(name);
      fieldBoxes[name] = checkbox(checked, label);
      fieldRow.append(fieldBoxes[name]);
    });
    box.append(el('div', 'plugin-stm-ai-assistant-label', '正文字段（名称始终发送）'), fieldRow);

    // ---- 备用开场白 ----
    const greetingsRow = el('div', 'plugin-stm-ai-assistant-row');
    const greetingsModes = [
      { value: 'none', label: '不发送' },
      { value: 'all', label: '全部发送' },
      { value: 'selected', label: '仅指定序号' },
    ];
    const greetingsMode = select(greetingsModes, spec.alternate_greetings.mode || 'none');
    const greetingsIndexes = input(
      (spec.alternate_greetings.indexes || []).map((n) => n + 1).join(','),
      'text', '第几条，从 1 开始，逗号分隔',
    );
    greetingsRow.append(greetingsMode, greetingsIndexes);
    box.append(el('div', 'plugin-stm-ai-assistant-label', '备用开场白'), greetingsRow);

    // ---- 附加内容 ----
    const extraRow = el('div', 'plugin-stm-ai-assistant-checks');
    const extraBoxes = {};
    EXTRA_OPTIONS.forEach(([name, label]) => {
      extraBoxes[name] = checkbox(spec.extras.includes(name), label);
      extraRow.append(extraBoxes[name]);
    });
    box.append(el('div', 'plugin-stm-ai-assistant-label', '附加内容（默认关闭，可能很长）'), extraRow);

    // ---- 世界书 ----
    const wiRow = el('div', 'plugin-stm-ai-assistant-row');
    const wiModes = [
      { value: 'auto', label: '按预算自动取样（推荐）' },
      { value: 'selected', label: '仅指定条目（但仍受预算限制）' },
      { value: 'none', label: '不发送' },
    ];
    const wiMode = select(wiModes, spec.worldbook.mode || 'auto');
    const wiBudget = numberInput(spec.worldbook.budget_tokens || 0, { min: 0, step: 100 });
    wiRow.append(wiMode, wiBudget);
    box.append(
      el('div', 'plugin-stm-ai-assistant-label', '世界书'),
      wiRow,
      el('p', 'plugin-stm-ai-assistant-hint',
        '第二个输入框是 token 上限（0 = 用全局预算）。指定条目请到工作台的世界书列表里勾选。'),
    );

    editorControls = editorControls.concat([
      greetingsMode, wiMode, wiBudget,
      ...Object.values(fieldBoxes), ...Object.values(extraBoxes),
    ]);

    // 把引用挂到 box 上，供 collect 读取
    box._selector = {
      fieldBoxes, extraBoxes, greetingsMode, greetingsIndexes, wiMode, wiBudget,
    };
    return box;
  }

  // -------------------------------------------------------------- 标签池

  const TAG_POOL_SOURCES = [
    { value: 'library', label: '整个标签库（推荐）' },
    { value: 'categories', label: '仅指定分类' },
    { value: 'manual', label: '仅手动列表' },
  ];

  function poolOf(profile) {
    const raw = profile && profile.tag_pool;
    const spec = raw && typeof raw === 'object' ? raw : {};
    return {
      source: spec.source || 'library',
      categories_include: spec.categories_include || [],
      categories_exclude: spec.categories_exclude || [],
      tags_include: spec.tags_include || [],
      tags_exclude: spec.tags_exclude || [],
      respect_isolated: spec.respect_isolated !== false,
      budget_tokens: spec.budget_tokens == null ? 1200 : spec.budget_tokens,
      allow_new_tags: spec.allow_new_tags !== false,
      max_new_tags: spec.max_new_tags == null ? 5 : spec.max_new_tags,
    };
  }

  function buildTagPoolSection(profile) {
    const spec = poolOf(profile);
    const box = el('div', 'plugin-stm-ai-assistant-selector');

    box.append(el('h4', '', '标签池'));
    box.append(el('p', 'plugin-stm-ai-assistant-hint',
      '决定给模型看哪些标签、花多少 token。隔离分类会变成硬过滤——'
      + '即使模型输出了这些标签也不会写进卡片。'));

    const row = el('div', 'plugin-stm-ai-assistant-row');
    const source = select(TAG_POOL_SOURCES, spec.source);
    const budget = numberInput(spec.budget_tokens, { min: 0, step: 100 });
    row.append(source, budget);
    box.append(
      el('div', 'plugin-stm-ai-assistant-label', '来源与预算（token）'),
      row,
      el('p', 'plugin-stm-ai-assistant-hint',
        '预算按 token 而非条数控制；0 表示完全不注入标签库。'),
    );

    const grid = el('div', 'plugin-stm-ai-assistant-grid');
    const categoriesInclude = input(spec.categories_include.join(','), 'text',
      '逗号分隔；留空表示不限');
    const categoriesExclude = input(spec.categories_exclude.join(','), 'text',
      '逗号分隔；隔离分类会自动加入');
    const tagsInclude = input(spec.tags_include.join(','), 'text', '逗号分隔，可追加库外标签');
    const tagsExclude = input(spec.tags_exclude.join(','), 'text', '逗号分隔');
    grid.append(
      field('只取这些分类', categoriesInclude),
      field('排除这些分类', categoriesExclude),
      field('手动追加标签', tagsInclude),
      field('手动排除标签', tagsExclude),
    );
    box.append(grid);

    const toggles = el('div', 'plugin-stm-ai-assistant-row');
    const respectIsolated = checkbox(spec.respect_isolated, '遵守主程序的隔离分类（硬过滤）');
    const allowNew = checkbox(spec.allow_new_tags, '允许模型提议新标签');
    const maxNew = numberInput(spec.max_new_tags, { min: 0, max: 20, step: 1 });
    toggles.append(respectIsolated, allowNew, maxNew);
    box.append(toggles);

    editorControls = editorControls.concat([source, budget, maxNew, respectIsolated, allowNew]);

    box._pool = {
      source, budget, categoriesInclude, categoriesExclude, tagsInclude, tagsExclude,
      respectIsolated, allowNew, maxNew,
    };
    return box;
  }

  function collectTagPool(box) {
    if (!box || !box._pool) return null;
    const refs = box._pool;
    const splitList = (value) => String(value || '')
      .split(/[,，\s]+/).map((item) => item.trim()).filter(Boolean);

    return {
      source: refs.source.value,
      categories_include: splitList(refs.categoriesInclude.value),
      categories_exclude: splitList(refs.categoriesExclude.value),
      tags_include: splitList(refs.tagsInclude.value),
      tags_exclude: splitList(refs.tagsExclude.value),
      respect_isolated: refs.respectIsolated.querySelector('input').checked,
      budget_tokens: Number(refs.budget.querySelector('input').value) || 0,
      allow_new_tags: refs.allowNew.querySelector('input').checked,
      max_new_tags: Number(refs.maxNew.querySelector('input').value) || 0,
    };
  }

  function collectSelector(box) {    if (!box || !box._selector) return null;
    const refs = box._selector;

    const cardFields = SELECTABLE_FIELDS
      .filter(([name]) => refs.fieldBoxes[name].querySelector('input').checked)
      .map(([name]) => name);

    const extras = EXTRA_OPTIONS
      .filter(([name]) => refs.extraBoxes[name].querySelector('input').checked)
      .map(([name]) => name);

    const indexes = String(refs.greetingsIndexes.value || '')
      .split(/[,，\s]+/)
      .map((item) => Number(item) - 1)
      .filter((n) => Number.isInteger(n) && n >= 0);

    return {
      card_fields: cardFields,
      alternate_greetings: {
        mode: refs.greetingsMode.value,
        indexes: refs.greetingsMode.value === 'selected' ? indexes : [],
      },
      extras,
      worldbook: {
        mode: refs.wiMode.value,
        // entry_uids 由工作台的世界书勾选同步，这里只保留已有值
        entry_uids: ((state.current && state.current.content_selector || {})
          .worldbook || {}).entry_uids || [],
        budget_tokens: Number(refs.wiBudget.querySelector('input').value) || 0,
      },
    };
  }

  /** 取档案的 output_schema（缺省时给一份可用的默认，与后端 DEFAULT_OUTPUT_SCHEMA 对齐）。 */
  function outputSchemaOf(profile) {
    const raw = profile && profile.output_schema;
    if (raw && typeof raw === 'object' && Object.keys(raw).length) {
      return JSON.parse(JSON.stringify(raw));
    }
    return {
      strip_think: true,
      json_only: true,
      forbidden_wrappers: ['```json', '```'],
      required: ['summary', 'tags'],
      properties: {
        summary: { type: 'string', min_chars: 0, max_chars: 300 },
        tags: {
          type: 'array', min_items: 0, max_items: 8, min_confidence: 0.5,
          order_by: 'confidence',
          item: {
            name: { type: 'string' },
            confidence: { type: 'number', min: 0, max: 1 },
          },
        },
        suggested_new_tags: {
          type: 'array', max_items: 6,
          item: { name: { type: 'string' }, reason: { type: 'string', max_chars: 200 } },
        },
      },
    };
  }

  function collect(fields, selectorBox, poolBox) {
    const profile = Object.assign({}, state.current);

    profile.id = fields.id.value.trim().toLowerCase();
    profile.name = fields.name.value.trim();
    profile.model_match = fields.model_match.value.trim() || '*';
    profile.priority = Number(fields.priority.querySelector('input').value) || 0;
    profile.missing_policy = fields.missing_policy.value;

    const selector = collectSelector(selectorBox);
    if (selector) profile.content_selector = selector;

    const tagPool = collectTagPool(poolBox);
    if (tagPool) profile.tag_pool = tagPool;

    profile.system_prelude = fields.system_prelude.value;
    profile.system = fields.system.value;
    profile.system_suffix = fields.system_suffix.value;
    profile.user_prelude = fields.user_prelude.value;
    profile.user_template = fields.user_template.value;
    profile.user_suffix = fields.user_suffix.value;
    profile.assistant_prefill = fields.assistant_prefill.value;

    // 输出 schema：以高级区里的完整 JSON 为基，再用常用控件覆盖其摘要项
    let schema;
    try {
      schema = JSON.parse(fields.output_schema.value || '{}');
    } catch (error) {
      throw new Error(`output_schema 不是合法 JSON：${error.message}`);
    }

    schema.json_only = fields.json_only.querySelector('input').checked;
    schema.strip_think = fields.strip_think.querySelector('input').checked;
    schema.forbidden_wrappers = fields.wrappers.value
      .split(',').map((item) => item.trim()).filter(Boolean);
    schema.required = fields.required_fields.value
      .split(',').map((item) => item.trim()).filter(Boolean);
    schema.properties = schema.properties || {};

    schema.properties.summary = Object.assign(
      { type: 'string' },
      schema.properties.summary || {},
      {
        min_chars: Number(fields.summary_min.querySelector('input').value) || 0,
        max_chars: Number(fields.summary_max.querySelector('input').value) || 0,
      },
    );

    schema.properties.tags = Object.assign(
      { type: 'array', item: { name: { type: 'string' }, confidence: { type: 'number' } } },
      schema.properties.tags || {},
      {
        min_items: Number(fields.tags_min.querySelector('input').value) || 0,
        max_items: Number(fields.tags_max.querySelector('input').value) || 0,
        min_confidence: Number(fields.confidence.querySelector('input').value) || 0,
      },
    );

    profile.output_schema = schema;

    try {
      profile.apply_actions = JSON.parse(fields.apply_actions.value || '[]');
    } catch (error) {
      throw new Error(`apply_actions 不是合法 JSON：${error.message}`);
    }

    profile.temperature = Number(fields.temperature.querySelector('input').value);
    profile.top_p = Number(fields.top_p.querySelector('input').value);
    profile.max_tokens = Number(fields.max_tokens.querySelector('input').value) || 2048;
    profile.stop = fields.stop.value.split('\n').map((item) => item.trim()).filter(Boolean);

    try {
      profile.extra_body = JSON.parse(fields.extra_body.value || '{}');
    } catch (error) {
      throw new Error(`extra_body 不是合法 JSON：${error.message}`);
    }

    return profile;
  }

  async function validateTemplates(fields) {
    try {
      const profile = collect(fields, selectorBox, poolBox);
      const data = await client.validateTemplates([
        profile.system_prelude, profile.system, profile.system_suffix,
        profile.user_prelude, profile.user_template, profile.user_suffix,
      ]);
      state.variables = data.known || [];
      renderVariableHint();

      if ((data.unknown || []).length) {
        api.host.toast(`存在未知变量：${data.unknown.join('、')}`, 4200, 'close');
      } else {
        api.host.toast('模板变量校验通过', 2400, 'check');
      }
    } catch (error) {
      api.host.toast(error.message, 3600, 'close');
    }
  }

  function renderVariableHint() {
    clear(variableHint);
    if (!state.variables.length) {
      variableHint.textContent = '提示：点击「校验变量」可检查模板里的未知变量。';
      return;
    }
    variableHint.textContent = `可用变量：${state.variables.join('、')}`;
  }

  async function previewWith(fields, selectorBox, poolBox) {
    if (!context.selectedCardIds || !context.selectedCardIds.length) {
      api.host.toast('请先在工作台选择一张卡片', 3000, 'close');
      return;
    }
    try {
      const profile = collect(fields, selectorBox, poolBox);
      const data = await client.preview({
        card_id: context.selectedCardIds[0],
        profile_id: profile.id,
      });
      api.host.toast(`预览：${data.tokens.total} token（${data.profile.id}）`, 3600, 'settings-help-entry');
      // 全文交给宿主的 modal surface（见 panel.js 的 createPromptPreview）
      if (typeof context.openPromptPreview === 'function') {
        context.openPromptPreview(data.messages || []);
      }
    } catch (error) {
      api.host.toast(`预览失败：${error.message}`, 3600, 'close');
    }
  }

  async function saveProfile(fields, selectorBox, poolBox) {
    let profile;
    try {
      profile = collect(fields, selectorBox, poolBox);
    } catch (error) {
      api.host.toast(error.message, 4000, 'close');
      return;
    }

    try {
      const data = await client.saveProfile(profile);
      state.current = data.profile;
      state.isNew = false;
      api.host.toast('已保存', 2400, 'check');
      await load();
      renderEditor();
    } catch (error) {
      const detail = (error.detail && error.detail.errors) || [];
      api.host.toast(`保存失败：${error.message}${detail.length ? `（${detail.join('；')}）` : ''}`, 6000, 'close');
    }
  }

  async function duplicateProfile(profileId) {
    try {
      const data = await client.duplicateProfile(profileId);
      api.host.toast(`已复制为 ${data.profile.id}`, 3000, 'check');
      await load();
      await openProfile(data.profile.id);
    } catch (error) {
      api.host.toast(`复制失败：${error.message}`, 3600, 'close');
    }
  }

  async function deleteProfile(item) {
    const confirmed = await api.ui.confirm({
      title: item.builtin ? `「${item.name}」是内置档案` : `删除档案「${item.name}」？`,
      message: item.builtin
        ? '删除将恢复出厂默认，被修改的内容会先自动备份。'
        : '此操作不可撤销，导出备份后可以再导入。',
      danger: true,
    });
    if (!confirmed) return;

    try {
      const data = await client.deleteProfile(item.id);
      api.host.toast(data.message || '已删除', 2400, 'check');
      if (state.current && state.current.id === item.id) state.current = null;
      if (state.activeId === item.id) state.activeId = '';
      await load();
      renderEditor();
    } catch (error) {
      api.host.toast(`删除失败：${error.message}`, 3600, 'close');
    }
  }

  /**
   * 导出档案。
   * @param {string} [profileId] 省略时导出全部；给出时只导出该档案。
   */
  async function exportProfiles(profileId) {
    const target = String(profileId || '').trim();
    try {
      const data = await client.exportProfiles(target);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = target
        ? `stm-ai-assistant-profile-${target}.json`
        : 'stm-ai-assistant-profiles.json';
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      const count = (data.profiles || []).length;
      api.host.toast(target ? `已导出档案 ${target}` : `已导出全部 ${count} 个档案`, 2600, 'check');
    } catch (error) {
      api.host.toast(`导出失败：${error.message}`, 3600, 'close');
    }
  }

  async function importProfile() {
    const result = await api.ui.prompt({
      title: '导入档案',
      fields: [{
        key: 'payload',
        label: '档案 JSON（单个对象或数组，或 {"profiles": [...]}）',
        type: 'string',
      }],
    });
    const raw = result && result.payload ? String(result.payload).trim() : '';
    if (!raw) return;

    try {
      const payload = JSON.parse(raw);
      const data = await client.importProfiles(payload);
      api.host.toast(`导入 ${data.imported.length} 个档案`
        + (data.failed.length ? `，失败 ${data.failed.length} 个` : ''), 4000, 'check');
      await load();
    } catch (error) {
      api.host.toast(`导入失败：${error.message}`, 4000, 'close');
    }
  }

  async function resetProfiles() {
    const confirmed = await api.ui.confirm({
      title: '恢复出厂档案？',
      message: '将删除全部自定义档案，并把被修改的内置档案恢复为出厂默认（会自动备份）。',
      danger: true,
    });
    if (!confirmed) return;
    try {
      const data = await client.resetProfiles();
      api.host.toast(`已重置（移除 ${data.removed.length} 个档案）`, 3200, 'check');
      state.current = null;
      await load();
      renderEditor();
    } catch (error) {
      api.host.toast(`重置失败：${error.message}`, 3600, 'close');
    }
  }

  async function load() {
    try {
      const data = await client.profiles();
      state.list = data.items || [];
      state.activeId = String(data.active || '');
      if (!context.profileId) context.profileId = state.activeId;
    } catch (error) {
      api.host.toast(`读取档案失败：${error.message}`, 3600, 'close');
      state.list = [];
    }
    renderList();
  }

  return {
    element: root,
    refresh: async () => {
      await load();
      if (state.current && state.current.id
        && !state.list.some((entry) => (entry.index || entry).id === state.current.id)) {
        // 当前编辑的档案已被删除 / 重置
        state.current = null;
      }
      if (!state.current && state.list.length) {
        await openProfile((state.list[0].index || state.list[0]).id);
      } else {
        renderList();
        renderEditor();
      }
    },
    destroy: () => {
      destroyEditorControls();
    },
  };
}
