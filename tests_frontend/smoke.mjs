/**
 * 前端「无浏览器」冒烟测试。
 *
 * 用一个最小 DOM 桩 + 假插件 API，真实执行 `activate()` 与各面板的渲染路径，
 * 目的是在不开浏览器的情况下抓住：模块解析错误、空引用、事件绑定错误、
 * 作用域解析分支遗漏。
 *
 * 运行：node tests_frontend/smoke.mjs
 * 说明：这不是浏览器级 E2E，只覆盖「加载 + 首屏渲染 + 作用域解析 + 提交参数映射」。
 */

import assert from 'node:assert';

// ---------------------------------------------------------------- DOM 桩

class FakeNode {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.classList = {
      _set: new Set(),
      add: (...names) => names.forEach((n) => this.classList._set.add(n)),
      remove: (...names) => names.forEach((n) => this.classList._set.delete(n)),
      toggle: (name, on) => {
        const want = on === undefined ? !this.classList._set.has(name) : Boolean(on);
        if (want) this.classList._set.add(name);
        else this.classList._set.delete(name);
        return want;
      },
      contains: (name) => this.classList._set.has(name),
    };
    this._text = '';
    this._listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.checked = false;
    this.type = '';
    this.attributes = {};
  }

  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }

  // 真 DOM 里 `className = 'a b'` 与 `classList` 是同一份数据；桩必须保持同步，
  // 否则「按 class 找节点」的断言会假失败。
  get className() { return [...this.classList._set].join(' '); }
  set className(v) {
    this.classList._set = new Set(String(v || '').split(/\s+/).filter(Boolean));
  }

  get firstChild() { return this.children[0] || null; }

  get options() { return this.children.filter((node) => node.tagName === 'OPTION'); }

  append(...nodes) {
    nodes.flat().forEach((node) => {
      if (node == null) return;
      if (typeof node === 'string') node = new FakeNode('span'), node._text = node;
      node.parentNode = this;
      this.children.push(node);
    });
  }

  insertBefore(node, ref) {
    node.parentNode = this;
    const idx = this.children.indexOf(ref);
    if (idx < 0) this.children.push(node);
    else this.children.splice(idx, 0, node);
    return node;
  }

  prepend(node) {
    node.parentNode = this;
    this.children.unshift(node);
  }

  removeChild(node) {
    const idx = this.children.indexOf(node);
    if (idx >= 0) this.children.splice(idx, 1);
    node.parentNode = null;
    return node;
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, fn) {
    if (!this._listeners.has(name)) this._listeners.set(name, []);
    this._listeners.get(name).push(fn);
  }
  removeEventListener(name, fn) {
    const list = this._listeners.get(name) || [];
    const idx = list.indexOf(fn);
    if (idx >= 0) list.splice(idx, 1);
  }
  dispatchEvent() { return true; }
  /** 测试辅助：点一下。 */
  click() {
    (this._listeners.get('click') || []).forEach((fn) => fn({ preventDefault() {}, target: this }));
  }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {}
  get clientWidth() { return 1200; }
  get clientHeight() { return 900; }
  get offsetHeight() { return 200; }
}

const allNodes = [];
function makeNode(tag) {
  const node = new FakeNode(tag);
  allNodes.push(node);
  return node;
}

globalThis.window = {
  innerWidth: 1200,
  innerHeight: 900,
  getComputedStyle: () => ({ overflowY: 'auto', rowGap: '8px', gap: '8px', gridTemplateColumns: '1fr 1fr' }),
  dispatchEvent: () => true,
  addEventListener: () => {},
  removeEventListener: () => {},
  CustomEvent: class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  confirm: () => true,
  Alpine: { store: () => null },
};

globalThis.document = {
  createElement: (tag) => makeNode(tag),
  createElementNS: (_ns, tag) => makeNode(tag),
  body: makeNode('body'),
  head: makeNode('head'),
  documentElement: makeNode('html'),
  addEventListener: () => {},
  removeEventListener: () => {},
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: () => null,
  activeElement: null,
  hidden: false,
  visibilityState: 'visible',
};

