"""标签池：来源、预算、相关性排序、隔离分类硬过滤、别名出站还原。

纯函数层，无 flask、无主程序依赖（设计文档 §4）。

**为什么需要独立的标签池**：

- 现状把「全部标签库、硬截断 200 条」塞进提示词。200 个中文标签 +
  分类 + 频次 ≈ **3000–5000 token**，对「分析一张卡」来说是荒谬开销，
  很可能超过卡片本身。
- 现状只按使用频次排序。频次高不代表**这张卡**用得上。

两组改进：

| 维度 | 改善 |
|---|---|
| 来源 | `library`（全库）/ `categories`（只取指定分类）/ `manual`（只用手填） |
| 预算 | 按 **token** 而非条数截断，两层注入（分类概览 + 预算内标签） |
| 排序 | `(-频次, -与卡片的词面重叠, name)` —— 让「这张卡可能用到的」优先进预算 |
| 过滤 | 隔离分类变成**硬过滤**（原先只当变量给 AI 看，是软约束） |
| 别名 | AI 输出的替代词 → **出站**还原成真实标签（单向，入站不转换） |

**与主程序的联动边界**：基底来自 `api.tags.library()`；用户裁剪只存插件档案，
**不回写主程序**（避免插件篡改用户的标签治理配置）。
"""

import re

from .tokens import estimate

#: 标签池来源
SOURCE_LIBRARY = 'library'
SOURCE_CATEGORIES = 'categories'
SOURCE_MANUAL = 'manual'
SOURCES = (SOURCE_LIBRARY, SOURCE_CATEGORIES, SOURCE_MANUAL)

DEFAULT_BUDGET_TOKENS = 1200

#: 分类概览（第 1 层）最多占预算的比例
OUTLINE_RATIO = 0.25

#: 单个标签行的最小估算（含换行）
MIN_LINE_TOKENS = 4

#: 与卡片词面重叠的判定：CJK 按 2 字滑窗，拉丁按 >=3 字母的词
_CJK_PATTERN = re.compile(r'[\u4e00-\u9fa5]+')
_WORD_PATTERN = re.compile(r'[A-Za-z][A-Za-z0-9]{2,}')
MAX_CARD_TOKENS_SCAN = 20000


def default_pool() -> dict:
    """默认标签池：等同改造前的行为（全库、按频次、尊重隔离分类）。"""
    return {
        'source': SOURCE_LIBRARY,
        'categories_include': [],
        'categories_exclude': [],
        'tags_include': [],
        'tags_exclude': [],
        'respect_isolated': True,
        'budget_tokens': DEFAULT_BUDGET_TOKENS,
        'allow_new_tags': True,
        'max_new_tags': 5,
    }


def _to_str_list(values) -> list:
    if not isinstance(values, (list, tuple, set)):
        return []
    result = []
    for item in values:
        text = str(item or '').strip()
        if text and text not in result:
            result.append(text)
    return result


def normalize_pool(raw) -> dict:
    """把任意输入收敛为合法标签池（坏值回落默认，不抛错）。"""
    data = raw if isinstance(raw, dict) else {}
    base = default_pool()

    source = str(data.get('source') or SOURCE_LIBRARY).strip().lower()
    if source not in SOURCES:
        source = SOURCE_LIBRARY

    try:
        budget = int(data.get('budget_tokens', DEFAULT_BUDGET_TOKENS) or 0)
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET_TOKENS
    budget = max(0, min(budget, 20000))

    try:
        max_new = int(data.get('max_new_tags', base['max_new_tags']) or 0)
    except (TypeError, ValueError):
        max_new = base['max_new_tags']

    return {
        'source': source,
        'categories_include': _to_str_list(data.get('categories_include')),
        'categories_exclude': _to_str_list(data.get('categories_exclude')),
        'tags_include': _to_str_list(data.get('tags_include')),
        'tags_exclude': _to_str_list(data.get('tags_exclude')),
        'respect_isolated': bool(data.get('respect_isolated', True)),
        'budget_tokens': budget,
        'allow_new_tags': bool(data.get('allow_new_tags', True)),
        'max_new_tags': max(0, min(max_new, 20)),
    }


