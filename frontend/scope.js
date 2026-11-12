/**
 * 作用域解析 —— 插件里**唯一**读取宿主上下文的地方。
 *
 * 设计约定（见 docs/2026-10-19-task-centric-redesign.md §5）：
 *
 * 宿主把「这次点击的语义目标」放在 `ctx.target` 里：
 *
 *   { kind: 'selection' | 'folder' | 'card' | 'none',
 *     mode: 'cards' | 'worldinfo' | ...,
 *     path: '科幻/太空',      // folder 时的分类路径（'' = 根目录）
 *     id: 'sub/card.json',    // card 时
 *     ids: ['…'] }            // selection 时（宿主选中集，仅作初始值）
 *
 * 插件**自己解析**成具体条目（不依赖宿主预解析），因此：
 * - 右键菜单点击瞬间不需要宿主拉全量卡片；
 * - 「分类不存在」「读取失败」能被区分出来并如实报错，而不是显示「0 张」。
 *
 * 宿主契约（`ST-Manager/static/js/pluginSurfaceHost.js` 的 `buildOpenContext`）：
 * 旧的扁平字段 `targetKind` / `targetPath` / `targetIds` / `targetId` 已被
 * **删除**，宿主在构造点主动丢弃它们。因此这里只读 `ctx.target` 与 `ctx.entryId`，
 * 不做任何旧形状兼容——留一条「读不到就读旧字段」的退路只会让契约重新分叉。
 */

export class ScopeError extends Error {
  constructor(message, code = 'scope_failed') {
    super(message);
    this.name = 'ScopeError';
    this.code = code;
  }
}

/** 取宿主的 `ctx.target`；没有作用域时返回 null。 */
export function normalizeTarget(ctx) {
  if (!ctx) return null;
  const target = ctx.target && typeof ctx.target === 'object' ? ctx.target : null;
  if (!target) return null;
  const kind = String(target.kind || '');
  if (!kind || kind === 'none') return null;
  return {
    kind,
    mode: String(target.mode || ctx.mode || ''),
    path: String(target.path == null ? '' : target.path),
    id: String(target.id || ''),
    ids: Array.isArray(target.ids) ? target.ids.map(String) : [],
  };
}

/** 读取宿主当前选中集（`api.host.selection()`，宿主已提供）。 */
export function readHostSelection(api) {
  try {
    if (api && api.host && typeof api.host.selection === 'function') {
      const data = api.host.selection() || {};
      return { mode: String(data.mode || ''), ids: (data.ids || []).map(String) };
    }
  } catch (error) {
    console.warn('[stm-ai] api.host.selection 调用失败:', error);
  }
  return { mode: '', ids: [] };
}

/** 列出世界书来源（`api.worldbook.sources()`，宿主已提供）。 */
/**
 * 世界书来源列表。
 *
 * `all: true` 走宿主的全量枚举（`sources({all:true})` 返回 Promise）：分页拉取、
 * 每页让步、总量封顶，并回传 `{items, total, truncated}`。
 * `truncated` 必须往上传——「分类里还有没取到的书」绝不能当成完整作用域去跑，
 * 否则用户以为改了整个分类，实际只改了一部分。
 */
export async function listWorldbookSources(api, { category = '', all = false } = {}) {
  const empty = { items: [], total: 0, truncated: false };
  try {
    if (!api || !api.worldbook || typeof api.worldbook.sources !== 'function') return empty;
    // 同步数组与 Promise 都能 await：默认路径仍是缓存，all 路径是网络
    const data = await api.worldbook.sources(all ? { category, all: true } : { category });
    if (Array.isArray(data)) return { items: data, total: data.length, truncated: false };
    if (data && Array.isArray(data.items)) {
      return {
        items: data.items,
        total: Number(data.total) || data.items.length,
        truncated: Boolean(data.truncated),
      };
    }
  } catch (error) {
    console.warn('[stm-ai] api.worldbook.sources 调用失败:', error);
  }
  return empty;
}

