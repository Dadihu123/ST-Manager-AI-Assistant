"""自动分类：分类方案的构造、AI 输出的收敛、目录名安全。

纯函数层，无 flask、无主程序依赖（设计文档 §7）。

**与打标的关系**（两者共用同一套骨架：选择器 → 提示词 → 解析 → 审批 → 应用）：

| | 分类 | 打标 |
|---|---|---|
| 粒度 | 大类 | 细致 |
| 落点 | 目录（文件系统） | 卡片 `tags` |
| 数量 | 少（10–30） | 多（可达数百） |

**安全边界**：AI 生成的分类名**绝不能直接当路径用**。主程序的
`api.cards.create_category` / `move` 已内置白名单（禁 `..`、绝对路径、
`\\/:*?"<>|`、保留名），本模块再做一层**保守预检**：

- 与其让主程序静默返回 `False`，不如在**提交前**就把不合法的方案指出来；
- 也避免把「路径穿越」这类字符串送到下游（纵深防御）。
"""

import re

#: 分类名里的 Windows 非法字符与控制字符。
#: **不含 `/`** —— 它是合法的子分类分隔符（主程序也支持 `题材/奇幻`）。
ILLEGAL_NAME_CHARS = re.compile(r'[\\:*?"<>|\x00-\x1f]')

#: 允许的路径分隔（分类可以有子级，用 `/` 表达）
PATH_SEPARATOR = '/'

#: 主程序保留目录名（与 `core/plugins/domains/cards.py` 口径一致）
RESERVED_NAMES = frozenset({
    'notes', 'backups', 'lorebooks', 'thumbnails', 'cards', 'trash',
})

#: 分类名长度上限（主程序也是 64）
MAX_NAME_LENGTH = 64

#: 分类路径层数上限（防止 AI 生成超深目录）
MAX_DEPTH = 5

#: 表示「根目录」的等价写法
ROOT_ALIASES = frozenset({'', '根目录', '/', 'root', '.'})

#: 置信度下限（低于此值不自动归类，进审批时会被标注）
DEFAULT_CONFIDENCE_MIN = 0.6


def normalize_category_path(value) -> tuple:
    """把 AI 输出的分类名规范化为安全路径。

    Returns:
        `(path, error)`。`error` 非空表示不合法，此时 `path` 为空串。
        `path` 为空串且 `error` 为空表示「根目录」。
    """
    raw = '' if value is None else str(value).strip()

    if raw.lower() in ROOT_ALIASES:
        return '', ''

    # 统一分隔符（AI 可能输出反斜杠）
    text = raw.replace('\\', PATH_SEPARATOR)

    if ILLEGAL_NAME_CHARS.search(text):
        return '', f'分类名含非法字符: {raw}'

    if text.startswith(PATH_SEPARATOR):
        return '', f'分类名不能是绝对路径: {raw}'

    parts = [part.strip() for part in text.split(PATH_SEPARATOR)]
    if any(part in ('', '.', '..') for part in parts):
        return '', f'分类名含空的或越级的路径段: {raw}'

    if len(parts) > MAX_DEPTH:
        return '', f'分类层级过深（> {MAX_DEPTH}）: {raw}'

    for part in parts:
        if len(part) > MAX_NAME_LENGTH:
            return '', f'分类名过长（> {MAX_NAME_LENGTH}）: {part[:20]}…'
        if part.lower() in RESERVED_NAMES:
            return '', f'分类名与主程序保留目录冲突: {part}'

    return PATH_SEPARATOR.join(parts), ''


def normalize_scheme(raw) -> list:
    """规范化「分类方案」——一组允许的候选分类。

    方案来源可以是主程序 `taxonomy.categories`，也可以是用户手写 / AI 提议。

    Args:
        raw: `['奇幻', '科幻']` 或 `[{'name': '奇幻', 'description': '...'}]`。

    Returns:
        `[{'path', 'name', 'description', 'examples'}]`；非法项被丢弃。
    """
    items = raw if isinstance(raw, (list, tuple)) else []
    result = []
    seen = set()

    for entry in items:
        if isinstance(entry, dict):
            # `path` 优先于 `name`：规范化后的项里 `name` 只是最后一段
            # （`题材/奇幻` → name='奇幻'），先取 name 会丢掉父级路径。
            # 因此本函数对「已规范化的输入」必须是幂等的。
            name = entry.get('path') or entry.get('name') or entry.get('category')
            description = str(entry.get('description') or '').strip()
            examples = entry.get('examples')
        else:
            name = entry
            description = ''
            examples = None

        path, error = normalize_category_path(name)
        if error or not path or path in seen:
            continue

        seen.add(path)
        result.append({
            'path': path,
            'name': path.rsplit(PATH_SEPARATOR, 1)[-1],
            'description': description,
            'examples': [str(item) for item in (examples or []) if str(item or '').strip()][:5],
        })

    return result