def profile_pool(profile, config=None) -> dict:
    """解析最终生效的标签池。

    优先级：**档案 > 全局配置 > 内置默认**（与 `content_selector` 一致：
    档案是「这次任务要看什么」的直接表达，理应压过全局）。
    """
    spec = profile if isinstance(profile, dict) else {}
    conf = config if isinstance(config, dict) else {}

    base = conf.get('tag_pool') if isinstance(conf.get('tag_pool'), dict) else None
    if base is None:
        base = default_pool()

    override = spec.get('tag_pool')
    if not isinstance(override, dict) or not override:
        return normalize_pool(base)

    merged = dict(normalize_pool(base))
    merged.update(override)
    return normalize_pool(merged)


# ------------------------------------------------------------------ 词面重叠


def card_text_vocabulary(card, *, limit: int = MAX_CARD_TOKENS_SCAN) -> set:
    """从卡片文本里抽「词面词汇」，用于判断某个标签跟这张卡是否相关。

    做法刻意保守（宁可漏判也不要错判）：

    - CJK：连续汉字串的 **2 字滑窗**（「校园恋爱」→ 校园/园恋/恋爱）
    - 拉丁：≥3 字母的词，小写化

    这不是分词，只是「共享子串」的粗判据 —— 对「标签是否与卡面文字沾边」
    这个用途足够了，且零依赖。
    """
    if not isinstance(card, dict):
        return set()

    # 卡片正文可能很大；只取前 N 字符，避免为了排序把整张卡过一遍
    block = card.get('data') if isinstance(card.get('data'), dict) else card
    parts = []
    for key in ('name', 'description', 'personality', 'scenario', 'first_mes',
                'mes_example', 'creator_notes', 'system_prompt'):
        value = block.get(key)
        if value:
            parts.append(str(value))
    tags = block.get('tags')
    if isinstance(tags, (list, tuple)):
        parts.extend(str(item) for item in tags)

    text = '\n'.join(parts)[:limit]
    if not text:
        return set()

    vocabulary = set()
    for match in _CJK_PATTERN.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            vocabulary.add(run)
            continue
        for index in range(len(run) - 1):
            vocabulary.add(run[index:index + 2])
    for match in _WORD_PATTERN.finditer(text):
        vocabulary.add(match.group(0).lower())

    return vocabulary


def _overlap_score(tag: str, vocabulary) -> int:
    """标签与卡面词汇的共享度（0 表示毫无关系）。"""
    if not vocabulary:
        return 0
    text = str(tag or '').strip()
    if not text:
        return 0

    score = 0
    for match in _CJK_PATTERN.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            score += 1 if run in vocabulary else 0
            continue
        for index in range(len(run) - 1):
            if run[index:index + 2] in vocabulary:
                score += 1
    for match in _WORD_PATTERN.finditer(text):
        if match.group(0).lower() in vocabulary:
            score += 1

    return score


# ------------------------------------------------------------------ 候选集合


def classify_tags(tag_library) -> dict:
    """把标签库拆成便于过滤的几份索引。

    Returns:
        `{'tags': [...], 'tag_to_category': {...}, 'categories': [...],
           'isolated': [...], 'usage': {...}, 'blacklist': [...]}`
    """
    data = tag_library if isinstance(tag_library, dict) else {}

    tags = _to_str_list(data.get('tags'))

    taxonomy = data.get('taxonomy') if isinstance(data.get('taxonomy'), dict) else {}
    tag_to_category = {}
    raw_map = taxonomy.get('tag_to_category')
    if isinstance(raw_map, dict):
        for tag, category in raw_map.items():
            name = str(tag or '').strip()
            group = str(category or '').strip()
            if name and group:
                tag_to_category[name] = group

    categories = _to_str_list(taxonomy.get('categories').keys()
                              if isinstance(taxonomy.get('categories'), dict) else [])

    prefs = data.get('prefs') if isinstance(data.get('prefs'), dict) else {}

    isolated = data.get('isolated_categories') or prefs.get('isolated_categories') or []
    if isinstance(isolated, dict):
        isolated = list(isolated.keys())
    isolated = _to_str_list(isolated)

    blacklist = prefs.get('tag_blacklist') or []
    if isinstance(blacklist, str):
        blacklist = [part.strip() for part in blacklist.split(',') if part.strip()]
    blacklist = _to_str_list(blacklist)

    return {
        'tags': tags,
        'tag_to_category': tag_to_category,
        'categories': categories,
        'isolated': isolated,
        'usage': data.get('usage') if isinstance(data.get('usage'), dict) else {},
        'blacklist': blacklist,
    }


