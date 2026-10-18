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
  const state = { providers: [], usage: null, status: null, suggestions: [], editing: null };

  const overviewSection = section('概览');
  const overviewBox = el('div', 'plugin-stm-ai-assistant-row');

  const providerSection = section('Provider', '每个供应商可配置独立的地址、模型与密钥；密钥存在主程序的 secrets.json（不回显）。');
  const providerList = el('div', 'plugin-stm-ai-assistant-provider-list');
  const providerEditor = el('div', 'plugin-stm-ai-assistant-provider-editor');
  let editorControls = [];

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

  root.append(overviewSection, providerSection, usageSection, suggestionSection);
  overviewSection.append(overviewBox);

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

    // 模型列表：拉取失败必须能退回手填（很多网关不实现 /models）
    const modelRow = el('div', 'plugin-stm-ai-assistant-row');
    const modelSelect = el('select', 'plugin-stm-ai-assistant-select');
    modelSelect.append(el('option', '', '（尚未拉取）'));
    modelSelect.value = '';
    modelSelect.addEventListener('change', () => {
      if (modelSelect.value) fields.model.value = modelSelect.value;
    });
    modelRow.append(
      modelSelect,
      button('拉取模型列表', 'is-secondary', () => fetchModels(fields, modelSelect)),
    );

    grid.append(
      field('id', fields.id),
      field('名称', fields.name),
      field('API 地址', fields.base_url),
      field('默认模型', fields.model),
      field('模型候选', modelRow, '点「拉取模型列表」从 /models 读取；失败时直接手填上方'),
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
    const id = fields.id.value.trim().toLowerCase();
    if (!id) {
      api.host.toast('先填 provider id（保存前也可以拉取）', 3200, 'close');
      return;
    }

    try {
      const data = await client.listModels(id, {
        base_url: fields.base_url.value.trim(),
        api_style: fields.api_style.value,
      });

      clear(modelSelect);
      if (!data.ok) {
        modelSelect.append(el('option', '', '（拉取失败：请手填模型名）'));
        modelSelect.value = '';
        api.host.toast(`拉取模型失败：${data.message || '未知原因'}`, 5200, 'close');
        return;
      }

      modelSelect.append(el('option', '', `（${data.models.length} 个模型）`));
      data.models.forEach((name) => {
        const option = el('option', '', name);
        option.value = name;
        modelSelect.append(option);
      });
      modelSelect.value = '';
      api.host.toast(`已拉取 ${data.models.length} 个模型`, 2600, 'check');
    } catch (error) {
      api.host.toast(`拉取模型失败：${error.message}`, 4200, 'close');
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
  }

  async function load() {
    try {
      const [status, providers, usage, suggestions] = await Promise.all([
        client.status(), client.providers(), client.usage(), client.suggestions(),
      ]);
      state.status = status;
      state.providers = providers.items || [];
      state.usage = usage;
      state.suggestions = suggestions.items || [];
    } catch (error) {
      api.host.toast(`读取设置失败：${error.message}`, 3600, 'close');
    }

    renderOverview();
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