// ---------------------------------------------------------------- 解析

const labelOfPath = (path) => (path === '' ? '根目录' : `「${path}」`);

/**
 * 把 `target` 解析成面板可用的作用域对象。
 *
 * @param {object} api 插件 API 门面
 * @param {object|null} target 宿主传来的语义目标
 * @param {{as?: 'cards'|'embedded-books'}} [options]
 *   `as: 'embedded-books'` 用于「右键卡片分类 → 精简这些卡的内嵌世界书」：
 *   先把分类解析成卡片，再把每张卡映射成一个 embedded 世界书来源。
 *   这一个参数替代了「按入口 id 猜语义」的做法——面板知道自己要什么。
 *
 * @returns {Promise<null | {
 *   kind: string, mode: string, label: string,
 *   items: Array, count: number, huge: boolean, empty: boolean,
 * }>}
 * @throws {ScopeError} 解析失败（分类不存在、权限、宿主接口缺失…）
 */
export async function resolveScope(api, target, options = {}) {
  if (!target || target.kind === 'none') return null;

  const as = String(options.as || 'cards');

  if (target.kind === 'selection') {
    const scope = await resolveSelection(api, target);
    // `as: 'embedded-books'` 必须一路传到这里：以前只有 folder 分支认这个选项，
    // 「选中几张卡 → 精简它们的卡片内嵌世界书」会拿到卡片作用域，面板于是按
    // 世界书列表去读，报「无法读取这本书 / 0 条」（§9 断链一、二）。
    // 只转 card 模式的作用域——世界书选中项本身已经是书，不能再套一层。
    return as === 'embedded-books' && scope.mode === 'cards'
      ? cardsToEmbeddedBooks(scope)
      : scope;
  }
  if (target.kind === 'card') {
    const scope = await resolveCard(api, target);
    return as === 'embedded-books' && scope.mode === 'cards'
      ? cardsToEmbeddedBooks(scope)
      : scope;
  }
  if (target.kind === 'folder') {
    if (target.mode === 'cards') {
      const cards = await resolveCardFolder(api, target);
      return as === 'embedded-books' ? cardsToEmbeddedBooks(cards) : cards;
    }
    if (target.mode === 'worldinfo') return resolveWorldbookFolder(api, target);
    throw new ScopeError(`不支持在「${target.mode}」视图里按分类操作`, 'unsupported_mode');
  }
  throw new ScopeError(`未知的作用域类型：${target.kind}`, 'unsupported_scope');
}

/**
 * 卡片集合 → 内嵌世界书来源集合。
 *
 * 注意：这里**不预读**每张卡有没有 character_book——那要 N 次读卡。
 * 真正读不到的书由 `/wi/entries` 逐本标记（`readable: false`），
 * 面板里显示「该卡片没有内嵌世界书」，其余书照常处理。
 */
function cardsToEmbeddedBooks(cardScope) {
  const items = cardScope.items.map((card) => ({
    source_type: 'embedded',
    file_path: '',
    card_id: card.id,
    category: card.category || '',
    name: card.name || card.id,
  }));
  return {
    kind: 'wi-folder',
    mode: 'worldinfo',
    label: `${cardScope.label}的卡片内嵌世界书`,
    items,
    count: items.length,
    empty: items.length === 0,
    huge: items.length > 50,
  };
}

async function resolveSelection(api, target) {
  const ids = (target.ids || []).map(String).filter(Boolean);
  if (!ids.length) {
    throw new ScopeError('这个入口没有带选中项：请先在列表里勾选，或从坞打开后手动选择', 'empty_selection');
  }
  if (target.mode === 'worldinfo') {
    const wanted = new Set(ids);
    const { items: cached } = await listWorldbookSources(api);
    let books = cached.filter((book) => wanted.has(bookIdOf(book)));
    if (!books.length) {
      // 默认分支只覆盖世界书视图的**当前页**：选中项可能已被翻页/筛选挤出缓存。
      // 补一次全量枚举再判断，否则「不在缓存里」会被误报成「接口不一致」。
      const { items: everyBook } = await listWorldbookSources(api, { all: true });
      books = everyBook.filter((book) => wanted.has(bookIdOf(book)));
    }
    if (!books.length) {
      // 宿主给了 id 但全量枚举里也找不到：宁可报错也不静默分析 0 本
      throw new ScopeError('无法识别选中的世界书（宿主列表里找不到这些 id）', 'unknown_selection');
    }
    return bookScope(books, 'selected');
  }
  return cardIdsScope(api, ids, 'selected');
}