def resolve_candidates(tag_library, pool, *, card=None) -> dict:
    """按标签池的「来源」与「排除项」算出可用候选集。

    Returns:
        `{'allowed': [...], 'excluded': {tag: reason}, 'isolated_categories': [...]}`
    """
    spec = normalize_pool(pool)
    index = classify_tags(tag_library)

    known = set(index['tags'])
    tag_to_category = index['tag_to_category']

    isolated_categories = set(index['isolated']) if spec['respect_isolated'] else set()
    include_categories = set(spec['categories_include'])
    exclude_categories = set(spec['categories_exclude']) | isolated_categories

    manual_in = list(spec['tags_include'])
    manual_out = set(spec['tags_exclude'])

    excluded = {}

    def _category_of(tag):
        return tag_to_category.get(tag, '')

    # ---- 决定基础集合 ----
    if spec['source'] == SOURCE_MANUAL:
        base = list(manual_in)
    else:
        base = list(index['tags'])

    # ---- 分类过滤（categories 来源额外要求「必须在 include 里」）----
    selected = []
    for tag in base:
        category = _category_of(tag)

        if category and category in exclude_categories:
            if category in isolated_categories:
                excluded[tag] = f'隔离分类 {category}'
            else:
                excluded[tag] = f'排除分类 {category}'
            continue

        if include_categories and category and category not in include_categories:
            excluded[tag] = f'不在指定分类 {category}'
            continue

        if spec['source'] == SOURCE_CATEGORIES:
            if not category:
                excluded[tag] = '无分类'
                continue
            if include_categories and category not in include_categories:
                excluded[tag] = f'不在指定分类 {category}'
                continue

        if tag in manual_out:
            excluded[tag] = '手动排除'
            continue

        selected.append(tag)

    # ---- 手动追加（即使不在库里也允许；但排除项优先）----
    for tag in manual_in:
        if tag in manual_out:
            excluded[tag] = '手动排除'
            continue
        category = _category_of(tag)
        if category and category in exclude_categories:
            excluded[tag] = f'隔离分类 {category}' if category in isolated_categories else f'排除分类 {category}'
            continue
        if tag not in selected:
            selected.append(tag)

    return {
        'allowed': selected,
        # 被「明确排除」的标签（隔离/排除分类/手动排除）。
        # **注意与 allowed 的区别**：`allowed` 是候选全集，不在里面**不等于**被禁；
        # 只有这里列出的才是「无论如何都不许写」的标签。
        'blocked': sorted(excluded),
        'excluded': excluded,
        'isolated_categories': sorted(isolated_categories),
        'known': known,
    }


# ------------------------------------------------------------------ 预算与分层


def _format_line(tag: str, category: str, used: int) -> str:
    parts = [tag]
    if category:
        parts.append(f'分类:{category}')
    if used:
        parts.append(f'已用{used}次')
    return '- ' + ' · '.join(parts)


def _outline_line(category: str, count: int) -> str:
    return f'- {category}（{count} 个）'