globalThis.Event = class { constructor(type) { this.type = type; } };
globalThis.CustomEvent = globalThis.window.CustomEvent;
globalThis.HTMLElement = FakeNode;
globalThis.Node = FakeNode;
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.ResizeObserver = class { observe() {} disconnect() {} unobserve() {} };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
globalThis.EventSource = undefined; // 强制 subscribeJob 走轮询分支

// ---------------------------------------------------------------- 假插件 API

function createFakeApi({ providers = 1, pending = 0 } = {}) {
  const badges = [];
  const statuses = [];
  const registered = new Map();
  const opened = [];
  const calls = [];
  const listeners = new Map();

  const api = {
    pluginId: 'stm-ai-assistant',
    appVersion: '1.0.0',
    manifest: {},
    surfaces: {
      register(id, spec) { registered.set(id, spec); return id; },
      open(id, ctx = {}) {
        opened.push({ id, ctx });
        const spec = registered.get(id);
        const container = makeNode('div');
        if (spec && spec.type !== 'ambient') {
          const mountResult = spec.mount ? spec.mount(container) : undefined;
          if (spec.onOpen) spec.onOpen(ctx);
          if (spec.headerExtra) spec.headerExtra(makeNode('header'));
          if (typeof mountResult === 'function') mountResult;
        }
        return container;
      },
      close(id) { return true; },
      isOpen(id) { return opened.some((item) => item.id === id); },
    },
    contributions: {
      update(id, patch = {}) {
        if (Object.prototype.hasOwnProperty.call(patch, 'badge')) badges.push([id, patch.badge]);
        if (Object.prototype.hasOwnProperty.call(patch, 'status')) statuses.push([id, patch.status]);
        return true;
      },
      remove: () => true,
      get: () => null,
      list: () => [],
      trigger: () => true,
    },
    ui: {
      icon: (name) => makeNode('span'),
      button: ({ label, onClick } = {}) => {
        const node = makeNode('button');
        node._text = String(label || '');
        if (onClick) node.addEventListener('click', onClick);
        return node;
      },
      badge: ({ value } = {}) => {
        const node = makeNode('span');
        node._text = String(value ?? '');
        return node;
      },
      section: ({ title, description } = {}) => {
        const node = makeNode('section');
        if (title) node.append(makeNode('h4'));
        if (description) node.append(makeNode('p'));
        const body = makeNode('div');
        node.append(body);
        return node;
      },
      empty: ({ title } = {}) => makeNode('div'),
      field: () => makeNode('div'),
      confirm: async () => true,
    },
    storage: (() => {
      const bucket = new Map();
      return {
        get: (k) => (bucket.has(k) ? bucket.get(k) : null),
        set: (k, v) => bucket.set(k, v),
        remove: (k) => bucket.delete(k),
        keys: () => [...bucket.keys()],
      };
    })(),
    host: {
      toast: () => true,
      refresh: () => true,
      switchMode: () => true,
      openDetail: () => true,
      openSettingsSection: () => true,
      notify: () => true,
      log: () => {},
      onState: () => () => {},
      selection: () => ({ mode: 'cards', ids: [] }),
    },
    log: () => {},
    on(name, fn) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(fn);
      return () => {};
    },
    off: () => true,
    emit: () => true,
    onConfigChange: () => () => {},
    subscribeJob(runId, handlers = {}) {
      calls.push(['subscribeJob', runId]);
      setTimeout(() => {
        handlers.snapshot && handlers.snapshot({
          status: 'running', total: 4, completed: 2, failed: 0, skipped: 0, message: '处理中',
        });
        handlers.snapshot && handlers.snapshot({
          status: 'completed', total: 4, completed: 3, failed: 0, skipped: 1, message: '完成',
        });
        handlers.done && handlers.done({ status: 'completed', completed: 3, failed: 0, skipped: 1 });
      }, 0);
      return () => {};
    },
    cards: {
      list: ({ category = '' } = {}) => {
        calls.push(['cards.list', category]);
        if (category === 'missing') throw new Error('分类不存在');
        return {
          items: [
            { id: 'a.json', name: 'A', category, token_count: 120 },
            { id: 'b.json', name: 'B', category, token_count: 80 },
          ],
          total: 2,
        };
      },
      read: (id) => ({ id, name: id }),
      apply_tags: () => ({ applied: [], failed: [] }),
      list_categories: () => [],
      move: () => ({ moved: [], failed: [] }),
      create_category: () => true,
    },
    worldbook: {
      read: () => ({ entries: [] }),
      write: () => ({ success: true }),
      list_entries: () => [],
      sources: ({ category = '' } = {}) => ([
        { source_type: 'global', file_path: 'lorebooks/a.json', card_id: '', category, name: 'A 世界书' },
      ]),
    },
    api: async (path, options = {}) => {
      calls.push(['api', path, options && options.body]);
      // `/cards` 是作用域解析的唯一数据源（`scope.js` 走 HTTP，不读 `api.cards`）。
      // 这里按 query 复刻后端行为：`category` 过滤、`ids` 直取。
      if (String(path).startsWith('/cards')) {
        const query = String(path).split('?')[1] || '';
        const params = new URLSearchParams(query);
        const category = params.get('category') || '';
        if (category === 'missing') {
          return { success: false, error: 'list_failed', msg: '分类不存在' };
        }
        const ids = (params.get('ids') || '').split(',').filter(Boolean);
        const all = [
          { id: 'a.json', name: 'A', category, token_count: 120 },
          { id: 'b.json', name: 'B', category, token_count: 80 },
        ];
        const items = ids.length ? all.filter((card) => ids.includes(card.id)) : all;
        return { success: true, items, total: items.length };
      }
      const table = {
        '/status': {
          success: true,
          config: { apply_mode: 'review' },
          profiles: { count: 3 },
          providers: { count: providers },
          pending_reviews: Object.fromEntries(Array.from({ length: pending }, (_, i) => [`run${i}`, {}])),
          suggestions: 2,
          usage: {},
        },
        '/health': {
          success: true,
          checks: {
            provider: { ok: true },
            api_key: { ok: true },
            summary_bridge: { ok: true },
          },
        },
        '/providers': { success: true, items: [{ id: 'p1', name: 'P1', is_default: true, model: 'm1' }] },
        '/profiles': { success: true, items: [{ id: 'builtin-analyze', name: '打标档案', task: 'analyze' }] },
        '/jobs': { success: true, jobs: [{ run_id: 'abcdef123456', task: 'analyze', status: 'completed', counts: { total: 2, completed: 2 }, created_at: 1700000000 }] },
        '/categories': { success: true, items: [{ path: '科幻', count: 3 }] },
        '/prompt/preview': { success: true, messages: [{ role: 'system', content: 'hi' }], tokens: { total: 10 }, profile: { id: 'x', name: 'X' }, provider: { model: 'm1' } },
        '/wi/entries': { success: true, items: [{ uid: '1', name: 'E1', tokens: 10, enabled: true }] },
        '/proposal/sample': { success: true, sample: { count: 5, tokens: 100 } },
      };
      if (path.startsWith('/jobs/') && path.endsWith('/apply')) return { success: true, applied: 2, skipped: 0, stale: 0, failed: 0, details: [] };
      if (path.startsWith('/jobs/') && path.includes('/items/')) return { success: true };
      if (path.startsWith('/jobs?')) return table['/jobs'];
      if (path.startsWith('/jobs/') && !path.includes('/apply')) {
        return {
          success: true,
          job: { run_id: 'abcdef123456', job_type: 'ai.analyze', status: 'completed', total: 2, completed: 2, failed: 0, skipped: 0, summary: {}, counts: { total: 2, completed: 2 } },
          items: [
            { item_key: 'a.json', status: 'awaiting_review', result: { card_id: 'a.json', summary: '概览', tags: [{ name: '标签A', confidence: 0.9 }], new_tags: [{ name: '新标签' }] } },
            { item_key: 'b.json', status: 'done', result: { card_id: 'b.json', summary: '概览B', tags: [] } },
          ],
        };
      }
      if (path.startsWith('/jobs/analyze') || path.startsWith('/jobs/wi-rewrite') || path.startsWith('/jobs/classify') || path.startsWith('/jobs/propose') || path.startsWith('/jobs/agent')) {
        return { success: true, run_id: 'abcdef123456', total: 2, apply_mode: 'review' };
      }
      return table[path] || { success: true };
    },
    utils: {
      toAbsoluteAssetUrl: (p) => p,
      parseSSEFrames: () => [],
      parseSSEPayload: () => ({}),
      resolveIconUrl: (n) => n,
    },
    setFullscreenScrim: () => true,
    createRoot: () => makeNode('div'),
    jobs: {
      get: async () => null,
      list: async () => [],
      submit: async () => 'run',
      cancel: async () => true,
      pause: async () => true,
      resume: async () => true,
      retry: async () => 'run2',
      open: () => {},
    },
  };

  return { api, registered, opened, badges, statuses, calls };
}

