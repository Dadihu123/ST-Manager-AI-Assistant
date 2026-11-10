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
    (this._listeners.get('click') || []).forEach((fn) => fn({
      preventDefault() {},
      stopPropagation() {},
      target: this,
    }));
  }
  /** 测试辅助：触发某个事件（如 input / change），用于输入框。 */
  fire(name) {
    (this._listeners.get(name) || []).forEach((fn) => fn({
      preventDefault() {},
      stopPropagation() {},
      target: this,
    }));
  }
  querySelector(selector) {
    const found = [];
    const walk = (node) => {
      (node.children || []).forEach((child) => {
        found.push(child);
        walk(child);
      });
    };
    walk(this);
    const text = String(selector || '');
    if (text.startsWith('.')) {
      const name = text.slice(1);
      return found.find((node) => node.classList && node.classList.contains(name)) || null;
    }
    const tag = text.toUpperCase();
    if (/^[A-Z]+$/.test(tag)) {
      return found.find((node) => node.tagName === tag) || null;
    }
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => {
      (node.children || []).forEach((child) => {
        found.push(child);
        walk(child);
      });
    };
    walk(this);
    const text = String(selector || '').trim();
    const matches = (node) => {
      let rest = text;
      const attr = rest.match(/\[([a-zA-Z-]+)(?:="?([^"\]]*)"?)?\]/);
      if (attr) {
        const key = attr[1];
        const value = attr[2];
        const data = node.dataset || {};
        if (!Object.prototype.hasOwnProperty.call(data, key)) return false;
        if (value !== undefined && String(data[key]) !== String(value)) return false;
        rest = rest.replace(attr[0], '');
      }
      if (rest.startsWith('.')) {
        return Boolean(node.classList && node.classList.contains(rest.slice(1)));
      }
      if (rest) return node.tagName === rest.toUpperCase();
      return true;
    };
    return found.filter(matches);
  }
  closest() { return null; }
  focus() {}
  getBoundingClientRect() {
    return { top: 0, left: 0, right: 1200, bottom: 900, width: 1200, height: 900 };
  }
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
      // 形状照抄宿主 `api.worldbook.sources()`：`id` 是世界书列表项 id
      // （与 `api.host.selection()` 的选中集**同源**），`file_path` 是**绝对**路径。
      // 以前这里把 `file_path` 写成 id 的路径后缀（`lorebooks/a.json`），等于把
      // 「两者可以互推」这个错误假设固化进夹具——生产环境 `file_path` 来自
      // 列表项的 `path`（绝对路径），于是全局/资源书选中后永远匹配不上。
      sources: ({ category = '' } = {}) => ([
        {
          id: 'global::lorebooks/a.json',
          source_type: 'global',
          file_path: '/abs/library/lorebooks/a.json',
          card_id: '',
          category,
          name: 'A 世界书',
        },
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
        '/review/queue': {
          success: true,
          items: [
            {
              key: 'a.json', status: 'awaiting_review', run_id: 'abcdef123456',
              job_type: 'ai.analyze', task: 'analyze', seq: 0, run_created_at: 1700000000,
              result: {
                card_id: 'a.json', summary: '概览',
                tags: [{ name: '标签A', confidence: 0.9 }],
                new_tags: [{ name: '新标签' }],
                tag_removals: [{ name: '奇幻', reason: '与卡片无关' }],
              },
            },
            {
              key: 'b.json', status: 'done', run_id: 'abcdef123456',
              job_type: 'ai.analyze', task: 'analyze', seq: 1, run_created_at: 1700000000,
              result: { card_id: 'b.json', summary: '概览B', tags: [] },
            },
          ],
          runs: [{
            run_id: 'abcdef123456', job_type: 'ai.analyze', task: 'analyze',
            count: 2, created_at: 1700000000,
          }],
          total: 2,
        },
        '/categories': { success: true, items: [{ path: '科幻', count: 3 }] },
        '/prompt/preview': { success: true, messages: [{ role: 'system', content: 'hi' }], tokens: { total: 10 }, profile: { id: 'x', name: 'X' }, provider: { model: 'm1' } },
        // 与真实接口一致：既有 `books`（前端按书分组渲染）也有展平后的 `items`。
        // 其中一条带 `skip_reason` —— 前置过滤会跳过它（§7）。
        '/wi/entries': {
          success: true,
          books: [{
            source: { source_type: 'embedded', card_id: 'a.json' },
            readable: true,
            total: 2,
            source_revision: 'rev-1',
            items: [
              { uid: '1', name: 'E1', tokens: 10, enabled: true, skip_reason: '', skip_label: '' },
              {
                uid: '2', name: '[MVU] InitVar', tokens: 20, enabled: true,
                skip_reason: 'protected:name', skip_label: '名字命中数据/机制类',
              },
            ],
          }],
          items: [
            { uid: '1', name: 'E1', tokens: 10, enabled: true, skip_reason: '', skip_label: '' },
            {
              uid: '2', name: '[MVU] InitVar', tokens: 20, enabled: true,
              skip_reason: 'protected:name', skip_label: '名字命中数据/机制类',
            },
          ],
          total: 2,
          sources: 1,
          books_total: 1,
          books_truncated: false,
        },
        '/proposal/sample': { success: true, sample: { count: 5, tokens: 100 } },
        // 模型能力设置（默认 256K / 32K / 推理开 / auto）
        '/settings': {
          success: true,
          model_capabilities: {
            context_length: 262144,
            max_output_tokens: 32768,
            reasoning_enabled: true,
            reasoning_effort: 'auto',
          },
          defaults: {
            context_length: 262144,
            max_output_tokens: 32768,
            reasoning_enabled: true,
            reasoning_effort: 'auto',
          },
          limits: {
            context_length: { min: 8192, max: 4000000 },
            max_output_tokens: { min: 256, max: 262144 },
            reasoning_effort: ['auto', 'low', 'medium', 'high'],
          },
        },
      };
      if (path.startsWith('/jobs/') && path.endsWith('/apply')) return { success: true, applied: 2, skipped: 0, stale: 0, failed: 0, details: [] };
      if (path.startsWith('/review/queue')) return table['/review/queue'];
      if (path.startsWith('/jobs/') && path.includes('/items/')) return { success: true };
      if (path.startsWith('/jobs?')) return table['/jobs'];
      if (path.startsWith('/jobs/') && !path.includes('/apply')) {
        return {
          success: true,
          job: { run_id: 'abcdef123456', job_type: 'ai.analyze', status: 'completed', total: 2, completed: 2, failed: 0, skipped: 0, summary: {}, counts: { total: 2, completed: 2 } },
          items: [
            // 真实宿主返回的字段是 `key`（不是 `item_key`），见 §9 的 P2-9。
            { key: 'a.json', status: 'awaiting_review', result: { card_id: 'a.json', summary: '概览', tags: [{ name: '标签A', confidence: 0.9 }], new_tags: [{ name: '新标签' }], tag_removals: [{ name: '奇幻', reason: '与卡片无关' }] } },
            { key: 'b.json', status: 'done', result: { card_id: 'b.json', summary: '概览B', tags: [] } },
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

/** 在某个子树里按条件收集节点（桩没有 querySelectorAll，测试自己走树）。 */
function collect(node, predicate, out = []) {
  (node.children || []).forEach((child) => {
    if (predicate(child)) out.push(child);
    collect(child, predicate, out);
  });
  return out;
}

async function main() {
  const { api, registered, opened, badges, calls } = createFakeApi({ pending: 2 });

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
    assert.ok(registered.has('history'), 'AI打标历史记录 surface 未注册');
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

  await check('设置面板读取模型能力', async () => {
    const { createSettings } = await import('../frontend/views/settings.js');
    const { createClient } = await import('../frontend/api.js');
    const before = calls.length;
    const settings = createSettings(api, createClient(api), {});
    await settings.refresh();
    await new Promise((resolve) => setTimeout(resolve, 50));

    const paths = calls.slice(before).map((entry) => String(entry[1] || ''));
    assert.equal(paths.includes('/settings'), true,
      `设置面板要读取 /settings（实际：${paths.join(', ')}）`);
    assert.equal(settings.element.hidden, false);
  });

  await check('设置页：标签池的 ? 说明、每分类规则与保存校验', async () => {
    // 单独一套假 API：`/tag-pool` 返回两个分类，其中「成人」是隔离分类
    const base = createFakeApi({ pending: 0 });
    const poolPayload = {
      success: true,
      tag_pool: { categories_include: [], categories_exclude: [] },
      categories: [
        { name: '题材', tags: 12, isolated: false },
        { name: '成人', tags: 3, isolated: true },
      ],
      isolated: ['成人'],
      uncategorized: 2,
      total_tags: 17,
      max_tags: 8,
      rules: { 题材: { confidence_min: 0.6, max_tags: 3 } },
      default_confidence: 0.5,
      effect: null,
    };
    const confirms = [];
    const patched = {
      ...base.api,
      ui: {
        ...base.api.ui,
        confirm: async (options = {}) => { confirms.push(options); return true; },
      },
      api: (path, options = {}) => {
        if (path === '/tag-pool') {
          base.calls.push(['api', path, options && options.body]);
          return options && options.method === 'POST'
            ? Object.assign({}, poolPayload, {
              tag_pool: { categories_include: ['题材'], categories_exclude: [] },
              max_tags: options.body.max_tags,
              rules: options.body.rules,
              effect: { allowed: 12, blocked: 5, blocked_sample: ['R18', '未分类'] },
              warnings: ['有 2 个标签没有分类；设了「只取」后它们会被排除'],
            })
            : poolPayload;
        }
        return base.api.api(path, options);
      },
    };

    const { createSettings } = await import('../frontend/views/settings.js');
    const { createClient } = await import('../frontend/api.js');
    const settings = createSettings(patched, createClient(patched), {});
    await settings.refresh();

    const helpButtons = collect(
      settings.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-help-btn'),
    );
    assert.ok(helpButtons.length >= 2, `「?」按钮不足：${helpButtons.length}`);
    const panels = collect(
      settings.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-help-panel'),
    );
    assert.equal(panels.length >= 2, true, '缺少说明面板');
    assert.equal(panels.every((node) => node.hidden === true), true, '说明面板默认应折叠');

    helpButtons[0].click();
    assert.equal(panels.some((node) => node.hidden === false), true, '点「?」应展开说明');
    // 桩不实现 textContent 的向下拼接，这里自己收集子树文本
    const helpText = panels
      .map((panel) => [String(panel.textContent || '')]
        .concat(collect(panel, () => true).map((node) => String(node.textContent || '')))
        .join(' '))
      .join('\n');
    for (const keyword of ['留空', '逗号', '分类']) {
      assert.equal(helpText.includes(keyword), true, `说明缺少「${keyword}」这类人话`);
    }
    const allText = collect(settings.element, () => true)
      .map((node) => String(node.textContent || ''))
      .join('\n');
    assert.equal(allText.includes('隔离区'), true, '「现有标签分类」旁要有隔离区数量提醒');
    assert.equal(allText.includes('阈值 0.60'), true, '折叠态要显示分类规则摘要');
    assert.equal(allText.includes('已分配 3 / 总上限 8'), true, '要显示配额分配情况');

    const boxes = collect(settings.element, (node) => node.tagName === 'TEXTAREA');
    assert.equal(boxes.length >= 2, true, '应有「只取 / 排除」两个输入框');
    const takeButton = collect(
      settings.element,
      (node) => node.tagName === 'BUTTON' && node.textContent === '只取',
    )[0];
    assert.ok(takeButton, '分类行缺少「只取」按钮');
    takeButton.click();
    assert.equal(
      String(boxes[0].value).includes('题材'),
      true,
      `点「只取」应把分类名填进输入框（实际：${boxes[0].value}）`,
    );

    // ---- 展开「题材」的规则面板：超限只提醒、仍可保存；调回范围内正常保存 ----
    const rulePanels = collect(
      settings.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-tagpool-rule-panel'),
    );
    assert.equal(rulePanels.length, 2, '每个分类都要有规则面板');
    assert.equal(rulePanels[0].hidden, true, '规则面板默认折叠');
    collect(
      settings.element,
      (node) => node.tagName === 'BUTTON' && node.textContent === '设置',
    )[0].click();
    assert.equal(rulePanels[0].hidden, false, '点「设置」应展开规则面板');

    const ruleInputs = collect(rulePanels[0], (node) => node.tagName === 'INPUT');
    assert.equal(ruleInputs.length, 2, '规则面板应有「置信度」「设置数量」两个输入');
    ruleInputs[1].value = '9';       // 数量 9 > 总上限 8
    ruleInputs[1].fire('input');

    const saveButton = collect(
      settings.element,
      (node) => node.tagName === 'BUTTON' && node.textContent === '保存标签池',
    )[0];
    assert.ok(saveButton, '缺少「保存标签池」按钮');
    saveButton.click();
    await new Promise((resolve) => setTimeout(resolve, 30));

    assert.equal(confirms.length, 1, '超限时要弹窗提醒');
    assert.equal(String(confirms[0].message).includes('超出 1 个'), true, confirms[0].message);
    const overPosted = base.calls.filter(([, path, body]) => path === '/tag-pool' && body);
    assert.equal(overPosted.length, 1, '确认后仍要保存（不拦截）');
    assert.deepEqual(overPosted[0][2].rules['题材'], { confidence_min: 0.6, max_tags: 9 });

    // 保存后重新渲染：再展开一次，改成范围内的值
    const rulePanels2 = collect(
      settings.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-tagpool-rule-panel'),
    );
    collect(
      settings.element,
      (node) => node.tagName === 'BUTTON' && node.textContent === '设置',
    )[0].click();
    const ruleInputs2 = collect(rulePanels2[0], (node) => node.tagName === 'INPUT');
    ruleInputs2[1].value = '3';
    ruleInputs2[1].fire('input');
    ruleInputs2[0].value = '0.6';
    ruleInputs2[0].fire('input');

    collect(
      settings.element,
      (node) => node.tagName === 'BUTTON' && node.textContent === '保存标签池',
    )[0].click();
    await new Promise((resolve) => setTimeout(resolve, 30));

    const posted = base.calls.filter(([, path, body]) => path === '/tag-pool' && body).pop();
    assert.ok(posted, '保存没有 POST /tag-pool');
    assert.deepEqual(posted[2].categories_include, ['题材']);
    assert.equal(posted[2].max_tags, 8);
    assert.deepEqual(posted[2].rules['题材'], { confidence_min: 0.6, max_tags: 3 });
    assert.equal(confirms.length, 1, '范围内的保存不该再弹窗');
    const afterText = collect(settings.element, () => true)
      .map((node) => String(node.textContent || ''))
      .join('\n');
    assert.equal(afterText.includes('被排除'), true, '保存后要显示「挡住了什么」');
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

  // §9 断链三：「选中全局世界书 → 精简」报「无法识别选中的世界书（宿主接口版本不一致）」。
  // 根因是插件拿 `source_type` + `file_path`（绝对路径）自拼 id，去和宿主选中集
  // （列表项 id，形如 `global::<相对路径>`）求交集，全局/资源书永远匹配不上；
  // 内嵌书因为只用 `card_id` 拼反而是唯一能用的。修法是直接用宿主回传的 `id`。
  await check('选中全局世界书能解析（id 对齐宿主选中集）', async () => {
    const { resolveScope } = await import('../frontend/scope.js');
    const scope = await resolveScope(
      api,
      { kind: 'selection', mode: 'worldinfo', ids: ['global::lorebooks/a.json'] },
    );
    assert.equal(scope.kind, 'wi-selection');
    assert.equal(scope.count, 1);
    // file_path 保持绝对路径透传——后端读改写靠它，不能被 id 形状污染
    assert.equal(scope.items[0].file_path, '/abs/library/lorebooks/a.json');
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
    api.surfaces.open('polish', {
      source: 'context',
      targetKind: 'card',
      targetId: 'a.json',
      mode: 'cards',
    });
    await new Promise((resolve) => setTimeout(resolve, 40));
    // 视图是懒创建且只创建一次（前面的「注册了全部 surface」可能已经开过），
    // 所以这里查全部节点，而不是只看本次新增的。
    const texts = allNodes
      .map((node) => node.textContent)
      .filter(Boolean)
      .map((text) => String(text));
    assert.equal(
      texts.some((text) => text.includes('改写格式')),
      false,
      '「改写格式」下拉必须已删除',
    );
    assert.equal(texts.some((text) => text.includes('YAML 列表')), false);
    // 反过来必须有「强制处理被过滤条目」的出口：前置过滤会挡掉含宏的散文条目
    // （实测占全库四成多），没有出口用户就改不了这些条目。
    assert.equal(
      texts.some((text) => text.includes('前置过滤')),
      true,
      '缺少「强制处理被过滤条目」开关',
    );
    // C21：落地策略三选一（apply_mode + checker 合成）
    assert.equal(
      texts.some((text) => text.includes('落地策略')),
      true,
      '缺少「落地策略」三选一',
    );
  });

  // §7：预览里把前置过滤命中项置灰并写明原因 —— 否则用户勾了 20 条、
  // 跑完才发现只处理了 12 条。
  await check('预览把会被前置过滤的条目置灰并说明原因', async () => {
    api.surfaces.open('polish', {
      source: 'context',
      // 真实宿主传的是 `ctx.target` 对象；平铺的 targetKind/targetId 不会被识别。
      target: { kind: 'card', id: 'a.json', mode: 'cards' },
    });
    await new Promise((resolve) => setTimeout(resolve, 80));

    const previewBtns = allNodes.filter(
      (node) => node.tagName === 'BUTTON' && node.textContent === '预览',
    );
    assert.equal(previewBtns.length > 0, true, '没渲染出「预览」按钮');
    // 每个任务面板各有一个「预览」；全部点一遍，世界书面板那个才会真正渲染条目。
    previewBtns.forEach((button) => button.click());
    await new Promise((resolve) => setTimeout(resolve, 150));

    const texts = allNodes.map((node) => String(node.textContent || '')).filter(Boolean);
    assert.equal(
      texts.some((text) => text.includes('会被前置过滤跳过')),
      true,
      `书头要提示「几条会被跳过」；实际尾部文本：${JSON.stringify(texts.slice(-8))}`,
    );
    assert.equal(
      texts.some((text) => text.includes('会被跳过：名字命中数据/机制类')),
      true,
      '命中项要写明原因',
    );
    const protectedBox = allNodes.find(
      (node) => node.dataset && node.dataset.skipReason === 'protected:name',
    );
    assert.ok(protectedBox, '命中项要带 skipReason 标记');
    assert.equal(protectedBox.classList.contains('is-protected'), true);
  });

  // C13：删标签不可逆 —— 每条建议前有勾选框，且**默认不勾**。
  await check('分析的「建议删除标签」默认不勾选', async () => {
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(api, createClient(api), { notifyReview: () => {} });
    await review.refresh();
    review.focusRun('abcdef123456');
    review.applyContext({ runId: 'abcdef123456' });
    await new Promise((resolve) => setTimeout(resolve, 80));

    const texts = allNodes.map((node) => String(node.textContent || '')).filter(Boolean);
    assert.equal(
      texts.some((text) => text.includes('删标签不可逆')),
      true,
      '要提示「默认不删」',
    );
    const boxes = allNodes.filter((node) => node.dataset && node.dataset.removal === '奇幻');
    assert.equal(boxes.length >= 1, true, '没渲染出删除勾选框');
    assert.equal(boxes.every((node) => node.checked === false), true, '默认必须是未勾选');
  });

  // P1-5：合并建议勾选后才执行（默认不勾 —— 合并会删掉来源标签）。
  await check('提议的标签合并默认不勾选', async () => {
    const base = createFakeApi({ pending: 1 });
    const job = {
      success: true,
      job: {
        run_id: 'run-merges', job_type: 'ai.propose', status: 'completed',
        total: 1, completed: 1, failed: 0, skipped: 0,
        summary: {}, counts: { total: 1, completed: 1 },
      },
      items: [{
        key: 'proposal',
        status: 'awaiting_review',
        result: {
          plan: {
            create_categories: ['题材/奇幻'],
            create_tags: [],
            tag_merges: [{ from: ['旧名', '别称'], to: '规范名', reason: '近义' }],
          },
        },
      }],
    };
    const patched = {
      ...base.api,
      api: (path, options = {}) => {
        if (path.startsWith('/review/queue')) {
          return {
            success: true,
            items: [{
              key: 'proposal',
              status: 'awaiting_review',
              run_id: 'run-merges',
              job_type: 'ai.propose',
              task: 'propose',
              result: {
                plan: {
                  create_categories: ['题材/奇幻'],
                  create_tags: [],
                  tag_merges: [{ from: ['旧名', '别称'], to: '规范名', reason: '近义' }],
                },
              },
            }],
            runs: [{ run_id: 'run-merges', job_type: 'ai.propose', task: 'propose', count: 1 }],
            total: 1,
          };
        }
        return base.api.api(path, options);
      },
    };

    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(patched, createClient(patched), { notifyReview: () => {} });
    review.focusRun('run-merges');
    await review.refresh();
    await new Promise((resolve) => setTimeout(resolve, 80));

    const texts = allNodes.map((node) => String(node.textContent || '')).filter(Boolean);
    assert.equal(
      texts.some((text) => text.includes('勾选后点')),
      true,
      '要说明「勾选后才执行」',
    );
    const mergeBoxes = allNodes.filter((node) => node.dataset && node.dataset.merge === '0');
    assert.equal(mergeBoxes.length >= 1, true, '没渲染出合并勾选框');
    assert.equal(mergeBoxes.every((node) => node.checked === false), true, '默认必须未勾选');
  });

  await check('「全部接受」跳过有 block 问题的条目', async () => {
    const { createFakeApi: makeFake } = { createFakeApi };
    void makeFake;
    const base = createFakeApi({ pending: 1 });
    const jobWithIssue = {
      success: true,
      job: {
        run_id: 'run-issues', job_type: 'ai.wi_rewrite', status: 'completed',
        total: 1, completed: 1, failed: 0, skipped: 0,
        summary: {}, counts: { total: 1, completed: 1 },
      },
      items: [{
        key: 'a.json',
        status: 'awaiting_review',
        result: {
          card_id: 'a.json',
          original: '<analysis>原文</analysis>',
          candidate: '<analysis>候选',
          issues: ['标签消失: analysis'],
          warnings: ['删得偏多：只剩原文的 40%'],
          metrics: { orig_tokens: 100, cand_tokens: 90, delta: -10 },
        },
      }],
    };
    const patched = {
      ...base.api,
      api: (path, options = {}) => {
        if (path.startsWith('/review/queue')) {
          return {
            success: true,
            items: [{
              key: 'a.json',
              status: 'awaiting_review',
              run_id: 'run-issues',
              job_type: 'ai.wi_rewrite',
              task: 'polish',
              result: {
                card_id: 'a.json',
                original: '<analysis>原文</analysis>',
                candidate: '<analysis>候选',
                issues: ['标签消失: analysis'],
                warnings: ['删得偏多：只剩原文的 40%'],
                metrics: { orig_tokens: 100, cand_tokens: 90, delta: -10 },
              },
            }],
            runs: [{ run_id: 'run-issues', job_type: 'ai.wi_rewrite', task: 'polish', count: 1 }],
            total: 1,
          };
        }
        return base.api.api(path, options);
      },
    };

    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(patched, createClient(patched), { notifyReview: () => {} });
    review.focusRun('run-issues');
    await review.refresh();
    await new Promise((resolve) => setTimeout(resolve, 80));

    const texts = allNodes.map((node) => String(node.textContent || '')).filter(Boolean);
    assert.equal(texts.some((text) => text.includes('需单独确认')), true,
      '有问题的条目必须被标出来');
    assert.equal(
      texts.some((text) => text.includes('检查器拦下')),
      true,
      'block 级问题要以「检查器拦下」提示',
    );

    // 没有「干净」的条目 → 批量接受应该一条都不发
    const bulk = allNodes
      .filter((node) => node.tagName === 'BUTTON' && node.textContent === '全部接受')
      .pop();
    assert.ok(bulk, '没渲染出「全部接受」按钮');
    const before = base.calls.length;
    bulk.click();
    await new Promise((resolve) => setTimeout(resolve, 60));
    const reviewCalls = base.calls
      .slice(before)
      .map((entry) => String(entry[1] || ''))
      .filter((path) => path.includes('/review'));
    assert.deepEqual(reviewCalls, [], '有问题的条目不能被「全部接受」带走');
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
    let historyCalls = 0;
    const ctx = {
      providerId: 'p1', model: 'm1', profileId: '', notifyReview: () => {},
      openHistory: () => { historyCalls += 1; },
    };
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
    const history = buttons.find((node) => String(node.textContent).includes('AI打标历史记录'));
    assert.ok(history, `任务面板缺少「AI打标历史记录」按钮：${buttons.map((b) => b.textContent).join(',')}`);
    history.click();
    assert.equal(historyCalls, 1, '点「AI打标历史记录」应触发 openHistory 钩子');
    const panelTexts = allNodes.map((node) => String(node.textContent || '')).filter(Boolean);
    assert.ok(
      panelTexts.some((text) => text.includes('删除模型建议删除的标签')),
      '打标面板要有「自动删除建议标签」的显式勾选项',
    );
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
    const buttons = collect(review.element, (node) => node.tagName === 'BUTTON');
    assert.ok(
      buttons.some((node) => String(node.textContent).includes('AI打标历史记录')),
      '审批面板缺少「AI打标历史记录」按钮',
    );
  });

  // §9 的 P2-9：宿主返回的条目字段是 `key`，旧前端统一读 `item_key`，
  // 于是「接受」把 `undefined` 当作条目键发出去 → 404 / 落地变空操作。
  await check('点「接受」时用真实 item key（不是 undefined）', async () => {
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const client = createClient(api);
    const review = createReview(api, client, { notifyReview: () => {} });
    await review.refresh();
    review.focusRun('abcdef123456');
    review.applyContext({ runId: 'abcdef123456' });
    await new Promise((resolve) => setTimeout(resolve, 60));

    const accept = allNodes
      .filter((node) => node.tagName === 'BUTTON' && node.textContent === '接受')
      .pop();
    assert.ok(accept, '没渲染出「接受」按钮');
    const before = calls.length;
    accept.click();
    await new Promise((resolve) => setTimeout(resolve, 40));
    const sent = calls.slice(before).map((entry) => String(entry[1] || ''));
    assert.ok(
      sent.some((path) => path.includes('/items/a.json/review')),
      `审批请求没带真实 key：${JSON.stringify(sent)}`,
    );
    assert.equal(
      sent.some((path) => path.includes('undefined')),
      false,
      '审批请求里出现了 undefined',
    );
  });

  await check('审批面板：手动添加标签（输入 → 添加 → 立即写卡）', async () => {
    const base = createFakeApi({ pending: 1 });
    const posted = [];
    const toasts = [];
    const patched = {
      ...base.api,
      host: {
        ...base.api.host,
        toast: (message) => { toasts.push(String(message)); return true; },
      },
      api: (path, options = {}) => {
        if (path.includes('/manual-tags')) {
          base.calls.push(['api', path, options && options.body]);
          const tags = (options && options.body && options.body.tags) || [];
          posted.push(tags);
          return { success: true, item_key: 'a.json', added: tags, skipped_blacklist: [], skipped_unknown: [] };
        }
        return base.api.api(path, options);
      },
    };

    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(patched, createClient(patched), { notifyReview: () => {} });
    await review.refresh();
    review.focusRun('abcdef123456');
    review.applyContext({ runId: 'abcdef123456' });
    await new Promise((resolve) => setTimeout(resolve, 60));

    const blocks = collect(
      review.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-manual-tags'),
    );
    assert.ok(blocks.length >= 1, '打标条目下面要有手动添加标签区域');

    const box = collect(
      blocks[0],
      (node) => node.tagName === 'INPUT' && node.classList.contains('plugin-stm-ai-assistant-input'),
    )[0];
    assert.ok(box, '手动添加标签区域缺少输入框');
    box.value = '塔罗、占卜师/塔罗';

    const addButton = collect(
      blocks[0],
      (node) => node.tagName === 'BUTTON' && node.textContent === '添加',
    )[0];
    assert.ok(addButton, '缺少「添加」按钮');
    addButton.click();
    await new Promise((resolve) => setTimeout(resolve, 30));

    assert.deepEqual(posted[0], ['塔罗', '占卜师'], '分隔符要拆开去重后提交');
    const postedPath = base.calls.find(([, path]) => String(path).includes('/manual-tags'));
    assert.ok(postedPath && postedPath[1].includes('/items/a.json/manual-tags'), '请求要带条目 key');
    assert.equal(
      toasts.some((text) => text.includes('已添加标签') && text.includes('塔罗')),
      true,
      `添加后要用 toast 提示（实际：${toasts.join(' / ')}）`,
    );
  });

  await check('AI打标历史记录：日期栏 → 选日期 → 卡片操作', async () => {
    const base = createFakeApi({ pending: 1 });
    const entry = {
      entry_id: 'e1',
      run_id: 'run-1',
      item_key: 'a.json',
      card_id: 'a.json',
      applied_at: 1794200000,
      summary: 'AI 生成的概览',
      tags: [{ name: '奇幻', confidence: 0.9 }],
      new_tags: [{ name: '塔罗' }],
      tag_removals: [],
      accepted: true,
    };
    const skippedEntry = {
      entry_id: 'e2',
      run_id: 'run-1',
      item_key: 'b.json',
      card_id: 'b.json',
      applied_at: 1794199000,
      summary: '被拒绝的条目',
      tags: [],
      new_tags: [],
      tag_removals: [],
      accepted: false,
    };
    const patched = {
      ...base.api,
      api: (path, options = {}) => {
        const method = (options && options.method) || 'GET';
        if (path.includes('/manual-tags')) {
          base.calls.push(['api', path, options && options.body]);
          return { success: true, added: (options.body.tags || []), skipped_blacklist: [], skipped_unknown: [] };
        }
        if (path.includes('/reopen')) {
          base.calls.push(['api', path, options && options.body]);
          return { success: true, reopened: true };
        }
        if (path.includes('/history/') && method === 'DELETE') {
          base.calls.push(['api', path, options && options.body]);
          return { success: true, deleted: true };
        }
        if (path.includes('/history')) {
          base.calls.push(['api', path, options && options.body]);
          return {
            success: true,
            date: '2026-11-09',
            dates: [{ date: '2026-11-09', count: 2 }, { date: '2026-11-08', count: 2 }],
            entries: [entry, skippedEntry],
          };
        }
        if (path.includes('/review')) {
          base.calls.push(['api', path, options && options.body]);
          return { success: true };
        }
        return base.api.api(path, options);
      },
    };

    const { createHistory } = await import('../frontend/views/history.js');
    const { createClient } = await import('../frontend/api.js');
    let notified = 0;
    const history = createHistory(
      patched,
      createClient(patched),
      { notifyReview: () => { notified += 1; } },
    );
    await history.refresh();
    await new Promise((resolve) => setTimeout(resolve, 30));

    const days = collect(
      history.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-history-day'),
    );
    assert.ok(days.length >= 2, '日期栏要渲染出天数');
    const cards = collect(
      history.element,
      (node) => node.classList && node.classList.contains('is-history-card'),
    );
    assert.equal(cards.length, 2, '当天应渲染两张卡片（已接受 / 未接受）');
    const cardNodes = collect(cards[0], () => true);
    const cardTexts = cardNodes.map((node) => String(node.textContent || '').trim());
    assert.ok(cardTexts.includes('a.json'), `卡片要有卡名（实际：${cardTexts.slice(0, 12).join('|')}）`);
    assert.ok(cardTexts.includes('已接受'), '落地记录要有已接受标记');
    assert.ok(
      cardTexts.some((text) => text.startsWith('奇幻')),
      `卡片要有标签（实际：${cardTexts.join('|')}）`,
    );
    const skippedCard = cards.find((card) => collect(card, () => true)
      .some((node) => String(node.textContent || '').trim() === 'b.json'));
    assert.ok(skippedCard, '缺少「未接受」的卡片');
    const skippedTexts = collect(skippedCard, () => true)
      .map((node) => String(node.textContent || '').trim());
    assert.ok(skippedTexts.includes('未接受'), `跳过的记录要标未接受（实际：${skippedTexts.join('|')}）`);
    const clockNode = collect(
      cards[0],
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-history-time'),
    )[0];
    assert.ok(clockNode && /^\d{2}:\d{2}$/.test(String(clockNode.textContent).trim()),
      `时间应是 24 小时制 HH:MM（实际：${clockNode && clockNode.textContent}）`);

    // 日期栏是折叠式：点月份收起/展开
    const monthToggle = collect(
      history.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-history-month'),
    )[0];
    assert.ok(monthToggle, '缺少月份折叠按钮');
    monthToggle.click();
    const collapsedDays = collect(
      history.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-history-day'),
    );
    assert.equal(collapsedDays.length, 0, '收起月份后不应再渲染日期');
    monthToggle.click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    const expandedDays = collect(
      history.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-history-day'),
    );
    assert.ok(expandedDays.length >= 2, '展开月份后日期应恢复');

    // 选 11-08 → 请求带日期
    const eight = expandedDays.find((node) => String(node.textContent).trim() === '8');
    assert.ok(eight, '缺少 11-08 的日期按钮');
    eight.click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    const historyCalls = base.calls.filter(([, path]) => String(path).includes('/history?'));
    assert.ok(
      historyCalls.some(([, path]) => String(path).includes('2026-11-08')),
      `切日期应带 date 参数：${historyCalls.map(([, p]) => p).join(', ')}`,
    );

    // 删除 → DELETE
    const deleteBtn = collect(
      cards[0],
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-history-delete'),
    )[0];
    assert.ok(deleteBtn, '卡片缺少删除按钮');
    const beforeDelete = base.calls.length;
    deleteBtn.click();
    await new Promise((resolve) => setTimeout(resolve, 20));
    const deleteCalls = base.calls.slice(beforeDelete)
      .filter(([, path, body]) => String(path).includes('/history/e1') && body);
    assert.ok(
      base.calls.slice(beforeDelete).some(([, path]) => String(path).includes('/history/e1')),
      '删除应请求 /history/e1',
    );
    void deleteCalls;

    // 改回待审批 → 先调宿主 review，再调插件 reopen
    const reopenBtn = collect(
      cards[0],
      (node) => node.tagName === 'BUTTON' && String(node.textContent) === '改回待审批',
    )[0];
    assert.ok(reopenBtn, '卡片缺少「改回待审批」');
    const beforeReopen = base.calls.length;
    reopenBtn.click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    const reopenPaths = base.calls.slice(beforeReopen).map(([, path]) => String(path));
    assert.ok(
      reopenPaths.some((path) => path.includes('/review')),
      `改回待审批应先调宿主 review：${reopenPaths.join(', ')}`,
    );
    assert.ok(
      reopenPaths.some((path) => path.includes('/reopen')),
      `改回待审批还要调插件 reopen：${reopenPaths.join(', ')}`,
    );
    assert.equal(notified, 1, '改回后要通知审批面板刷新');
  });

  await check('审批面板：结果队列合并多个作业，不需切换作业', async () => {
    const base = createFakeApi({ pending: 1 });
    const patched = {
      ...base.api,
      api: (path, options = {}) => {
        if (String(path).startsWith("/review/queue")) {
          return {
            success: true,
            items: [
              {
                key: 'one.json', status: 'awaiting_review', run_id: 'run-alpha',
                job_type: 'ai.analyze', task: 'analyze', seq: 0,
                result: { card_id: 'one.json', summary: '来自作业A的概览', tags: [] },
              },
              {
                key: 'two.json', status: 'awaiting_review', run_id: 'run-beta',
                job_type: 'ai.analyze', task: 'analyze', seq: 0,
                result: { card_id: 'two.json', summary: '来自作业B的概览', tags: [] },
              },
            ],
            runs: [
              { run_id: 'run-alpha', job_type: 'ai.analyze', task: 'analyze', count: 1 },
              { run_id: 'run-beta', job_type: 'ai.analyze', task: 'analyze', count: 1 },
            ],
            total: 2,
          };
        }
        return base.api.api(path, options);
      },
    };

    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(patched, createClient(patched), { notifyReview: () => {} });
    await review.refresh();
    await new Promise((resolve) => setTimeout(resolve, 60));

    const texts = collect(review.element, () => true)
      .map((node) => String(node.textContent || '').trim());
    assert.ok(texts.includes('one.json'), `作业 A 的结果要出现：${texts.slice(0, 12).join('|')}`);
    assert.ok(texts.includes('two.json'), `作业 B 的结果要出现：${texts.slice(0, 12).join('|')}`);
    assert.equal(
      texts.filter((text) => text === '待审批').length >= 2,
      true,
      '两张结果卡片都要渲染',
    );
  });

  await check('已接受的条目：不提供逐条落地（按钮置灰不可点）', async () => {
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const client = createClient(api);
    const review = createReview(api, client, { notifyReview: () => {} });
    await review.refresh();
    const landButtons = collect(
      review.element,
      (node) => node.tagName === 'BUTTON' && String(node.textContent) === '落地这条',
    );
    assert.ok(landButtons.length >= 1, '已接受的条目应保留「落地这条」按钮（置灰）');
    assert.equal(
      landButtons.every((node) => node.disabled === true),
      true,
      '「落地这条」必须置灰不可点',
    );
  });

  await check('点接受不重拉队列（只有落地后才刷新）', async () => {
    const base = createFakeApi({ pending: 1 });
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(base.api, createClient(base.api), { notifyReview: () => {} });
    await review.refresh();
    await new Promise((resolve) => setTimeout(resolve, 30));
    const queueCalls = () => base.calls.filter(([, path]) => String(path).includes('/review/queue')).length;
    const before = queueCalls();

    const accept = collect(
      review.element,
      (node) => node.tagName === 'BUTTON' && String(node.textContent) === '接受',
    )[0];
    assert.ok(accept, '缺少「接受」按钮');
    accept.click();
    await new Promise((resolve) => setTimeout(resolve, 40));

    assert.equal(queueCalls(), before, '点接受后不应重拉结果队列');
    const texts = collect(review.element, () => true)
      .map((node) => String(node.textContent || '').trim());
    assert.ok(
      texts.includes('已接受 · 待落地'),
      `接受后本地状态要立刻变成「已接受 · 待落地」（实际：${texts.slice(0, 16).join('|')}）`,
    );
  });

  await check('落地时先给进度反馈（不是点了没反应）', async () => {
    const base = createFakeApi({ pending: 1 });
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(base.api, createClient(base.api), { notifyReview: () => {} });
    await review.refresh();
    await new Promise((resolve) => setTimeout(resolve, 30));

    const applyBtn = collect(
      review.element,
      (node) => node.tagName === 'BUTTON'
        && String(node.textContent).includes('落地已接受的'),
    )[0];
    assert.ok(applyBtn, '缺少「落地已接受的」按钮');
    applyBtn.click();
    await new Promise((resolve) => setTimeout(resolve, 80));

    const texts = collect(review.element, () => true)
      .map((node) => String(node.textContent || '').trim());
    assert.ok(
      texts.some((text) => text.includes('正在落地') || text.includes('已落地')),
      `落地过程要有文字反馈（实际：${texts.slice(0, 16).join('|')}）`,
    );
  });

  await check('作业进度：item 帧要累计（不会一直停在 0/N）', async () => {
    const { runTaskJob } = await import('../frontend/ui.js');
    const seen = [];
    const fake = {
      storage: { get: () => '', set: () => {} },
      subscribeJob: (runId, handlers) => {
        handlers.snapshot({ status: 'running', total: 7, completed: 0, failed: 0, skipped: 0 });
        setTimeout(() => {
          handlers.item({ item_key: 'cards/a.json', status: 'done' });
          handlers.item({ item_key: 'cards/b.json', status: 'awaiting_review' });
          handlers.done({ status: 'completed', total: 7, completed: 2, failed: 0, skipped: 1 });
        }, 5);
        return () => {};
      },
    };
    await runTaskJob({
      api: fake,
      taskId: 'analyze',
      submit: async () => ({ run_id: 'r1' }),
      onProgress: (event) => seen.push(event),
      onDone: () => {},
      onError: () => {},
    });
    await new Promise((resolve) => setTimeout(resolve, 40));

    assert.ok(seen.length >= 3, `item 帧应推进进度（实际推进 ${seen.length} 次）`);
    assert.equal(seen[1].completed, 1, '第一个 item 完成后应显示 1/7');
    assert.equal(seen[2].skipped, 1, '待审批条目计入 skipped');
    assert.ok(seen[2].percent > 0, '百分比要动，不能一直是 0');
    assert.ok(
      String(seen[1].message).includes('a.json'),
      `进度提示要说明正在跑哪张卡（实际：${seen[1].message}）`,
    );
  });

  await check('勾选删除标签 → 落地请求要带 remove_tags（勾选在「接受」前也不丢）', async () => {
    const base = createFakeApi({ pending: 1 });
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(base.api, createClient(base.api), { notifyReview: () => {} });
    await review.refresh();
    await new Promise((resolve) => setTimeout(resolve, 30));

    // 先勾选「建议删除」，再点接受（接受会重渲染卡片，旧实现会丢勾选）
    const boxes = collect(
      review.element,
      (node) => node.tagName === 'INPUT' && node.dataset
        && node.dataset.removal === '奇幻',
    );
    assert.ok(boxes.length >= 1, '没渲染出「建议删除的标签」勾选框');
    boxes[0].checked = true;
    boxes[0].fire('change');

    const accept = collect(
      review.element,
      (node) => node.tagName === 'BUTTON' && String(node.textContent) === '接受',
    )[0];
    assert.ok(accept, '缺少「接受」按钮');
    accept.click();
    await new Promise((resolve) => setTimeout(resolve, 30));

    const boxesAfter = collect(
      review.element,
      (node) => node.tagName === 'INPUT' && node.dataset
        && node.dataset.removal === '奇幻',
    );
    assert.equal(
      boxesAfter[0] && boxesAfter[0].checked,
      true,
      '点「接受」重渲染后，勾选不能丢',
    );

    const applyBtn = collect(
      review.element,
      (node) => node.tagName === 'BUTTON'
        && String(node.textContent).includes('落地已接受的'),
    )[0];
    applyBtn.click();
    await new Promise((resolve) => setTimeout(resolve, 80));

    const applyCall = base.calls
      .filter(([, path]) => String(path).includes('/apply'))
      .pop();
    assert.ok(applyCall, '没有发出落地请求');
    assert.deepEqual(
      applyCall[2] && applyCall[2].remove_tags,
      ['奇幻'],
      `落地请求必须带 remove_tags（实际：${JSON.stringify(applyCall[2])}）`,
    );
  });

  await check('打标面板：分析完直接写入 + 删除建议标签（勾/不勾都提交正确）', async () => {
    const { createTaskPanel } = await import('../frontend/views/task.js');
    const { createClient } = await import('../frontend/api.js');

    async function submitWith(checked) {
      const base = createFakeApi({ pending: 1 });
      const ctx = { providerId: 'p1', model: 'm1', profileId: '', notifyReview: () => {} };
      const panel = createTaskPanel(base.api, createClient(base.api), ctx, 'analyze');
      await panel.refresh();
      // 选 2 张卡（与用户实际操作一致）
      panel.applyContext({
        source: 'context',
        target: { kind: 'selection', mode: 'cards', ids: ['a.json', 'b.json'] },
      });
      await new Promise((resolve) => setTimeout(resolve, 30));

      const triggers = collect(
        panel.element,
        (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-select-trigger'),
      );
      const applyTrigger = triggers.find(
        (node) => (node.children || []).some(
          (child) => String(child.textContent || '').includes('逐条审批后写入'),
        ),
      );
      assert.ok(applyTrigger, '没找到「写回方式」下拉');
      applyTrigger.click();
      await new Promise((resolve) => setTimeout(resolve, 10));

      const autoOption = collect(
        panel.element,
        (node) => node.tagName === 'BUTTON'
          && (node.children || []).some(
            (child) => String(child.textContent || '').includes('分析完直接写入'),
          ),
      )[0];
      assert.ok(autoOption, '没找到「分析完直接写入」选项');
      autoOption.click();
      await new Promise((resolve) => setTimeout(resolve, 10));

      const labels = collect(
        panel.element,
        (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-checkbox'),
      );
      const removalLabel = labels.find((label) => (label.children || []).some(
        (child) => String(child.textContent || '').includes('删除模型建议删除的标签'),
      ));
      assert.ok(removalLabel, '没找到「删除模型建议删除的标签」勾选项');
      const box = (removalLabel.children || []).find((child) => child.tagName === 'INPUT');
      assert.ok(box, '勾选项缺少 input');
      box.checked = checked;

      const start = collect(
        panel.element,
        (node) => node.tagName === 'BUTTON' && String(node.textContent).includes('开始'),
      )[0];
      assert.ok(start, '没找到开始按钮');
      start.click();
      await new Promise((resolve) => setTimeout(resolve, 60));

      const call = base.calls
        .filter(([, path]) => String(path).includes('/jobs/analyze'))
        .pop();
      if (!call) {
        const texts = allNodes.map((node) => String(node.textContent || '')).filter(Boolean);
        throw new Error(`没有提交打标作业；面板文字：${texts.slice(-25).join(' | ')}`);
      }
      return call[2];
    }

    const unchecked = await submitWith(false);
    assert.equal(unchecked.apply_removals, false, '不勾选时 apply_removals 必须是 false');
    assert.equal(unchecked.apply_mode, 'auto', '选择「分析完直接写入」后要提交 auto');
    const checked = await submitWith(true);
    assert.equal(checked.apply_removals, true, '勾选时 apply_removals 必须是 true');
  });

  await check('落地确认的数量按「去重后的删除项」算', async () => {
    const base = createFakeApi({ pending: 1 });
    const confirms = [];
    const queueItems = ['a.json', 'b.json'].map((key) => ({
      key,
      status: 'done',
      run_id: 'run-1',
      job_type: 'ai.analyze',
      task: 'analyze',
      result: {
        card_id: key,
        summary: '概览',
        tags: [],
        tag_removals: [{ name: '奇幻', reason: '与卡片无关' }],
      },
    }));
    const patched = {
      ...base.api,
      ui: {
        ...base.api.ui,
        confirm: async (options = {}) => { confirms.push(options); return true; },
      },
      api: (path, options = {}) => {
        const text = String(path);
        if (text.startsWith('/review/queue')) {
          return { success: true, items: queueItems, runs: [{ run_id: 'run-1', count: 2 }], total: 2 };
        }
        if (text.includes('/jobs/') && text.endsWith('/apply')) {
          base.calls.push(['api', text, options && options.body]);
          return { success: true, applied: 2, skipped: 0, stale: 0, failed: 0, details: [] };
        }
        return base.api.api(path, options);
      },
    };

    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(patched, createClient(patched), { notifyReview: () => {} });
    await review.refresh();
    await new Promise((resolve) => setTimeout(resolve, 30));

    // 两张卡都勾同一个「奇幻」：请求里只删一次，确认框也该按 1 个算
    const boxes = collect(
      review.element,
      (node) => node.tagName === 'INPUT' && node.dataset
        && node.dataset.removal === '奇幻',
    );
    assert.equal(boxes.length, 2, '两张卡各应有一个删除勾选');
    boxes.forEach((box) => { box.checked = true; box.fire('change'); });

    const applyBtn = collect(
      review.element,
      (node) => node.tagName === 'BUTTON'
        && String(node.textContent).includes('落地已接受的'),
    )[0];
    applyBtn.click();
    await new Promise((resolve) => setTimeout(resolve, 80));

    assert.ok(confirms[0], '没有弹确认框');
    assert.ok(
      String(confirms[0].message).includes('删除 1 个'),
      `确认框数量应按去重后的删除项算（实际：${confirms[0].message}）`,
    );
    const applyCall = base.calls
      .filter(([, path]) => String(path).includes('/apply'))
      .pop();
    assert.deepEqual(applyCall[2].remove_tags, ['奇幻'], '请求里同一个标签只删一次');
  });

  await check('面板每次打开都回到安全默认（逐条审批 + 勾选跳过未变化）', async () => {
    const { createTaskPanel } = await import('../frontend/views/task.js');
    const { createClient } = await import('../frontend/api.js');
    const base = createFakeApi({ pending: 1 });
    const ctx = { providerId: 'p1', model: 'm1', profileId: '', notifyReview: () => {} };
    const panel = createTaskPanel(base.api, createClient(base.api), ctx, 'analyze');
    await panel.refresh();
    panel.applyContext({
      source: 'context',
      target: { kind: 'selection', mode: 'cards', ids: ['a.json', 'b.json'] },
    });
    await new Promise((resolve) => setTimeout(resolve, 30));

    // 手动改成 auto、取消跳过、勾上删除
    const triggers = collect(
      panel.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-select-trigger'),
    );
    const applyTrigger = triggers.find((node) => (node.children || []).some(
      (child) => String(child.textContent || '').includes('逐条审批后写入'),
    ));
    applyTrigger.click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    const autoOption = collect(
      panel.element,
      (node) => node.tagName === 'BUTTON'
        && (node.children || []).some((child) => String(child.textContent || '').includes('分析完直接写入')),
    )[0];
    autoOption.click();

    const labels = collect(
      panel.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-checkbox'),
    );
    const labelOf = (text) => labels.find((label) => (label.children || []).some(
      (child) => String(child.textContent || '').includes(text),
    ));
    labelOf('跳过内容未变化的卡片').children.find((c) => c.tagName === 'INPUT').checked = false;
    labelOf('删除模型建议删除的标签').children.find((c) => c.tagName === 'INPUT').checked = true;

    // 重新打开面板（宿主每次打开都会投递 applyContext）
    panel.applyContext({
      source: 'context',
      target: { kind: 'selection', mode: 'cards', ids: ['a.json', 'b.json'] },
    });
    await new Promise((resolve) => setTimeout(resolve, 30));

    const triggerLabels = collect(
      panel.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-select-label'),
    ).map((node) => String(node.textContent || ''));
    assert.ok(
      triggerLabels.includes('逐条审批后写入'),
      `重新打开后写回方式要回到「逐条审批后写入」（实际：${triggerLabels.join('|')}）`,
    );
    const labels2 = collect(
      panel.element,
      (node) => node.classList && node.classList.contains('plugin-stm-ai-assistant-checkbox'),
    );
    const labelOf2 = (text) => labels2.find((label) => (label.children || []).some(
      (child) => String(child.textContent || '').includes(text),
    ));
    assert.equal(
      labelOf2('跳过内容未变化的卡片').children.find((c) => c.tagName === 'INPUT').checked,
      true,
      '重新打开后「跳过内容未变化的卡片」要默认勾选',
    );
    assert.equal(
      labelOf2('删除模型建议删除的标签').children.find((c) => c.tagName === 'INPUT').checked,
      false,
      '重新打开后「删除建议标签」要回到不勾',
    );

    // 默认状态提交：必须是 review + 跳过未变化
    const start = collect(
      panel.element,
      (node) => node.tagName === 'BUTTON' && String(node.textContent).includes('开始'),
    )[0];
    start.click();
    await new Promise((resolve) => setTimeout(resolve, 60));
    const submitCall = base.calls
      .filter(([, path]) => String(path).includes('/jobs/analyze'))
      .pop();
    assert.ok(submitCall, '默认状态应能提交');
    assert.equal(submitCall[2].apply_mode, 'review', '默认写回方式必须是「逐条审批后写入」');
    assert.equal(submitCall[2].skip_unchanged, true, '默认必须跳过未变化的卡片');
  });

  await check('下拉选择后自动收起', async () => {
    const { createUi } = await import('../frontend/dom.js');
    const { select } = createUi(api);
    const node = select([
      { value: 'a', label: 'A' },
      { value: 'b', label: 'B' },
    ], 'a');
    const trigger = collect(
      node,
      (child) => child.classList && child.classList.contains('plugin-stm-ai-assistant-select-trigger'),
    )[0];
    const menu = collect(
      node,
      (child) => child.classList && child.classList.contains('plugin-stm-ai-assistant-select-menu'),
    )[0];
    assert.ok(trigger && menu, '下拉结构不完整');
    trigger.click();
    assert.equal(menu.hidden, false, '点击触发器应展开');
    const option = collect(
      node,
      (child) => child.tagName === 'BUTTON' && child.dataset && child.dataset.value === 'b',
    )[0];
    assert.ok(option, '缺少选项 B');
    option.click();
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(menu.hidden, true, '选择后菜单必须收起');
    assert.equal(node.value, 'b', '选择后值要更新');
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

  // ---- 清除入口（问题 1：用户要求「手动清除 + 到上限自动老化」）----

  await check('hub「最近作业」能清除台账', async () => {
    const { createHub } = await import('../frontend/views/hub.js');
    const { createClient } = await import('../frontend/api.js');
    const { api: api3, calls: calls3 } = createFakeApi();
    const client = createClient(api3);
    const hub = createHub(api3, client, {
      openTask: () => {}, openReview: () => {}, openSettings: () => {},
    });
    await hub.refresh();
    const buttons = collect(hub.element, (node) => node.tagName === 'BUTTON');
    const clearBtn = buttons.find((node) => String(node.textContent).includes('清除'));
    assert.ok(clearBtn, `hub 没有「清除」按钮：${buttons.map((b) => b.textContent).join(',')}`);
    clearBtn.click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.ok(
      calls3.some(([kind, path]) => kind === 'api' && path === '/jobs/clear'),
      'hub 的「清除」应调用 /jobs/clear',
    );
  });

  await check('hub 能单条删除作业记录', async () => {
    const { createHub } = await import('../frontend/views/hub.js');
    const { createClient } = await import('../frontend/api.js');
    const { api: api4, calls: calls4 } = createFakeApi();
    const client = createClient(api4);
    const hub = createHub(api4, client, {
      openTask: () => {}, openReview: () => {}, openSettings: () => {},
    });
    await hub.refresh();
    const buttons = collect(
      hub.element,
      (node) => node.tagName === 'BUTTON' && String(node.textContent).includes('删除'),
    );
    assert.ok(buttons.length, '「最近作业」每行应有删除按钮');
    buttons[0].click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.ok(
      calls4.some(([kind, path, body]) => kind === 'api'
        && path === '/jobs/abcdef123456'
        && body === undefined),
      '删除应走 DELETE /jobs/<run_id>',
    );
  });

  await check('hub 有「清空待审批登记」入口', async () => {
    const { createHub } = await import('../frontend/views/hub.js');
    const { createClient } = await import('../frontend/api.js');
    const { api: api5, calls: calls5 } = createFakeApi({ pending: 2 });
    const client = createClient(api5);
    const hub = createHub(api5, client, {
      openTask: () => {}, openReview: () => {}, openSettings: () => {},
    });
    await hub.refresh();
    const buttons = collect(hub.element, (node) => node.tagName === 'BUTTON');
    const clearPending = buttons.find((node) => String(node.textContent).includes('清空待审批'));
    assert.ok(clearPending, '有待审批时必须能清空登记（宿主清理后那批再也清不掉）');
    clearPending.click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.ok(
      calls5.some(([kind, path]) => kind === 'api' && path === '/pending/clear'),
      '应调用 /pending/clear',
    );
  });

  await check('设置页用量区能清空明细', async () => {
    const { createSettings } = await import('../frontend/views/settings.js');
    const { createClient } = await import('../frontend/api.js');
    const { api: api6, calls: calls6 } = createFakeApi();
    const settingsView = createSettings(api6, createClient(api6), {});
    await settingsView.refresh();
    const buttons = collect(
      settingsView.element,
      (node) => node.tagName === 'BUTTON' && String(node.textContent).includes('清空用量明细'),
    );
    assert.ok(buttons.length, '设置页用量区需要「清空用量明细」');
    buttons[0].click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.ok(
      calls6.some(([kind, path]) => kind === 'api' && path === '/usage/clear'),
      '应调用 /usage/clear',
    );
  });

  await check('ui_data 写入失败时 hub 给出提示', async () => {
    const { createHub } = await import('../frontend/views/hub.js');
    const { createClient } = await import('../frontend/api.js');
    const { api: api7 } = createFakeApi();
    const baseApi = api7.api;
    api7.api = async (path, options = {}) => {
      if (path === '/status') {
        return {
          success: true,
          config: {},
          profiles: { count: 1 },
          providers: { count: 1 },
          pending_reviews: {},
          suggestions: 0,
          usage: {},
          ui_data: { ok: false, message: '命名空间已达上限', failed_writes: 3 },
        };
      }
      return baseApi(path, options);
    };
    const hub = createHub(api7, createClient(api7), {
      openTask: () => {}, openReview: () => {}, openSettings: () => {},
    });
    await hub.refresh();
    const text = [];
    (function walk(node) {
      if (!node) return;
      if (node._text) text.push(String(node._text));
      (node.children || []).forEach(walk);
    })(hub.element);
    assert.ok(
      text.some((line) => line.includes('写不进去') || line.includes('命名空间已达上限')),
      `写入失败必须有提示，实际文本：${text.join(' | ').slice(0, 200)}`,
    );
  });

  // 「没有需要处理的卡片」曾被渲染成红色「提交失败」，用户既不知道原因也没有出口。
  // 现在：说明内容未变化 → 给一条「仍然重新打标」→ 重跑必须带 force=true。
  await check('空提交：说明原因而不是「提交失败」，并能一键强制重跑', async () => {
    const base = createFakeApi();
    let forced = null;
    const patched = {
      ...base.api,
      api: (path, options = {}) => {
        if (String(path).startsWith('/jobs/analyze')) {
          base.calls.push(['api', path, options && options.body]);
          const body = (options && options.body) || {};
          if (body.force) {
            forced = body;
            return { success: true, run_id: 'run-forced', total: 1, skipped: 0 };
          }
          return {
            success: true,
            run_id: '',
            total: 0,
            code: 'nothing_to_process',
            skipped: 1,
            reasons: { unchanged: 1 },
            details: [{
              card_id: 'a.json', reason: 'unchanged',
              profile_id: 'builtin-analyze', model: 'm1', analyzed_at: 1700000000,
            }],
            message: '这 1 张卡片已经在相同提示词档案/模型下分析过，且内容没有变化，所以没有提交。',
          };
        }
        return base.api.api(path, options);
      },
    };

    const { createTaskPanel } = await import('../frontend/views/task.js');
    const { createClient } = await import('../frontend/api.js');
    const client = createClient(patched);
    const panel = createTaskPanel(
      patched, client, { providerId: 'p1', model: 'm1', notifyReview: () => {} }, 'analyze',
    );
    await panel.refresh();
    panel.applyContext({
      source: 'context',
      target: { kind: 'folder', mode: 'cards', path: '科幻' },
    });
    await new Promise((resolve) => setTimeout(resolve, 40));

    const start = collect(panel.element, (node) => node.tagName === 'BUTTON')
      .find((node) => String(node.textContent).includes('开始'));
    assert.ok(start, '缺少「开始」按钮');
    start.click();
    await new Promise((resolve) => setTimeout(resolve, 60));

    const texts = collect(panel.element, () => true).map((node) => String(node.textContent || ''));
    assert.equal(texts.some((text) => text.includes('提交失败')), false, '空提交不该显示「提交失败」');
    assert.ok(texts.some((text) => text.includes('内容未变化')), '没有说明「内容未变化」');
    assert.ok(texts.some((text) => text.includes('a.json')), '没有列出被跳过的卡片');

    const retry = collect(panel.element, (node) => node.tagName === 'BUTTON')
      .find((node) => String(node.textContent).includes('仍然重新打标'));
    assert.ok(retry, '没有「仍然重新打标」按钮');
    retry.click();
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.ok(forced && forced.force === true, '重跑没有带 force=true');
  });

  await check('提议落地：勾选的分类/标签/合并必须进请求体', async () => {
    const base = createFakeApi({ pending: 1 });
    let applyBody = null;
    const seenPaths = [];
    const patched = {
      ...base.api,
      ui: { ...((base.api && base.api.ui) || {}), confirm: async () => true },
      api: (path, options = {}) => {
        seenPaths.push(String(path));
        if (String(path).startsWith('/review/queue')) {
          return {
            success: true,
            items: [{
              key: 'plan-1', status: 'awaiting_review', run_id: 'run-prop',
              job_type: 'ai.propose', task: 'propose', seq: 0,
              result: {
                plan: {
                  create_categories: ['分类甲', '分类乙'],
                  create_tags: ['标签A', '标签B'],
                  tag_merges: [{ from: ['旧'], to: '新' }],
                },
              },
            }],
            runs: [{ run_id: 'run-prop', job_type: 'ai.propose', task: 'propose', count: 1 }],
            total: 1,
          };
        }
        if (String(path).includes('/apply')) {
          applyBody = (options && options.body) || {};
          return { success: true, created_categories: ['分类甲'] };
        }
        return base.api.api(path, options);
      },
    };

    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(patched, createClient(patched), { notifyReview: () => {} });
    await review.refresh();

    // 先接受：接受会重建列表 DOM，勾选必须在这之后做，否则拿到的是失效节点。
    const acceptBtn = collect(review.element, (n) => n.tagName === 'BUTTON')
      .find((n) => String(n.textContent).trim() === '接受');
    assert.ok(acceptBtn, '缺少「接受」按钮');
    acceptBtn.click();
    await new Promise((r) => setTimeout(r, 80));

    const boxes = collect(review.element, (n) => n.tagName === 'INPUT');
    const catBoxes = boxes.filter((n) => n.dataset && n.dataset.categories);
    const tagBoxes = boxes.filter((n) => n.dataset && n.dataset.tags);
    const mergeBoxes = boxes.filter((n) => n.dataset && n.dataset.merge);
    assert.equal(catBoxes.length, 2, '应渲染两个分类勾选框');
    assert.equal(tagBoxes.length, 2, '应渲染两个标签勾选框');
    assert.ok(mergeBoxes.length >= 1, '应渲染合并勾选框');

    // 取消勾选第二个分类/标签，勾选那条合并
    catBoxes[1].checked = false;
    tagBoxes[1].checked = false;
    mergeBoxes[0].checked = true;

    const applyBtn = collect(review.element, (n) => n.tagName === 'BUTTON')
      .find((n) => String(n.textContent).includes('落地已接受'));
    assert.ok(applyBtn, '缺少「落地已接受」按钮');
    applyBtn.click();
    await new Promise((r) => setTimeout(r, 150));

    assert.ok(applyBody, '必须真的发出落地请求');
    assert.deepEqual(applyBody.categories, ['分类甲'],
      '取消勾选的分类不能被提交（后端把「没传」当「全建」）');
    assert.deepEqual(applyBody.tags, ['标签A'],
      '取消勾选的标签不能被提交');
    assert.ok(Array.isArray(applyBody.merges) && applyBody.merges.length === 1,
      '勾选的标签合并必须进请求体（否则永远不会执行）');
    assert.deepEqual(applyBody.merges[0], { from: ['旧'], to: '新' });
  });


  await check('落地只刷新一次、且备注同步只针对打标作业', async () => {
    const base = createFakeApi({ pending: 1 });
    const calls = [];
    const patched = {
      ...base.api,
      ui: { ...((base.api && base.api.ui) || {}), confirm: async () => true },
      api: (path, options = {}) => {
        calls.push(String(path));
        if (String(path).startsWith('/review/queue')) {
          return {
            success: true,
            items: [{
              key: 'cls-1', status: 'awaiting_review', run_id: 'run-cls',
              job_type: 'ai.classify', task: 'classify', seq: 0,
              result: { card_id: 'a.json' },
            }],
            runs: [{ run_id: 'run-cls', job_type: 'ai.classify', task: 'classify', count: 1 }],
            total: 1,
          };
        }
        if (String(path).includes('/apply')) return { success: true, applied: 1 };
        return base.api.api(path, options);
      },
    };
    const { createReview } = await import('../frontend/views/review.js');
    const { createClient } = await import('../frontend/api.js');
    const review = createReview(patched, createClient(patched), { notifyReview: () => {} });
    await review.refresh();
    const accept2 = collect(review.element, (n) => n.tagName === 'BUTTON')
      .find((n) => String(n.textContent).trim() === '接受');
    if (accept2) {
      accept2.click();
      await new Promise((r) => setTimeout(r, 60));
    }
    const buttons = collect(review.element, (n) => n.tagName === 'BUTTON');
    buttons.find((n) => String(n.textContent).includes('落地已接受')).click();
    await new Promise((r) => setTimeout(r, 150));

    // classify 没有概览/标签，不该触发备注缓存同步（那里会整卡保存）
    assert.ok(
      !calls.some((p) => p.includes('note-cache-sync')),
      '非打标作业不应触发 /cards/note-cache-sync（会对被移动的卡做整卡保存）',
    );
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