def build_pool_text(tag_library, pool, *, card=None, budget_tokens=None,
                    usage=None) -> dict:
    """按预算构造两层标签池文本。

    第 1 层：分类概览（只有分类名 + 数量，成本极低但能让模型知道「有什么维度」）。
    第 2 层：预算内的标签明细，按 `(-频次, -词面重叠, name)` 排序。

    Args:
        usage: 使用频次表；不给则取标签库自带的那份，
            再没有就不按频次排序（仍会按词面重叠与名称排）。

    Returns:
        `{'text', 'outline', 'listed', 'dropped', 'tokens', 'budget_tokens',
           'excluded', 'isolated_categories', 'available'}`
    """
    spec = normalize_pool(pool)
    index = classify_tags(tag_library)
    resolved = resolve_candidates(tag_library, spec, card=card)

    allowed = resolved['allowed']
    usage_map = usage if isinstance(usage, dict) else index['usage']
    if not isinstance(usage_map, dict):
        usage_map = {}
    tag_to_category = index['tag_to_category']

    budget = int(budget_tokens if budget_tokens is not None else spec['budget_tokens'] or 0)
    budget = max(0, budget)

    # ---- 候选统计（给 UI 与预览用，不受预算影响）----
    available = len(allowed)
    result = {
        'text': '',
        'outline': '',
        'listed': [],
        'dropped': [],
        'tokens': 0,
        'budget_tokens': budget,
        'excluded': dict(sorted(resolved['excluded'].items())),
        'isolated_categories': resolved['isolated_categories'],
        'available': available,
    }

    if not allowed or budget <= 0:
        result['dropped'] = list(allowed)
        # 预算为 0 时仍要给出候选集与黑名单：硬过滤与「发多少」是两件事
        result['allowed'] = list(allowed)
        result['blocked'] = resolved['blocked']
        return result

    vocabulary = card_text_vocabulary(card)

    def _sort_key(tag):
        return (
            -int(usage_map.get(tag) or 0),
            -_overlap_score(tag, vocabulary),
            tag,
        )

    ordered = sorted(allowed, key=_sort_key)

    # ---- 第 1 层：分类概览 ----
    category_counts = {}
    for tag in ordered:
        category = tag_to_category.get(tag)
        if category:
            category_counts[category] = category_counts.get(category, 0) + 1

    outline_lines = []
    outline_tokens = 0
    outline_budget = int(budget * OUTLINE_RATIO)

    for category in sorted(category_counts, key=lambda name: (-category_counts[name], name)):
        line = _outline_line(category, category_counts[category])
        cost = estimate(line) + 1
        if outline_tokens + cost > outline_budget and outline_lines:
            break
        outline_lines.append(line)
        outline_tokens += cost

    # ---- 第 2 层：预算内标签明细 ----
    remaining = budget - outline_tokens
    listed = []
    dropped = []
    detail_lines = []
    detail_tokens = 0

    for tag in ordered:
        line = _format_line(tag, tag_to_category.get(tag, ''), int(usage_map.get(tag) or 0))
        cost = estimate(line) + 1
        if cost >= remaining or detail_tokens + cost > remaining:
            dropped.append(tag)
            continue
        detail_lines.append(line)
        detail_tokens += cost
        listed.append(tag)

    sections = []
    if outline_lines:
        sections.append('（分类概览）\n' + '\n'.join(outline_lines))
    if detail_lines:
        sections.append('\n'.join(detail_lines))
    if dropped:
        sections.append(f'（还有 {len(dropped)} 个标签因预算未列出）')

    text = '\n\n'.join(sections)

    # 尾部括注也吃预算：超出就逐项回退，直到装进预算。
    # 不做这一步会出现「预算 20、实际 31」—— 预算形同虚设。
    while detail_lines and estimate(text) > budget:
        dropped.insert(0, listed.pop())
        detail_lines.pop()
        sections = []
        if outline_lines:
            sections.append('（分类概览）\n' + '\n'.join(outline_lines))
        if detail_lines:
            sections.append('\n'.join(detail_lines))
        if dropped:
            sections.append(f'（还有 {len(dropped)} 个标签因预算未列出）')
        text = '\n\n'.join(sections)

    if estimate(text) > budget:
        # 连索引 + 括注都放不下 → 只留一句最小提示（再不行就彻底不出）
        sections = []
        if detail_lines:
            sections.append('\n'.join(detail_lines))
        text = '\n\n'.join(sections)
        outline_lines = []

    result.update({
        'text': text,
        'outline': '\n'.join(outline_lines),
        'listed': listed,
        'dropped': dropped,
        'tokens': estimate(text),
        # 候选全集（不在于此 = 陌生标签，会走「新标签建议」分支）
        'allowed': list(allowed),
        # 明确被禁的标签：只有这些才该被硬过滤挡下
        'blocked': resolved['blocked'],
    })
    return result


# ------------------------------------------------------------------ 别名还原


def normalize_aliases(raw) -> dict:
    """把别名表规范化为 `{alias: canonical}`。

    支持三种输入形态（与主程序 `parse_merge_rules` 同源，便于从规则集导入）：

    - `[{"alias": "a", "canonical": "b"}]`（本插件原生形态）
    - `[{"from": "a", "to": "b"}]`
    - `["a→b", "c -> d"]`
    """
    result = {}

    if isinstance(raw, dict):
        items = [{'alias': key, 'canonical': value} for key, value in raw.items()]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return result

    arrow = re.compile(r'(.*?)(?:→|->|=>)(.+)')

    for entry in items:
        alias = canonical = ''

        if isinstance(entry, str):
            match = arrow.search(entry)
            if match:
                alias = match.group(1).strip()
                canonical = match.group(2).strip()
        elif isinstance(entry, dict):
            alias = str(entry.get('alias') or entry.get('from') or '').strip()
            canonical = str(entry.get('canonical') or entry.get('to')
                            or entry.get('target') or '').strip()

        if alias and canonical and alias != canonical:
            result[alias] = canonical

    return result