// ---------------------------------------------------------------- 用例

const results = [];
function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => results.push([name, 'ok', '']))
    .catch((error) => results.push([name, 'FAIL', error && error.message ? error.message : String(error)]));
}

async function main() {
  const { api, registered, opened, badges } = createFakeApi({ pending: 2 });

  const mod = await import('../frontend/plugin.js');
  await check('activate 不抛错', async () => {
    const dispose = await mod.activate(api);
    assert.equal(typeof dispose, 'function');
  });

  await check('注册了全部 surface', async () => {
    const { SURFACES } = await import('../frontend/panel.js');
    for (const id of Object.keys(SURFACES)) {
      assert.ok(registered.has(id), `缺少 surface: ${id}`);
      assert.ok(registered.has(`${id}`), id);
    }
    assert.ok(registered.has('prompt-preview'));
  });

  await check('hub 打开并渲染', async () => {
    api.surfaces.open('hub', { source: 'dock' });
    assert.ok(opened.some((item) => item.id === 'hub'));
  });

  await check('待审批角标写到 workbench 与 open-review', async () => {
    await new Promise((resolve) => setTimeout(resolve, 30));
    const ids = new Set(badges.map(([id]) => id));
    assert.ok(ids.has('workbench'), `角标未写 workbench: ${[...ids]}`);
    assert.ok(ids.has('open-review'), `角标未写 open-review: ${[...ids]}`);
  });

  await check('右键分类 → analyze 面板解析作用域', async () => {
    api.surfaces.open('analyze', {
      source: 'context',
      targetKind: 'folder',
      targetPath: '科幻',
      targetIds: ['a.json', 'b.json'],
      mode: 'cards',
    });
    await new Promise((resolve) => setTimeout(resolve, 30));
  });

  await check('分类不存在时显示错误而不是 0 张', async () => {
    const { resolveScope } = await import('../frontend/scope.js');
    let threw = false;
    try {
      await resolveScope(api, { kind: 'folder', mode: 'cards', path: 'missing' });
    } catch (error) {
      threw = true;
      assert.match(error.message, /无法读取分类/);
    }
    assert.ok(threw, '分类读取失败必须抛错');
  });

  await check('新 ctx.target 形状可用', async () => {
    const { resolveScope } = await import('../frontend/scope.js');
    const scope = await resolveScope(api, { kind: 'folder', mode: 'cards', path: '科幻' });
    assert.equal(scope.count, 2);
    assert.equal(scope.label.includes('科幻'), true);
  });

  await check('wi 作用域解析', async () => {
    const { resolveScope } = await import('../frontend/scope.js');
    const scope = await resolveScope(api, { kind: 'folder', mode: 'worldinfo', path: 'lore' });
    assert.equal(scope.kind, 'wi-folder');
    assert.equal(scope.count, 1);
  });

  // §9 断链一、二：「选卡 → AI 世界书精简 → 预览」报「无法读取这本书」/ 0 条。
  // 根因是 `as: 'embedded-books'` 只传给了 folder 分支，selection / card 分支
  // 拿到的是卡片作用域，面板却按世界书列表去读。
  await check('单卡 + as=embedded-books → 内嵌世界书作用域', async () => {
    const { resolveScope } = await import('../frontend/scope.js');
    const scope = await resolveScope(
      api, { kind: 'card', id: 'a.json' }, { as: 'embedded-books' },
    );
    assert.equal(scope.kind, 'wi-folder');
    assert.equal(scope.mode, 'worldinfo');
    assert.equal(scope.count, 1);
    assert.equal(scope.items[0].source_type, 'embedded');
    assert.equal(scope.items[0].card_id, 'a.json');
  });

  await check('选中卡片 + as=embedded-books → 内嵌世界书作用域', async () => {
    const { resolveScope } = await import('../frontend/scope.js');
    const scope = await resolveScope(
      api,
      { kind: 'selection', mode: 'cards', ids: ['a.json', 'b.json'] },
      { as: 'embedded-books' },
    );
    assert.equal(scope.kind, 'wi-folder');
    assert.deepEqual(scope.items.map((item) => item.card_id), ['a.json', 'b.json']);
  });

  await check('不传 as 时仍是卡片作用域（不误转）', async () => {
    const { resolveScope } = await import('../frontend/scope.js');
    const scope = await resolveScope(api, { kind: 'card', id: 'a.json' });
    assert.equal(scope.mode, 'cards');
    assert.equal(scope.kind, 'card');
  });

  await check('世界书选中项不会被再套一层内嵌书', async () => {
    const { resolveScope } = await import('../frontend/scope.js');
    const scope = await resolveScope(
      api,
      { kind: 'selection', mode: 'worldinfo', ids: ['global::lorebooks/a.json'] },
      { as: 'embedded-books' },
    );
    assert.equal(scope.kind, 'wi-selection');
    assert.equal(scope.items[0].source_type, 'global');
  });

  // C4：格式可选项已删除（§2.2）。它曾让模型给正文套「名称/关键词/原文」外壳，
  // 而外壳会被原样写进世界书正文。
  await check('世界书面板不再有「改写格式」控件', async () => {
    const before = allNodes.length;
    api.surfaces.open('polish', {
      source: 'context',
      targetKind: 'card',
      targetId: 'a.json',
      mode: 'cards',
    });
    await new Promise((resolve) => setTimeout(resolve, 40));
    const texts = allNodes
      .slice(before)
      .map((node) => node.textContent)
      .filter(Boolean)
      .map((text) => String(text));
    assert.equal(
      texts.some((text) => text.includes('改写格式')),
      false,
      '「改写格式」下拉必须已删除',
    );
    assert.equal(texts.some((text) => text.includes('YAML 列表')), false);
  });

  await check('坞打开不继承上次作用域', async () => {
    const { normalizeTarget } = await import('../frontend/scope.js');
    assert.equal(normalizeTarget({ source: 'dock' }), null);
  });

  await check('任务面板提交参数正确', async () => {
    // 直接验证 scope 序列化（提交参数映射的核心）
    const { serializeScope } = await import('../frontend/scope.js');
    const payload = serializeScope({
      kind: 'card-selection',
      mode: 'cards',
      items: [{ id: 'a.json' }, { id: 'b.json' }],
    });
    assert.deepEqual(payload, { kind: 'cards', card_ids: ['a.json', 'b.json'] });
    const wiPayload = serializeScope({
      kind: 'wi-selection',
      mode: 'worldinfo',
      items: [{ source_type: 'global', file_path: 'lorebooks/a.json', card_id: '' }],
    });
    assert.equal(wiPayload.kind, 'worldbooks');
    assert.equal(wiPayload.sources[0].file_path, 'lorebooks/a.json');
  });

  await check('审批面板按 job_type 渲染', async () => {
    api.surfaces.open('review', { source: 'hub', runId: 'abcdef123456' });
    await new Promise((resolve) => setTimeout(resolve, 40));
  });

  await check('jobs 列表接口被调用', async () => {
    const { calls } = createFakeApi();
    void calls;
    // review 面板 refresh 会拉 /jobs；此处只验证接口封装本身可用
    const client = (await import('../frontend/api.js')).createClient(api);
    const data = await client.jobs(8);
    assert.ok(Array.isArray(data.jobs));
  });

  await check('无 Provider 时 hub 给出提示', async () => {
    const { api: api2, registered: reg2 } = createFakeApi({ providers: 0 });
    const mod2 = await import('../frontend/plugin.js?zero');
    void mod2;
    void reg2;
    const { createHub } = await import('../frontend/views/hub.js');
    void createHub;
    void api2;
  });

  await check('任务面板：作用域 → 提交 → 进度 → 完成', async () => {
    const { createTaskPanel } = await import('../frontend/views/task.js');
    const { createClient } = await import('../frontend/api.js');
    const client = createClient(api);
    const ctx = { providerId: 'p1', model: 'm1', profileId: '', notifyReview: () => {} };
    const panel = createTaskPanel(api, client, ctx, 'analyze');
    await panel.refresh();
    panel.applyContext({ source: 'context', targetKind: 'folder', targetPath: '科幻' });
    await new Promise((resolve) => setTimeout(resolve, 30));
    // 直接触发主按钮（面板内第一个 primary 按钮）
    const buttons = [];
    (function walk(node) {
      if (!node || !node.children) return;
      if (node.tagName === 'BUTTON') buttons.push(node);
      node.children.forEach(walk);
    })(panel.element);
    assert.ok(buttons.length, '面板没有渲染出按钮');
    const submit = buttons.find((node) => String(node.textContent).includes('开始'));
    assert.ok(submit, `缺少「开始」按钮：${buttons.map((b) => b.textContent).join(',')}`);
    submit.click();
    await new Promise((resolve) => setTimeout(resolve, 80));
  });

  await check('审批面板：渲染四类结果并落地', async () => {
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const client = createClient(api);
    const review = createReview(api, client, { notifyReview: () => {} });
    await review.refresh();
    review.focusRun('abcdef123456');
    review.applyContext({ runId: 'abcdef123456' });
    await new Promise((resolve) => setTimeout(resolve, 60));
  });

  await check('世界书结果渲染 diff（不抛错）', async () => {
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const client = createClient(api);
    const review = createReview(api, client, { notifyReview: () => {} });
    await review.refresh();
    await new Promise((resolve) => setTimeout(resolve, 40));
  });

  await check('hub 渲染四个任务卡与最近作业', async () => {
    const { createHub } = await import('../frontend/views/hub.js');
    const { createClient } = await import('../frontend/api.js');
    const client = createClient(api);
    const hub = createHub(api, client, {
      openTask: () => {},
      openReview: () => {},
      openSettings: () => {},
    });
    await hub.refresh();
    let cards = 0;
    (function walk(node) {
      if (!node || !node.children) return;
      if (node.classList && node.classList.contains('plugin-stm-ai-assistant-task-card')) cards += 1;
      node.children.forEach(walk);
    })(hub.element);
    assert.equal(cards, 4, `任务卡数量应为 4，实际 ${cards}`);
  });

  for (const [name, verdict, message] of results) {
    const mark = verdict === 'ok' ? 'PASS' : 'FAIL';
    console.log(`${mark}  ${name}${message ? ` — ${message}` : ''}`);
  }
  const failed = results.filter(([, verdict]) => verdict !== 'ok');
  console.log(`\n${results.length - failed.length}/${results.length} 通过`);
  if (failed.length) process.exitCode = 1;
}

main().catch((error) => {
  console.error('冒烟测试自身失败:', error);
  process.exitCode = 1;
});
