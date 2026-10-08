/**
 * 提示词档案编辑器：按模型分档的 system / user 模板、破限段、格式与字数规则。
 *
 * 保存前会调用 `/prompt/validate` 做未知变量检查（模板里出现未知变量必须在保存时被拒绝，
 * 见设计文档 §5.4）。
 */

import {
  badge,
  button,
  checkbox,
  clear,
  el,
  field,
  formatTime,
  input,
  section,
  select,
  textarea,
} from '../dom.js';

const MISSING_POLICIES = [
  { value: 'error', label: '缺失必需变量时报错' },
  { value: 'empty', label: '缺失一律渲染为空' },
];

export function createProfiles(api, client, context) {
  const root = el('div', 'plugin-stm-ai-assistant-view');
  const state = { list: [], current: null, isNew: false, variables: [] };

  const listSection = section('提示词档案', '按模型匹配（通配或前缀），优先级越高越优先。');
  const listBox = el('div', 'plugin-stm-ai-assistant-profile-list');
  const listActions = el('div', 'plugin-stm-ai-assistant-row');
  const editorBox = el('div', 'plugin-stm-ai-assistant-profile-editor');
  const variableHint = el('p', 'plugin-stm-ai-assistant-hint');

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
      const active = state.current && state.current.id === item.id;
      if (active) row.classList.add('is-active');

      row.append(
        badge(item.id, item.builtin ? 'is-muted' : 'is-ok'),
        el('span', 'plugin-stm-ai-assistant-profile-name', item.name),
        el('span', 'plugin-stm-ai-assistant-hint',
          `${item.model_match} · 优先级 ${item.priority} · v${item.version}`),
      );

      const actions = el('div', 'plugin-stm-ai-assistant-row');
      actions.append(
        button('编辑', 'is-secondary', () => openProfile(item.id)),
        button('复制', 'is-secondary', () => duplicateProfile(item.id)),
        button('删除', 'is-danger', () => deleteProfile(item)),
      );
      row.append(actions);
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
    renderEditor();
  }

  async function openProfile(profileId) {
    try {
      const data = await client.profile(profileId);
      state.current = data.profile;
      state.isNew = false;
      renderEditor();
    } catch (error) {
      api.toast(`读取档案失败：${error.message}`, 3600, 'close');
    }
  }

  function renderEditor() {
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
    fields.priority = input(profile.priority, 'number');
    fields.missing_policy = select(MISSING_POLICIES, profile.missing_policy);

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
    fields.summary_min = input(lengthRules.summary_min_chars, 'number');
    fields.summary_max = input(lengthRules.summary_max_chars, 'number');
    fields.tags_min = input(lengthRules.tags_min, 'number');
    fields.tags_max = input(lengthRules.tags_max, 'number');
    fields.confidence = input(lengthRules.confidence_min, 'number');
    fields.temperature = input(profile.temperature, 'number');
    fields.top_p = input(profile.top_p, 'number');
    fields.max_tokens = input(profile.max_tokens, 'number');
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
    profile.priority = Number(fields.priority.value) || 0;
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
      summary_min_chars: Number(fields.summary_min.value) || 0,
      summary_max_chars: Number(fields.summary_max.value) || 300,
      tags_min: Number(fields.tags_min.value) || 0,
      tags_max: Number(fields.tags_max.value) || 8,
      confidence_min: Number(fields.confidence.value) || 0,
    };

    profile.temperature = Number(fields.temperature.value);
    profile.top_p = Number(fields.top_p.value);
    profile.max_tokens = Number(fields.max_tokens.value) || 2048;
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
        api.toast(`存在未知变量：${data.unknown.join('、')}`, 4200, 'close');
      } else {
        api.toast('模板变量校验通过', 2400, 'check');
      }
    } catch (error) {
      api.toast(error.message, 3600, 'close');
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
      api.toast('请先在工作台选择一张卡片', 3000, 'close');
      return;
    }
    try {
      const profile = collect(fields);
      const data = await client.preview({
        card_id: context.selectedCardIds[0],
        profile_id: profile.id,
      });
      api.toast(`预览：${data.tokens.total} token（${data.profile.id}）`, 3600, 'settings-help-entry');
    } catch (error) {
      api.toast(`预览失败：${error.message}`, 3600, 'close');
    }
  }

  async function saveProfile(fields) {
    let profile;
    try {
      profile = collect(fields);
    } catch (error) {
      api.toast(error.message, 4000, 'close');
      return;
    }

    try {
      const data = await client.saveProfile(profile);
      state.current = data.profile;
      state.isNew = false;
      api.toast('已保存', 2400, 'check');
      await load();
      renderEditor();
    } catch (error) {
      const detail = (error.detail && error.detail.errors) || [];
      api.toast(`保存失败：${error.message}${detail.length ? `（${detail.join('；')}）` : ''}`, 6000, 'close');
    }
  }

  async function duplicateProfile(profileId) {
    try {
      const data = await client.duplicateProfile(profileId);
      api.toast(`已复制为 ${data.profile.id}`, 3000, 'check');
      await load();
      await openProfile(data.profile.id);
    } catch (error) {
      api.toast(`复制失败：${error.message}`, 3600, 'close');
    }
  }

  async function deleteProfile(item) {
    const message = item.builtin
      ? `「${item.name}」是内置档案，删除将恢复出厂默认，继续？`
      : `删除档案「${item.name}」？`;
    if (!window.confirm(message)) return;

    try {
      const data = await client.deleteProfile(item.id);
      api.toast(data.message || '已删除', 2400, 'check');
      if (state.current && state.current.id === item.id) state.current = null;
      await load();
      renderEditor();
    } catch (error) {
      api.toast(`删除失败：${error.message}`, 3600, 'close');
    }
  }

  async function exportProfiles() {
    try {
      const data = await client.exportProfiles();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'stm-ai-assistant-profiles.json';
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      api.toast(`导出失败：${error.message}`, 3600, 'close');
    }
  }

  async function importProfile() {
    const raw = window.prompt('粘贴档案 JSON（单个对象或数组，或 {"profiles": [...]}）');
    if (!raw) return;
    try {
      const payload = JSON.parse(raw);
      const data = await client.importProfiles(payload);
      api.toast(`导入 ${data.imported.length} 个档案`
        + (data.failed.length ? `，失败 ${data.failed.length} 个` : ''), 4000, 'check');
      await load();
    } catch (error) {
      api.toast(`导入失败：${error.message}`, 4000, 'close');
    }
  }

  async function resetProfiles() {
    if (!window.confirm('将删除全部自定义档案，并把被修改的内置档案恢复为出厂默认（会自动备份）。继续？')) {
      return;
    }
    try {
      const data = await client.resetProfiles();
      api.toast(`已重置（移除 ${data.removed.length} 个档案）`, 3200, 'check');
      state.current = null;
      await load();
      renderEditor();
    } catch (error) {
      api.toast(`重置失败：${error.message}`, 3600, 'close');
    }
  }

  async function load() {
    try {
      const data = await client.profiles();
      state.list = data.items || [];
    } catch (error) {
      api.toast(`读取档案失败：${error.message}`, 3600, 'close');
      state.list = [];
    }
    renderList();
  }

  return {
    element: root,
    refresh: async () => {
      await load();
      if (!state.current && state.list.length) {
        await openProfile((state.list[0].index || state.list[0]).id);
      } else {
        renderEditor();
      }
    },
    destroy: () => {},
  };
}