def resolve_alias(tag: str, aliases, *, _seen=None) -> str:
    """沿别名链解析到终点（`a→b→c` 得到 `c`），带环检测。"""
    current = str(tag or '').strip()
    if not current:
        return ''

    seen = set(_seen or ())
    while current in aliases:
        if current in seen:
            # 成环：停在当前值，不追下去（避免死循环）
            break
        seen.add(current)
        nxt = str(aliases.get(current) or '').strip()
        if not nxt or nxt == current:
            break
        current = nxt

    return current


def restore_aliases(tags, aliases) -> tuple:
    """把 AI 输出的替代词还原成真实标签（**出站**方向）。

    必须在 `split_tags` **之前**做：那里用真实标签集判断「已知 / 新标签」，
    若 AI 输出的是别名而没先换回来，真实标签会被误判成新标签进建议队列。

    Args:
        tags: `[{'name', 'confidence'}]` 或字符串列表。
        aliases: 别名表（三种形态皆可）。

    Returns:
        `(归一化后的 tags, {'applied': [{'from','to'}], 'aliases': {...}})`
    """
    table = normalize_aliases(aliases)

    items = tags if isinstance(tags, list) else []
    applied = []
    result = []
    seen = {}

    for entry in items:
        if isinstance(entry, dict):
            original = str(entry.get('name') or '').strip()
        else:
            original = str(entry or '').strip()

        if not original:
            continue

        resolved = resolve_alias(original, table) or original
        if resolved != original:
            applied.append({'from': original, 'to': resolved})

        # 还原后可能与已有项重合：合并置信度，取较高者
        if resolved in seen:
            existing = result[seen[resolved]]
            current_conf = entry.get('confidence') if isinstance(entry, dict) else None
            kept = existing.get('confidence')
            if current_conf is not None and (kept is None or current_conf > kept):
                existing['confidence'] = current_conf
            continue

        seen[resolved] = len(result)
        if isinstance(entry, dict):
            merged = dict(entry)
            merged['name'] = resolved
            result.append(merged)
        else:
            result.append(resolved)

    return result, {'applied': applied, 'aliases': table}


def validate_aliases(aliases, known_tags=None) -> list:
    """校验别名表，返回错误信息列表。

    检查两件真会出错的事：

    1. **同一别名映射到多个 canonical**（歧义）→ 报错。
    2. **别名恰好是真实标签**（出站会误替换真词）→ 报错。
       例如把 `校园` 当别名映射到 `学园`，那么模型正常输出的真实标签
       `校园` 也会被悄悄改掉。
    """
    errors = []

    raw_items = []
    if isinstance(aliases, dict):
        raw_items = [{'alias': key, 'canonical': value} for key, value in aliases.items()]
    elif isinstance(aliases, (list, tuple)):
        raw_items = list(aliases)

    arrow = re.compile(r'(.*?)(?:→|->|=>)(.+)')
    seen = {}

    for entry in raw_items:
        if isinstance(entry, str):
            match = arrow.search(entry)
            if not match:
                continue
            alias = match.group(1).strip()
            canonical = match.group(2).strip()
        elif isinstance(entry, dict):
            alias = str(entry.get('alias') or entry.get('from') or '').strip()
            canonical = str(entry.get('canonical') or entry.get('to')
                            or entry.get('target') or '').strip()
        else:
            continue

        if not alias or not canonical:
            continue
        if alias == canonical:
            errors.append(f'别名与目标相同，无意义: {alias}')
            continue

        if alias in seen and seen[alias] != canonical:
            errors.append(
                f'别名「{alias}」同时映射到「{seen[alias]}」与「{canonical}」（歧义）',
            )
            continue
        seen[alias] = canonical

    if known_tags:
        known = {str(item or '').strip() for item in known_tags}
        for alias in seen:
            if alias in known:
                errors.append(
                    f'别名「{alias}」与真实标签同名，出站会误替换真实标签',
                )

    return errors