async function resolveCard(api, target) {
  const id = String(target.id || '');
  if (!id) throw new ScopeError('右键目标为空', 'empty_target');
  return cardIdsScope(api, [id], 'single');
}

async function resolveCardFolder(api, target) {
  const path = String(target.path || '');
  let data;
  try {
    data = await callCardsList(api, { category: path, limit: 0 });
  } catch (error) {
    throw new ScopeError(`无法读取分类${labelOfPath(path)}：${error.message}`, 'list_failed');
  }
  const items = (data.items || []).map((card) => ({
    id: String(card.id || ''),
    name: String(card.name || card.file_name || ''),
    category: String(card.category || ''),
    token_count: Number(card.token_count || 0),
  })).filter((card) => card.id);

  return {
    kind: 'card-folder',
    mode: 'cards',
    label: `分类${labelOfPath(path)}（含子分类）`,
    items,
    count: items.length,
    empty: items.length === 0,
    huge: items.length > 200,
  };
}

async function resolveWorldbookFolder(api, target) {
  const path = String(target.path || '');
  // 右键的那个分类可能远大于当前页：必须走全量枚举，否则作用域静默少书。
  // 枚举本身分页 + 封顶，不会因为「分类里有一千本」把页面拖住。
  const { items: books, total, truncated } = await listWorldbookSources(api, {
    category: path,
    all: true,
  });
  const scope = bookScope(books, 'folder', `分类${labelOfPath(path)}`);
  return { ...scope, total, truncated };
}

function cardIdsScope(api, ids, label) {
  const unique = Array.from(new Set(ids.map(String).filter(Boolean)));
  return {
    kind: label === 'single' ? 'card' : 'card-selection',
    mode: 'cards',
    label: label === 'single' ? '单张卡片' : '选中的卡片',
    items: unique.map((id) => ({ id })),
    count: unique.length,
    empty: unique.length === 0,
    huge: unique.length > 200,
  };
}

function bookScope(books, label, customLabel = '') {
  const items = books.map((book) => ({
    source_type: book.source_type,
    file_path: book.file_path || '',
    card_id: sourceCardId(book),
    category: book.category || '',
    name: book.name || '',
  }));
  return {
    kind: label === 'folder' ? 'wi-folder' : 'wi-selection',
    mode: 'worldinfo',
    label: customLabel || (label === 'folder' ? '世界书分类' : '选中的世界书'),
    items,
    count: items.length,
    empty: items.length === 0,
    huge: items.length > 50,
  };
}

/**
 * 世界书来源里**该不该带 `card_id`**。
 *
 * 宿主 `core/plugins/api.py::_resolve_worldbook_source` 的判断是
 * `if source_type in ('embedded', 'card') or card_id:` —— 只要 `card_id` 非空，
 * **无论 `source_type` 写的是什么**，都会按「角色卡内嵌世界书」去解析。
 *
 * 但宿主 `api.worldbook.sources()` 给**资源目录世界书**也填了 `card_id`
 * （那是资源目录所属角色卡的 `owner_card_id`，见 `pluginHost.js:1383`）。
 * 原样转发就会把「资源书 2 条」读成「那张卡的内嵌书 120 条」：预览与作业都
 * 作用在**另一本书**上，写回时同样的错位会把候选写进内嵌书。
 *
 * 所以只有 `embedded` 才带 `card_id`；global / resource 一律留空，
 * 让宿主按 `file_path` 解析。
 */
