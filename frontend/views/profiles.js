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
      format_rules: {
        json_only: true,
        forbidden_wrappers: ['```json', '```'],
        required_keys: ['summary', 'tags'],
        strip_think: true,
      },
      length_rules: {
        summary_min_chars: 0,
        summary_max_chars: 300,
        tags_min: 0,
        tags_max: 8,
        confidence_min: 0.5,
      },
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
    const formatRules = profile.format_rules || {};
    const lengthRules = profile.length_rules || {};

    fields.json_only = checkbox(formatRules.json_only !== false, '强制 JSON 输出');
    fields.strip_think = checkbox(formatRules.strip_think !== false, '剥离思考段');
    fields.wrappers = input((formatRules.forbidden_wrappers || []).join(','), 'text', '逗号分隔');
    fields.required_keys = input((formatRules.required_keys || []).join(','), 'text', '逗号分隔');
    fields.summary_min = numberInput(lengthRules.summary_min_chars, { min: 0, step: 1 });
    fields.summary_max = numberInput(lengthRules.summary_max_chars, { min: 0, step: 1 });
    fields.tags_min = numberInput(lengthRules.tags_min, { min: 0, step: 1 });
    fields.tags_max = numberInput(lengthRules.tags_max, { min: 0, step: 1 });
    fields.confidence = numberInput(lengthRules.confidence_min, { min: 0, max: 1, step: 0.05 });
    fields.temperature = numberInput(profile.temperature, { min: 0, max: 2, step: 0.05 });
    fields.top_p = numberInput(profile.top_p, { min: 0, max: 1, step: 0.05 });
    fields.max_tokens = numberInput(profile.max_tokens, { min: 1, step: 1 });
    fields.stop = textarea((profile.stop || []).join('\n'), 3);
    fields.extra_body = textarea(JSON.stringify(profile.extra_body || {}, null, 2), 4);

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
      field('必需字段', fields.required_keys, '逗号分隔'),
    );

    const stopGrid = el('div', 'plugin-stm-ai-assistant-grid');
    stopGrid.append(
      field('stop 序列（每行一个）', fields.stop),
      field('extra_body（JSON）', fields.extra_body, '合并进请求体，用于供应商特有参数'),
    );

    const actions = el('div', 'plugin-stm-ai-assistant-row');
    const saveBtn = button('保存', 'is-primary', () => saveProfile(fields));
    const validateBtn = button('校验变量', 'is-secondary', () => validateTemplates(fields));
    const previewBtn = button('用当前档案预览', 'is-secondary', () => previewWith(fields));
    actions.append(saveBtn, validateBtn, previewBtn);

    if (profile.updated_at) {
      actions.append(el('span', 'plugin-stm-ai-assistant-hint', `更新于 ${formatTime(profile.updated_at)}`));
    }

    editorBox.append(basicGrid, promptGrid, toggles, formatGrid, rulesGrid, stopGrid, actions);
  }

  function collect(fields) {
    const profile = Object.assign({}, state.current);

    profile.id = fields.id.value.trim().toLowerCase();
    profile.name = fields.name.value.trim();
    profile.model_match = fields.model_match.value.trim() || '*';
    profile.priority = Number(fields.priority.querySelector('input').value) || 0;
    profile.missing_policy = fields.missing_policy.value;

    profile.system_prelude = fields.system_prelude.value;
    profile.system = fields.system.value;
    profile.system_suffix = fields.system_suffix.value;
    profile.user_prelude = fields.user_prelude.value;
    profile.user_template = fields.user_template.value;
    profile.user_suffix = fields.user_suffix.value;
    profile.assistant_prefill = fields.assistant_prefill.value;

    profile.format_rules = {
      json_only: fields.json_only.querySelector('input').checked,
      strip_think: fields.strip_think.querySelector('input').checked,
      forbidden_wrappers: fields.wrappers.value.split(',').map((item) => item.trim()).filter(Boolean),
      required_keys: fields.required_keys.value.split(',').map((item) => item.trim()).filter(Boolean),
    };

    profile.length_rules = {
      summary_min_chars: Number(fields.summary_min.querySelector('input').value) || 0,
      summary_max_chars: Number(fields.summary_max.querySelector('input').value) || 300,
      tags_min: Number(fields.tags_min.querySelector('input').value) || 0,
      tags_max: Number(fields.tags_max.querySelector('input').value) || 8,
      confidence_min: Number(fields.confidence.querySelector('input').value) || 0,
    };

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
      const profile = collect(fields);
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

  async function previewWith(fields) {
    if (!context.selectedCardIds || !context.selectedCardIds.length) {
      api.host.toast('请先在工作台选择一张卡片', 3000, 'close');
      return;
    }
    try {
      const profile = collect(fields);
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

  async function saveProfile(fields) {
    let profile;
    try {
      profile = collect(fields);
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