def normalize_category_items(raw) -> list:
    """把宿主返回的**分类列表**归一成 `[{'path', 'name', 'count'}]`。

    宿主侧 `api.cards.list_categories()` 的返回形状变过（H4 / P0-6）：

    - 旧：字符串数组 `['同人', '同人/动漫']`
    - 新：对象数组 `[{'path': '同人/动漫', 'name': '动漫', 'count': 12}]`

    **两种都要能读**。插件与宿主的版本是解耦的，用户可能只升级一边；
    只认一种形状就会像修之前那样「读取现有分类永远是空的」且不报错。
    """
    items = raw if isinstance(raw, (list, tuple)) else []
    result = []
    for entry in items:
        if isinstance(entry, dict):
            path = str(entry.get('path') or entry.get('name') or '').strip()
            name = str(entry.get('name') or '').strip() or path.rsplit(PATH_SEPARATOR, 1)[-1]
            try:
                count = int(entry.get('count') or 0)
            except (TypeError, ValueError):
                count = 0
        else:
            path = str(entry or '').strip()
            name = path.rsplit(PATH_SEPARATOR, 1)[-1]
            count = 0
        if not path:
            continue
        result.append({'path': path, 'name': name, 'count': count})
    return result


def category_paths(raw) -> list:
    """只要分类路径列表（提示词注入 / 方案校验用）。"""
    return [item['path'] for item in normalize_category_items(raw)]


def format_scheme(scheme) -> str:
    """把分类方案渲染成提示词片段。

    **空方案返回空串**（理由同标签池）：占位文案会被发进提示词，
    还可能被模型当成一个真实分类。
    """
    items = normalize_scheme(scheme)
    if not items:
        return ''

    lines = []
    for item in items:
        line = f'- {item["path"]}'
        if item['description']:
            line += f'：{item["description"]}'
        if item['examples']:
            line += f'（例：{"、".join(item["examples"])}）'
        lines.append(line)

    return '\n'.join(lines)


def scheme_paths(scheme) -> set:
    """方案里允许的分类路径集合。"""
    return {item['path'] for item in normalize_scheme(scheme)}


def resolve_choice(category, scheme, *, allow_new: bool = False) -> tuple:
    """把 AI 选的分类对照方案解析成最终目标路径。

    **关键取舍**：AI 可能写出方案里没有的分类名。默认**不接受**（`allow_new=False`），
    因为「AI 顺手发明一个分类」会让卡片散落到一堆意外目录里，且难以回收。
    只有用户显式开启 `allow_new` 才放行。

    Returns:
        `(path, error)`；`error` 非空表示无法落地。
    """
    path, path_error = normalize_category_path(category)
    if path_error:
        return '', path_error

    # 根目录：只有方案里真的含根目录、或允许新分类时才接受
    if not path:
        if allow_new or '' in scheme_paths(scheme):
            return '', ''
        return '', '模型给出了根目录，但方案里没有根目录'

    allowed = scheme_paths(scheme)
    if allow_new or not allowed:
        return path, ''
    if path in allowed:
        return path, ''

    return '', f'模型给出的分类「{path}」不在方案内'