export function sourceCardId(book) {
  if (!book || typeof book !== 'object') return '';
  const type = String(book.source_type || book.type || '').toLowerCase();
  if (type !== 'embedded' && type !== 'card') return '';
  return String(book.card_id || '');
}

/**
 * 世界书来源的稳定标识。
 *
 * **优先取宿主回传的列表 `id`**：`resolveSelection` 要拿它和宿主选中集
 * （`api.host.selection()` / `ctx.target.ids`）求交集，而选中集存的就是世界书
 * 列表项 id。用 `source_type` + `file_path` 自己拼是对不上的：`file_path` 是
 * **绝对路径**，而宿主 id 里带的是相对世界书根的路径——
 *   global   → 宿主 `global::sub/a.json`，自拼 `global::D:/…/lorebooks/sub/a.json`
 *   resource → 宿主 `resource::<key>::a.json`，自拼 `resource::D:/…/a.json`
 * 于是全局/资源书永远筛不出东西，「精简选中的世界书」报「无法识别选中的世界书」；
 * 内嵌书因为只用 `card_id` 拼，反而是唯一正常的。§9 断链三。
 *
 * `id` 缺席（宿主较旧、没回传该字段）时退回旧拼法：仍然解析不出全局/资源书，
 * 但至少不自造出会误伤内嵌书的 id。
 */
export function bookIdOf(book) {
  if (!book || typeof book !== 'object') return '';
  const hostId = String(book.id || '');
  if (hostId) return hostId;
  if (book.card_id && book.source_type === 'embedded') return `embedded::${book.card_id}`;
  return `${book.source_type}::${book.file_path}`;
}

/** 卡片集合：`api.cards.list` 的薄封装，兼容不同返回形状。 */
export async function callCardsList(api, { category = '', limit = 0, ids = [] } = {}) {
  // 前端 API 门面**没有** `api.cards` 命名空间（那是后端的 Python 域），
  // 只能走插件自己的 HTTP 路由。旧实现直接调 `api.cards.list` → 运行时
  // 「Cannot read properties of undefined」，「分析此分类」整个不可用。
  const params = [];
  if (ids.length) {
    params.push(`ids=${encodeURIComponent(ids.join(','))}`);
  } else {
    if (category) params.push(`category=${encodeURIComponent(category)}`);
    // `limit=0` = 该分类下全部（后端兜底上限），截断会让「分析此分类」静默漏卡
    params.push(`limit=${encodeURIComponent(String(limit))}`);
  }
  const query = params.length ? `?${params.join('&')}` : '';
  const data = await api.api(`/cards${query}`);
  if (!data || data.success !== true) {
    throw new Error((data && (data.msg || data.error)) || '读取卡片列表失败');
  }
  return { items: Array.isArray(data.items) ? data.items : [] };
}

/** 把作用域序列化成后端可消费的形状（提交作业时用）。 */
export function serializeScope(scope) {
  if (!scope) return null;
  if (scope.kind === 'card' || scope.kind === 'card-selection' || scope.kind === 'card-folder') {
    return { kind: 'cards', card_ids: scope.items.map((item) => item.id) };
  }
  if (scope.kind === 'wi-selection' || scope.kind === 'wi-folder') {
    return {
      kind: 'worldbooks',
      sources: scope.items.map((item) => ({
        source_type: item.source_type,
        file_path: item.file_path,
        // 只有内嵌书才允许带 card_id（见 `sourceCardId` 的注释：宿主见到
        // card_id 就按内嵌书解析，资源书会被整个读错）。
        card_id: sourceCardId(item),
      })),
    };
  }
  return null;
}

/** 作用域一行摘要（面板顶部横幅用）。 */
export function describeScope(scope) {
  if (!scope) return '尚未选择作用域';
  const unit = scope.mode === 'worldinfo' ? '本世界书' : '张卡片';
  return `${scope.label} · ${scope.count} ${unit}`;
}