def normalize_result_category(data, scheme, *, allow_new: bool = False,
                              confidence_min: float = DEFAULT_CONFIDENCE_MIN) -> dict:
    """收敛 AI 的分类结果。

    Args:
        data: 解析后的字段值（含 `category` / `confidence` / `reason`）。
        scheme: 分类方案。
        allow_new: 是否接受方案外的分类名。
        confidence_min: 低于此置信度会带 `low_confidence=True` 标记。

    Returns:
        `{'category', 'confidence', 'reason', 'low_confidence', 'issues', 'warnings'}`
    """
    payload = data if isinstance(data, dict) else {}

    raw_category = payload.get('category') or payload.get('target_category') or ''
    confidence = payload.get('confidence')
    reason = str(payload.get('reason') or '').strip()

    issues = []
    warnings = []

    try:
        score = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        score = None
        warnings.append(f'置信度不是数字，已忽略: {confidence!r}')

    path, error = resolve_choice(raw_category, scheme, allow_new=allow_new)

    # 模型明确说「不属于任何候选」（category 留空）= 自认不确定，**不是**落地失败。
    # 按 §4.4 这属于 note 级：留给人看，而不是替人做决定。
    undecided = not str(raw_category or '').strip()
    if undecided:
        error = ''
        path = ''

    if error:
        issues.append(error)

    low = bool(score is not None and score < float(confidence_min or 0))
    score_provided = score is not None
    if undecided:
        # 留空 = 明确弃权，不是「低置信度」。两者语义不同：
        # - 弃权：confidence 置空，避免出现「置信度 0.95 + 低置信度」的自相矛盾；
        # - 低分：保留数字，让审批页按阈值提示。
        low = True
        score = None
        warnings.append(
            '模型认为这张卡不属于任何候选分类（category 留空），需要人工指定',
        )
    if low and not undecided:
        warnings.append(
            f'置信度 {score:.2f} 低于 {confidence_min:.2f}，建议人工确认后再移动',
        )
    if score is None and not score_provided:
        warnings.append('模型未给出置信度')

    return {
        'category': path,
        'raw_category': str(raw_category),
        'confidence': score,
        'reason': reason,
        'low_confidence': low,
        'issues': issues,
        'warnings': warnings,
    }


def build_classify_instruction(scheme) -> str:
    """生成「怎么归类」的要求文本（追加在任务规格之后）。

    三条实测补丁（§4.1）：

    1. **注入 description**：只给路径名时，`原创/都市日常` 与 `综合/其他` 的边界
       全靠猜（3 张判读偏弱的卡都是这么错的）。方案里带描述就一起给模型。
    2. **幂等指令**：卡片已经在候选分类里时直接返回它，否则重复跑会重复产出待审项。
    3. **允许说不知道**：拿不准给低 confidence；不属于任何候选就留空 category。
       模型能说「我不确定」，系统就把这条留给人看，而不是替人做决定。
    """
    items = normalize_scheme(scheme)
    lines = ['输出要求：', '1. 只输出一个 JSON 对象，不要解释或 Markdown 代码块。']

    if items:
        has_desc = any(item['description'] for item in items)
        lines.append(
            '2. category 必须从下面的分类中选一个，不要发明新分类'
            + ('（「路径 — 描述」是判断边界的主要依据）：' if has_desc else '：'),
        )
        lines.append(format_scheme(items))
    else:
        lines.append('2. category 用一个简短的中文分类名概括这张卡。')

    lines.append('3. 若卡片**已经**在某个分类，且该分类在候选里，直接返回该分类。')
    lines.append(
        '4. confidence 为 0-1 的数值：拿不准就给低分，不要硬猜。'
        '如果不属于任何候选分类，category 留空，并在 reason 里说明为什么。',
    )
    lines.append('5. reason 用一句话说明判断依据（不超过 60 字）。')

    return '\n'.join(lines)


def summarize_plan(results) -> dict:
    """按分类汇总一批分类结果（供审批页做批量接受）。"""
    items = results if isinstance(results, list) else []

    buckets = {}
    for entry in items:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get('category') or '')
        confidence = entry.get('confidence')

        bucket = buckets.setdefault(category, {
            'category': category,
            'card_ids': [],
            'low_confidence': 0,
        })
        card_id = str(entry.get('card_id') or entry.get('key') or '')
        if card_id:
            bucket['card_ids'].append(card_id)
        if entry.get('low_confidence'):
            bucket['low_confidence'] += 1

    ordered = sorted(
        buckets.values(),
        key=lambda item: (-len(item['card_ids']), item['category']),
    )
    return {'buckets': ordered, 'total': sum(len(b['card_ids']) for b in ordered)}
